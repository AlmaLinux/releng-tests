"""A NEVRA must name exactly one artifact, everywhere it appears.

``name-epoch:version-release.arch`` is RPM's identity. Two files that
share a NEVRA but differ in bytes are, as far as every tool in the
stack is concerned, the same package — ``dnf`` will happily install
whichever copy a given mirror or pipeline stage hands it, and no
upgrade path will ever reconcile them. The only correct number of such
pairs in a release is zero.

The existing parity tests check this *within* one release:
``test_noarch_parity`` invariant (A) asserts same-NEVRA-same-sha256
across arches and repos — but only for ``noarch`` packages, and only
for the one source under test. Nothing compared a release against the
other places its own packages live. This module does, and it covers
every arch, not just ``noarch``.

What it catches, concretely: a package rebuilt or re-signed without a
release bump — either between two stages of the pipeline (build →
compose → beta → GA), or relative to what the current GA already ships
— and a stale artifact left behind on a mirror when a same-NEVRA
replacement was published elsewhere. Both look like nothing at all in
a per-release check, because inside either release the file is
perfectly self-consistent.

Two references, two tests
-------------------------
The invariant is unconditional — a NEVRA shared by *any* two
repositories must carry the same bytes, whatever releases they belong
to — so the reference does not have to be "the matching release". It
only has to be somewhere the same NEVRAs actually turn up. Two
references answer two different operator questions, so each gets its
own test and its own row in the report:

* **adjacent pipeline stage** (:data:`_ADJACENT_STAGE`) — "did this
  package change between hand-offs of *this* release?" The two sides
  overlap heavily by construction, so zero shared NEVRAs means the
  reference went stale and the test fails rather than passing empty.
* **the current GA** (:data:`_GA_CHECKED_SOURCES`, compared against
  ``stable X.(Y-1)``) — "did a pre-release stage rebuild something the
  GA already ships, without bumping its release?" Here zero overlap is
  a legitimate *pass*: ``pulp``'s build repo carries only fresh builds
  and normally shares nothing with the GA, which is exactly the
  property being asserted.

Shared-NEVRA counts measured on ``x86_64`` when this module was
written:

==================  ==================  =================
source under test   candidate           shared NEVRAs
==================  ==================  =================
``beta`` 10.3       ``pungi`` 10.3      4354
``beta`` 10.3       ``stable`` 10.2     5073
``pungi`` 10.3      ``stable`` 10.2     5045
``pulp`` 10.3       ``pungi`` 10.3      1744
``pulp`` 10.3       ``stable`` 10.2     **0**
``stable`` 10.2     ``beta`` 10.2       619 (BaseOS only)
``stable`` 10.2     ``stable`` 10.1     **0** — 404
==================  ==================  =================

That last zero is why ``stable`` is not in the GA test: a **superseded
minor is pruned from the public mirror** — once 10.2 shipped,
``repo.almalinux.org/almalinux/10.1`` began answering 404 (the same
pruning that makes ``test_iso_presence`` fail on an old minor). A
``stable`` run is instead checked against the **beta of its own
version**: vault keeps every past beta, and "the GA ships what the beta
shipped" is the invariant worth asserting there anyway. For every
other source the previous minor *is* the current GA, still served.

The per-arch counts of actually-compared NEVRAs go into the report via
``report_detail``, so a green row can be told apart from a row that
had nothing to compare.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest
import requests

from post_check.config import (
    arch_supports_major,
    compute_prev_version,
    load_architectures,
    load_sources,
)
from post_check.helpers import http, repodata
from post_check.helpers.repo_scan import collect_packages_by_arch

pytestmark = [pytest.mark.online, pytest.mark.release]


# source-under-test -> the pipeline stage it is compared against, at the
# same version. Each pair is one hand-off where a package can get
# rebuilt without a release bump: stable is checked against its own
# beta, beta against the compose, and pulp's build repo against the
# compose its builds feed. pungi has no entry: everything upstream of a
# compose (pulp) is checked from its own side, and nothing downstream
# (beta) reliably exists yet while a compose is being validated. Its
# reference is the GA, below.
_ADJACENT_STAGE: dict[str, str] = {
    "stable": "beta",
    "beta": "pungi",
    "pulp": "pungi",
}

# Every pre-GA stage must leave the current GA's packages alone: a NEVRA
# that stable X.(Y-1) already ships may reappear in X.Y only as the very
# same file. stable itself is absent — a stable run *is* the GA, and its
# own previous minor is pruned from the mirror (see module docstring).
_GA_CHECKED_SOURCES: frozenset[str] = frozenset({"beta", "pungi", "pulp"})


def _nevra(pkg: repodata.Package) -> str:
    """RPM identity as a flat string. Epoch is deliberately excluded.

    ``primary.xml`` reports an epoch of ``0`` for packages whose RPM
    header has none at all, and the two are not distinguishable here.
    Including it would therefore create spurious mismatches between
    repositories whose metadata was generated by different createrepo
    versions, which is a metadata-quality question rather than the
    byte-identity one this module asks.
    """
    return f"{pkg.name}-{pkg.version}-{pkg.release}.{pkg.arch}"


def compute_nevra_checksum_conflicts(
    target: dict[str, dict[str, str]],
    reference: dict[str, dict[str, str]],
    *,
    target_label: str = "target",
    reference_label: str = "reference",
) -> list[str]:
    """Pure check: NEVRAs present in both sides with differing checksums.

    Input shape for both sides::

        {arch: {nevra: checksum}}

    Output: human-readable lines, empty when the two agree on every
    NEVRA they share.

    Split out from the test body so the meta-tests in
    ``tests/internal/test_assertions_actually_fire.py`` can prove both
    the detection and the rendering fire on synthetic input, with no
    HTTP involved.
    """
    failures: list[str] = []
    for arch in sorted(target):
        ref = reference.get(arch)
        if not ref:
            continue
        tgt = target[arch]
        conflicts = sorted(
            n for n in tgt.keys() & ref.keys() if tgt[n] != ref[n]
        )
        if not conflicts:
            continue
        failures.append(
            f"[{arch}] {len(conflicts)} NEVRA(s) with differing content "
            f"between {target_label} and {reference_label}:"
        )
        for n in conflicts:
            failures.append(f"  {n}")
            failures.append(f"    {target_label:<24} sha256={tgt[n]}")
            failures.append(f"    {reference_label:<24} sha256={ref[n]}")
    return failures


def _all_arches(major: str | None = None) -> tuple[str, ...]:
    """Every architecture published for AlmaLinux ``major``.

    Same rationale as the other parity modules: byte-identity is a
    property of the release, not of the operator's ``ALMA_ARCHES``
    subset, so narrowing the matrix must not narrow the invariant.
    """
    arches = sorted(load_architectures().keys())
    if major is not None:
        arches = [a for a in arches if arch_supports_major(a, major)]
    return tuple(arches)


def _checksums_by_arch(
    session: requests.Session,
    *,
    source: str,
    version: str,
    arches: Iterable[str],
) -> dict[str, dict[str, str]]:
    """``{arch: {nevra: checksum}}`` for one source, full repo set.

    A NEVRA seen more than once inside the *same* source keeps the
    first checksum encountered. That intra-source case is already
    owned by ``test_noarch_parity`` invariant (A); re-reporting it here
    would duplicate findings across two tests in the same report.
    """
    collected = collect_packages_by_arch(
        session,
        source=source,
        version=version,
        arches=arches,
        repos=list(load_sources()[source]["repos"]),
    )
    return {
        arch: {_nevra(p): p.checksum for p in reversed(pkgs)}
        for arch, pkgs in collected.items()
    }


def _report_overlap(
    report_detail: Callable[[str], None],
    arches: Iterable[str],
    target: dict[str, dict[str, str]],
    reference: dict[str, dict[str, str]],
    reference_label: str,
) -> int:
    """Put per-arch compared-NEVRA counts in the report; return the total.

    A run that compared nothing is indistinguishable from a clean one
    in the report unless the counts are spelled out.
    """
    total_shared = 0
    for arch in arches:
        ref = reference.get(arch, {})
        shared = len(target[arch].keys() & ref.keys())
        total_shared += shared
        report_detail(
            f"`{arch}`: {len(target[arch])} pkg(s) vs {len(ref)} in "
            f"{reference_label} — {shared} shared NEVRA(s) compared"
        )
    return total_shared


def test_nevra_content_matches_adjacent_stage(runtime_config, report_detail):
    """No NEVRA may change bytes between adjacent stages of the pipeline.

    Collects every binary RPM of the release under test and of the
    pipeline stage next to it (stable↔beta, beta↔pungi, pulp↔pungi, all
    at the same version), then asserts that every NEVRA they have in
    common resolves to the same sha256. A mismatch means the package
    was rebuilt between hand-offs without a release bump.
    """
    source = runtime_config.source
    version = runtime_config.version

    if source not in _ADJACENT_STAGE:
        pytest.skip(
            f"source={source}: no adjacent stage that reliably exists while "
            f"it is validated — its builds are checked from pulp's side, and "
            f"its GA comparison is test_no_ga_nevra_rebuilt_with_different_content"
        )
    ref_source = _ADJACENT_STAGE[source]

    arches = _all_arches(major=version.split(".")[0])
    s = http.session()
    target = _checksums_by_arch(s, source=source, version=version, arches=arches)
    reference = _checksums_by_arch(s, source=ref_source, version=version, arches=arches)

    target_label = f"{source} {version}"
    reference_label = f"{ref_source} {version}"

    # Adjacent stages overlap by thousands of NEVRAs by construction, so
    # sharing none on every arch means the reference went stale (a
    # compose rolled over, a mirror was pruned) and the check silently
    # stopped testing anything.
    total_shared = _report_overlap(report_detail, arches, target, reference, reference_label)
    assert total_shared, (
        f"{target_label} and {reference_label} share no NEVRA on any arch — "
        f"the reference is not comparable any more, so this check verified "
        f"nothing. Revisit _ADJACENT_STAGE."
    )

    failures = compute_nevra_checksum_conflicts(
        target,
        reference,
        target_label=target_label,
        reference_label=reference_label,
    )
    assert not failures, "\n".join(failures)


def test_no_ga_nevra_rebuilt_with_different_content(runtime_config, report_detail):
    """No package the current GA ships may reappear under its NEVRA with other bytes.

    Collects every binary RPM of the pre-release stage under test
    (beta, pungi or pulp at X.Y) and of the current GA (stable
    X.(Y-1)), then asserts that every NEVRA they have in common
    resolves to the same sha256. A mismatch means a GA package was
    rebuilt for the new minor without a release bump, so ``dnf`` on a
    GA system will never pick the new build up.
    """
    source = runtime_config.source
    version = runtime_config.version

    if source not in _GA_CHECKED_SOURCES:
        pytest.skip(
            f"source={source}: this run is the GA itself; it is compared "
            f"against its own beta by test_nevra_content_matches_adjacent_stage"
        )
    ga_version = compute_prev_version(version)
    if ga_version is None:
        pytest.skip(
            f"{source} {version}: no in-major previous minor, so no GA of this "
            f"major to compare against (x.0 is the first minor of its major, "
            f"and major-only versions carry no previous minor)"
        )

    arches = _all_arches(major=version.split(".")[0])
    s = http.session()
    target = _checksums_by_arch(s, source=source, version=version, arches=arches)
    reference = _checksums_by_arch(s, source="stable", version=ga_version, arches=arches)

    # The guard that matters here is that the GA was fetched at all, not
    # that it overlaps: pulp's build repo legitimately shares nothing
    # with the GA. An empty GA means every cell answered 404, i.e. the
    # minor was superseded and pruned from the mirror after this
    # release shipped — there is nothing left to compare against.
    if not any(reference.values()):
        pytest.skip(
            f"stable {ga_version} is no longer served on the public mirror "
            f"(a superseded minor is pruned once the next one ships), so "
            f"there is no GA to compare {source} {version} against"
        )

    target_label = f"{source} {version}"
    reference_label = f"stable {ga_version} (GA)"
    _report_overlap(report_detail, arches, target, reference, reference_label)

    failures = compute_nevra_checksum_conflicts(
        target,
        reference,
        target_label=target_label,
        reference_label=reference_label,
    )
    assert not failures, "\n".join(failures)
