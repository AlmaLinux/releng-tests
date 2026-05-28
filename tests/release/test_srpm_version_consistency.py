"""Per-arch SRPM version consistency across all repos of a release.

For every published architecture, every binary RPM must reference
exactly **one** version of any given source RPM name. A release where
two binaries from different versions of the same SRPM coexist
(``foo-bin-1.0-1.x86_64.rpm`` next to ``foo-doc-1.0-2.x86_64.rpm``,
both listed in some ``primary.xml`` under the same arch) is
"split-brain": dnf would non-deterministically resolve subpackages
to disagreeing builds, and users would see version drift between
binaries that were supposed to ship together.

The check catches publication bugs where a rebuild was pushed for
some subpackages but not all of them, or where stale binary RPMs from
an earlier build were left in the repository after the SRPM was
bumped. Both are observed in practice on the per-arch composes —
hence the existing per-arch
``check-<arch>-compose-srpm-versions-<major>.py`` scripts under
``releng-almalinux/tools/``, which this test consolidates into a
single ``pytest.mark.release`` invariant.

Like the other parity-style tests in this suite, the check spans the
**full** architecture matrix from ``config/architectures.yaml`` AND
the full repo set of the source. Consistency is a release-level
property and must hold regardless of which subset the operator passed
via ``ALMA_ARCHES`` / ``ALMA_REPOS`` — otherwise narrowing the matrix
would silently turn the check into a no-op.

Modular packages (``.module`` in release) are excluded. Module streams
have their own per-arch / per-stream lifecycle and routinely carry
multiple co-existing builds in a single repodata; counting them as a
parity violation would flood the report with AL9 false positives
(same rationale as :func:`tests.release.test_noarch_parity._is_modular`).

Scope note: debug / debuginfo repositories are intentionally NOT
walked. A debug binary shares its SRPM with the corresponding main
binary by construction, so an inconsistency in debug almost always
also surfaces in main. Adding debug coverage is straightforward (an
extra URL pattern per source) if a real-world miss ever needs catching
there — the original per-arch scripts walk debug too, and the
simplification here is a deliberate trade for a cleaner first cut.

TODO: the ``devel`` repository (``.../almalinux/<major>/devel/<arch>/os/``)
is also intentionally NOT walked here. ``devel`` has its own lifecycle —
pre-GA / staging builds land there and routinely sit at different EVRs
than the user-facing repos, so folding it into this invariant would
flood the report with drift that is by-design. If we want to surface
the state of ``devel`` in the release artefact, it should be a
**separate** ``pytest.mark.release`` test whose failures are scoped to
``devel`` alone (or surfaced via ``report_detail`` as soft warnings).
Adding it as another entry in ``_DEFAULT_REPOS`` would break unrelated
tests (``test_mirrorlist``, ``.repo``-section parity, etc.) that
correctly do not expect ``devel`` on a stock install.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
import requests

from post_check.config import arch_supports_major, load_architectures, load_sources
from post_check.helpers import http, repodata
from post_check.helpers.rpm_evr import evr_cmp
from post_check.helpers.url_builder import RepoURL, pulp_internal_beta_repo_base

pytestmark = [pytest.mark.online, pytest.mark.release]


# ``name-version-release.src.rpm``. The greedy first group consumes
# the longest match for the package name (which itself can contain
# hyphens, e.g. ``glibc-langpack-de``); ``[^-]+`` on the two trailing
# groups forces them to be the LAST two hyphen-separated chunks
# before ``.src.rpm``.
_SRPM_RE = re.compile(r"^(.+)-([^-]+)-([^-]+)\.src\.rpm$")


def _parse_srpm(filename: str) -> tuple[str, str, str] | None:
    """Parse ``foo-1.0-1.el10.src.rpm`` -> ``("foo", "1.0", "1.el10")``.

    Returns ``None`` for malformed input. We treat a malformed
    ``sourcerpm`` field as a soft skip rather than a hard failure —
    that's a separate metadata-quality bug, not an SRPM version
    inconsistency.
    """
    m = _SRPM_RE.match(filename)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def _is_modular(pkg: repodata.Package) -> bool:
    """True iff ``pkg`` was built as part of a DNF module.

    Modular RPMs are versioned per stream, independently of the base
    release, and routinely coexist with the non-modular build of the
    same SRPM in a single repodata. Counting them as inconsistencies
    would create false positives on every AL9 release that ships
    modular content.
    """
    return ".module" in pkg.release


def compute_srpm_version_inconsistencies(
    arch_to_packages: dict[str, Iterable[repodata.Package]],
) -> list[str]:
    """Pure check: returns per-arch SRPM inconsistency lines.

    Input shape::

        {arch: iterable of repodata.Package}   # all repos of one arch

    Output: a list of human-readable lines (empty == clean release).

    Split out from the test body so the meta-tests in
    ``tests/internal/test_assertions_actually_fire.py`` can feed
    synthetic :class:`repodata.Package` records and prove the detection
    logic AND the rendering both fire on the right shape of breakage
    without going through HTTP.

    Layout of a failure block (one per affected arch)::

        [<arch>] <N> source package(s) with multiple versions:
          <srpm-name>:
            Newest SRPM: <srpm-filename>
            Older  SRPM: <srpm-filename>
              - <binary-rpm-filename>
              - …

    The "Older SRPM"'s binary RPM filenames are what the operator
    actually needs to act on — those are the files to delete from the
    repository before re-running ``createrepo`` to clear the drift.
    """
    failures: list[str] = []
    for arch in sorted(arch_to_packages):
        # srpm_name -> {(version, release, srpm_filename) -> [(nevra, location)]}
        srpm_map: dict[
            str, dict[tuple[str, str, str], list[tuple[str, str]]]
        ] = defaultdict(lambda: defaultdict(list))
        for pkg in arch_to_packages[arch]:
            if not pkg.sourcerpm:
                # Stripped metadata — can't decide consistency, soft skip.
                continue
            if _is_modular(pkg):
                continue
            parsed = _parse_srpm(pkg.sourcerpm)
            if not parsed:
                continue
            src_name, src_ver, src_rel = parsed
            key = (src_ver, src_rel, pkg.sourcerpm)
            srpm_map[src_name][key].append((pkg.nevra, pkg.location))

        inconsistent = {
            name: versions
            for name, versions in srpm_map.items()
            if len(versions) > 1
        }
        if not inconsistent:
            continue

        block: list[str] = [
            f"[{arch}] {len(inconsistent)} source package(s) with "
            f"multiple versions:"
        ]
        for src_name in sorted(inconsistent):
            versions = inconsistent[src_name]
            keys = list(versions.keys())
            # Pick the newest SRPM via rpmvercmp. SRPM filenames don't
            # encode epoch (RPM convention) — same as the original
            # per-arch scripts, we compare with epoch=0.
            newest = keys[0]
            for k in keys[1:]:
                if evr_cmp(("0", k[0], k[1]), ("0", newest[0], newest[1])) > 0:
                    newest = k
            older = [k for k in keys if k != newest]

            block.append(f"  {src_name}:")
            block.append(f"    Newest SRPM: {newest[2]}")
            for k in older:
                block.append(f"    Older  SRPM: {k[2]}")
                for nevra, loc in sorted(versions[k]):
                    fname = loc.rsplit("/", 1)[-1] if loc else f"{nevra}.rpm"
                    block.append(f"      - {fname}")
        failures.extend(block)
    return failures


def _all_arches(major: str | None = None) -> tuple[str, ...]:
    """Every architecture published for AlmaLinux ``major``.

    Same rationale as
    :func:`tests.release.test_noarch_parity._all_arches`: consistency
    is a release property, not subject to the operator's
    ``ALMA_ARCHES`` subset. ``skip_categories`` is intentionally NOT
    consulted — every published arch must satisfy the invariant.
    """
    arches = sorted(load_architectures().keys())
    if major is not None:
        arches = [a for a in arches if arch_supports_major(a, major)]
    return tuple(arches)


# Per (arch, repo) we fetch one ``primary.xml`` and stream-parse every
# binary RPM in it. With the full matrix that's ~36-50 fetches per run,
# and each ``primary.xml`` carries thousands of packages — running them
# sequentially blows past the wall-clock budget. The original per-arch
# scripts under ``releng-almalinux/tools/`` solved this with a
# ``ThreadPoolExecutor`` of 4 workers; we use the same approach. 8 is
# a comfortable upper bound for the AlmaLinux mirror network — higher
# and we start seeing connection-reset noise from the CDN.
_FETCH_WORKERS = 8


def _fetch_one_cell(
    session: requests.Session,
    repo_base: str,
) -> list[repodata.Package]:
    """Fetch + parse one ``(arch, repo)`` cell's ``primary.xml``.

    Returns ``[]`` on 403/404 — a missing repo (RT/NFV on non-x86,
    ResilientStorage on AL10, an arch not yet pushed to the
    internal-beta) is not a consistency violation. Other HTTP errors
    bubble up: those are infrastructure problems, not "release
    missing", and the operator wants the raw exception (same contract
    as :func:`tests.release.test_release_parity._find_pkg`).
    """
    try:
        primary = repodata.find_primary_xml_url(session, repo_base)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (403, 404):
            return []
        raise
    return list(repodata.iter_packages(session, primary))


def _collect_arch_packages_parallel(
    session: requests.Session,
    source: str,
    version: str,
    arches: Iterable[str],
    repos: Iterable[str],
) -> dict[str, list[repodata.Package]]:
    """Fetch every ``(arch, repo)`` cell in parallel; aggregate per arch.

    Parallelism is per (arch, repo) — the actual unit of network I/O —
    so a slow arch's biggest repo doesn't gate the rest. Total
    wall-clock is bounded by the slowest single cell rather than the
    sum of all of them. On ``pulp`` the named repo set collapses to
    one flat per-arch URL — we only schedule one cell per arch in
    that case, and ``repos`` is ignored.

    The :class:`requests.Session` returned by ``http.session()`` (a
    ``requests_cache.CachedSession`` wrapping ``requests.Session``) is
    safe to share across threads — each ``get()`` uses its own
    underlying urllib3 connection from the pool.
    """
    arches = list(arches)
    arch_to_packages: dict[str, list[repodata.Package]] = {a: [] for a in arches}

    # Pre-resolve all URLs synchronously (cheap, no I/O — just config
    # lookups). The futures dict is keyed by arch so a single fetch
    # failure surfaces against the right arch.
    tasks: list[tuple[str, str]] = []  # (arch, repo_base)
    for arch in arches:
        if source == "pulp":
            tasks.append((arch, pulp_internal_beta_repo_base(version=version, arch=arch)))
            continue
        for repo in repos:
            base = RepoURL.from_config(
                source=source, version=version, arch=arch, repo=repo
            ).repo_base()
            tasks.append((arch, base))

    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_one_cell, session, base): arch
            for arch, base in tasks
        }
        for fut in as_completed(futures):
            arch = futures[fut]
            arch_to_packages[arch].extend(fut.result())
    return arch_to_packages


def test_srpm_versions_consistent_within_each_arch(runtime_config, report_detail):
    """Each arch must reference exactly one version of every source RPM.

    For every published arch of the release, the test collects every
    binary RPM across the full repo set and builds the map
    ``{srpm_name: {srpm_evr: [binary RPMs]}}``. Any ``srpm_name`` with
    more than one distinct EVR is a publication inconsistency: older
    binary RPMs were left behind after the SRPM was rebuilt, or the
    rebuild was pushed for some subpackages but not others.
    """
    major = runtime_config.version.split(".")[0]
    arches = _all_arches(major=major)
    # pulp's internal-beta is a flat per-arch repo (no BaseOS / AppStream
    # split) — the per-component repo list collapses to one URL. For
    # every other source we walk the full repo set.
    if runtime_config.source == "pulp":
        repos: list[str] = []
    else:
        repos = list(load_sources()[runtime_config.source]["repos"])

    s = http.session()
    arch_to_packages = _collect_arch_packages_parallel(
        s, runtime_config.source, runtime_config.version, arches, repos
    )
    # Surface the per-arch package count under the test bullet so an
    # operator reading a green ✅ can still see that the test actually
    # had data to chew on (vs. silently inspecting an empty matrix).
    for arch in arches:
        report_detail(f"`{arch}`: {len(arch_to_packages[arch])} package(s) inspected")

    failures = compute_srpm_version_inconsistencies(arch_to_packages)
    assert not failures, "\n".join(failures)
