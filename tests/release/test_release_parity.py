"""Parity of ``almalinux-release`` / ``almalinux-repos`` across arches.

Contract (see README, item 9): these packages must have the **same
N-E-V-R** across all arches of a single release. Architecture (A in
NEVRA) and binary sha256 are NOT part of the invariant: AlmaLinux 10.1
ships these packages as per-arch (``almalinux-release-10.1-16.el10.x86_64.rpm``
and ``.aarch64.rpm``), so the A component and the binary checksum
naturally differ. Logically, however, this is the same package -- its
name, epoch, version, and release must match.

Parity is a property of **the release**, not of whatever subset of
arches the operator happened to pass via ``ALMA_ARCHES``. So these
tests iterate over the full architecture matrix from
``config/architectures.yaml`` regardless of ``ALMA_ARCHES`` — passing
``ALMA_ARCHES=x86_64`` must not silently turn parity into a no-op.

Per-source URL where the parity-target build of ``almalinux-release``
actually lives:

* ``stable`` / ``beta`` / ``pungi`` — ``BaseOS`` of the
  release-under-test (built by :class:`RepoURL`).
* ``pulp`` — the layered **internal-beta** repo on
  ``build.almalinux.org/pulp/content/...`` (flat, not split into
  BaseOS/AppStream/…). The major-aliased stable URL that
  :class:`RepoURL` returns for ``source="pulp"`` points at the current
  GA in that major (``10.1``) — already validated by stable runs and,
  by construction, can't satisfy ``version == ALMA_VERSION`` (where
  ``ALMA_VERSION`` is the upgrade target, e.g. ``10.2``). The
  internal-beta IS the layer that carries the bumped release.
"""

import pytest
import requests
from post_check.config import arch_supports_major, load_architectures
from post_check.helpers.url_builder import RepoURL, pulp_internal_beta_repo_base
from post_check.helpers import http, repodata

pytestmark = [pytest.mark.online, pytest.mark.release]


def _all_arches(major: str | None = None) -> tuple[str, ...]:
    """Every architecture published for the given AlmaLinux ``major``.

    Lives here (not in ``runtime_config``) on purpose: parity is a
    cross-arch invariant of the release, never of the operator's
    local subset.

    When ``major`` is given, arches whose ``supported_majors`` doesn't
    include that major are filtered out — otherwise ``x86_64_v2``
    (AL10-only) would show up as "missing on AL9" in parity output.
    Without ``major`` the full set is returned.

    Note: ``skip_categories`` is intentionally NOT consulted here.
    Parity is a property of *every published arch*: an i686-only
    rebuild that bumps a noarch's release while the rest of the matrix
    stays put is exactly the kind of release-time skew this test
    must surface.
    """
    arches = sorted(load_architectures().keys())
    if major is not None:
        arches = [a for a in arches if arch_supports_major(a, major)]
    return tuple(arches)


def _parity_repo_base(*, source: str, version: str, arch: str) -> str:
    """Repo URL where ``almalinux-release`` / ``almalinux-repos`` live
    for the parity check, per source.

    For ``stable`` / ``beta`` / ``pungi`` it's ``BaseOS`` of the
    release-under-test, via :class:`RepoURL`. For ``pulp`` it's the
    flat per-arch internal-beta repo on
    ``build.almalinux.org/pulp/content/...`` — see the module docstring
    for why we don't go through :class:`RepoURL` for that source.
    """
    if source == "pulp":
        return pulp_internal_beta_repo_base(version=version, arch=arch)
    return RepoURL.from_config(
        source=source, version=version, arch=arch, repo="BaseOS"
    ).repo_base()


def _find_pkg(session, source, version, arch, name):
    """Return ``Package`` for ``name`` in the parity-target repo, or None.

    Returns ``None`` in two distinguishable cases — both surfaced as
    "missing on this arch" by the calling test:

    * the parity-target repo for ``(source, version, arch)`` does not
      exist (4xx on ``repomd.xml``) — happens on a non-existent /
      unpublished version (e.g. ``10.3`` while only ``10.2`` is out),
      or on pulp when the internal-beta hasn't been pushed for an arch;
    * the package is not in the repo's primary.xml.

    Other failures (5xx, network/SSL errors, malformed XML) are *not*
    swallowed — those are infrastructure problems, not "release
    missing", and the operator wants the raw exception.
    """
    base = _parity_repo_base(source=source, version=version, arch=arch)
    try:
        primary = repodata.find_primary_xml_url(session, base)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (403, 404):
            return None
        raise
    for pkg in repodata.iter_packages(session, primary):
        if pkg.name == name:
            return pkg
    return None


def _nevr(pkg) -> tuple[str, str, str, str]:
    """``(name, epoch, version, release)`` -- without arch and without checksum."""
    return (pkg.name, pkg.epoch, pkg.version, pkg.release)


@pytest.mark.parametrize("pkg_name", ["almalinux-release", "almalinux-repos"])
def test_release_pkg_same_nevr_across_arches(runtime_config, pkg_name, report_detail):
    """N-E-V-R of package ``pkg_name`` must match on every published arch.

    Always checks all 4 arches — parity is a release-level invariant,
    not subject to ``ALMA_ARCHES``.
    """
    s = http.session()
    major = runtime_config.version.split(".")[0]
    arches = _all_arches(major=major)
    per_arch = {
        arch: _find_pkg(s, runtime_config.source, runtime_config.version, arch, pkg_name)
        for arch in arches
    }
    missing = [a for a, p in per_arch.items() if p is None]
    if missing:
        # Distinguish "the entire release is missing" (every arch failed) from
        # "the package was dropped on some arches" (a real parity violation).
        # On a non-existent ALMA_VERSION (e.g. ``10.3`` before it ships) every
        # arch's BaseOS 404s and ``missing == arches`` — surface that as the
        # release-level problem it is, not as a per-package parity failure.
        if set(missing) == set(arches):
            where = (
                "pulp internal-beta"
                if runtime_config.source == "pulp"
                else "BaseOS"
            )
            pytest.fail(
                f"{where} not published for `{runtime_config.source}/"
                f"{runtime_config.version}` on any of {list(arches)} — "
                f"cannot check `{pkg_name}` parity. Likely cause: "
                f"`ALMA_VERSION={runtime_config.version}` does not exist yet."
            )
        pytest.fail(
            f"Package `{pkg_name}` is missing on {missing} "
            f"(present on {[a for a, p in per_arch.items() if p is not None]}) "
            f"in `{runtime_config.source}/{runtime_config.version}`."
        )

    nevrs = {arch: _nevr(p) for arch, p in per_arch.items()}
    distinct = set(nevrs.values())

    # Surface the actual NEVR found per arch — that's what the operator
    # really wants to see in a release report ("we shipped 10.2-1.el10
    # everywhere" is the headline, not "passed").
    for arch, (n, e, v, r) in nevrs.items():
        epoch = f"{e}:" if e and e != "0" else ""
        report_detail(f"`{arch}`: {n}-{epoch}{v}-{r}")

    assert len(distinct) == 1, f"N-E-V-R differ across arches: {nevrs}"


def test_almalinux_release_version_matches_input_version(runtime_config, report_detail):
    """The ``almalinux-release`` package's ``version`` field equals
    ``ALMA_VERSION`` (or matches by major for pungi major-only inputs).

    Single-arch lookup is fine — the cross-arch parity test above
    already ensures the same ``version`` ships on every arch, so
    checking any one arch is representative.
    """
    s = http.session()
    # Single-arch lookup is fine here — the previous test already
    # asserts cross-arch parity, so any arch is representative.
    major = runtime_config.version.split(".")[0]
    arch = _all_arches(major=major)[0]
    pkg = _find_pkg(s, runtime_config.source, runtime_config.version, arch, "almalinux-release")
    if pkg is None:
        where = (
            "pulp internal-beta"
            if runtime_config.source == "pulp"
            else "BaseOS"
        )
        pytest.fail(
            f"`almalinux-release` not found in `{runtime_config.source}/"
            f"{runtime_config.version}/{where}/{arch}` — likely cause: "
            f"`ALMA_VERSION={runtime_config.version}` does not exist yet "
            f"(or the {where} repo for this arch is unpublished)."
        )
    report_detail(
        f"found `almalinux-release` on `{arch}`: "
        f"version={pkg.version}, release={pkg.release}"
    )
    report_detail(f"`ALMA_VERSION` = {runtime_config.version}")
    # For stable 10.1 -> version=10.1; for pungi 10 -> version=10 (and then matches major)
    if "." in runtime_config.version:
        assert pkg.version == runtime_config.version, \
            f"almalinux-release.version={pkg.version} != ALMA_VERSION={runtime_config.version}"
    else:
        assert pkg.version.split(".")[0] == runtime_config.version
