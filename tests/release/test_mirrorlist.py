"""Tests for mirrorlist + baseurl as shipped in the ``almalinux-repos`` package.

We download the latest ``almalinux-repos`` RPM from BaseOS, parse the
``.repo`` files under ``/etc/yum.repos.d/``, and walk only the
**expected primary sections** for the current ``(major, arch)`` —
defined in :func:`post_check.config.expected_pkg_sections`. That list
encodes the per-major contract (AL9 has ResilientStorage, AL10
doesn't; ``rt``/``nfv`` are x86_64-only in both majors), so the
mirrorlist/baseurl tests validate exactly what real users will get on
a freshly-installed system, not whatever happens to be ``enabled=1``
in the package.

A separate test (``test_almalinux_repos_pkg_ships_all_expected_sections``
in ``test_almalinux_repos_pkg.py``) ensures every expected section is
actually shipped — if one is missing, *that* test fails first and the
URL probes here gracefully skip the missing entries with a clear
reason.

Verified per ``(arch, section)``:
  1. ``mirrorlist`` returns 200 + non-empty body.
  2. Every URL in the response contains the requested arch (guards
     against the bug where mirrorlist for x86_64 returns aarch64
     mirrors).
  3. Every URL contains the requested version: for beta —
     ``vault.almalinux.org/<version>-beta/...``; for stable — the
     bare ``<version>`` on a real public mirror (never ``vault``,
     never ``-beta``).
  4. ``baseurl`` directly serves ``repodata/repomd.xml`` (HEAD = 200).
  5. On the majority (≥2 of the first 3) of mirrors returned by the
     mirrorlist for the first expected section, ``repomd.xml`` is
     reachable.

stable and beta both ship a mirrorlist. beta's only ever returns
``vault.almalinux.org`` (beta isn't fanned out to community mirrors),
which is fine — the URL is reachable and serves real
``repodata/repomd.xml``.

Pungi: the entire module is skipped. Pungi has no mirrorlist service,
and the ``.repo`` files inside the ``almalinux-repos`` shipped with a
pungi compose still point at the public mirror layout
(``repo.almalinux.org`` / ``vault.almalinux.org``), not at the
per-arch pungi compose hostname — so probing those URLs from a pungi
run yields nothing useful. ``test_almalinux_repos_pkg.py`` skips its
own baseurl probe for pungi for the same reason.
"""

from __future__ import annotations

import re

import pytest

from post_check.config import (
    arch_uses_vault_only_mirrorlist,
    expected_pkg_sections,
)
from post_check.helpers import http, repo_file, rpm_extractor


# almalinux-repos ships ``# baseurl=...`` *commented out* alongside a
# live ``mirrorlist=``. configparser drops comments so they never reach
# RepoSection.baseurl; we extract them with a regex instead. The
# fallback URL is a documented contract: a user who can't reach
# mirrors.almalinux.org is expected to uncomment this line and have it
# work — so we check it does.
_COMMENTED_BASEURL = re.compile(
    r"^\s*#\s*baseurl\s*=\s*(\S+)\s*$", re.MULTILINE
)


def _extract_commented_baseurls(text: str) -> dict[str, str]:
    """Map ``section_id`` → commented-out ``# baseurl=`` URL.

    Walks the .repo file linearly so we can attribute each commented
    baseurl to whichever ``[section]`` header most recently preceded
    it. Sections without a commented baseurl don't appear in the
    returned dict.
    """
    out: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            continue
        if current is None:
            continue
        m = _COMMENTED_BASEURL.match(line)
        if m:
            out[current] = m.group(1)
    return out

pytestmark = [pytest.mark.online, pytest.mark.release]


# ---- helpers -----------------------------------------------------------------


def _repo_files(rpm_bytes: bytes) -> dict[str, str]:
    """Map ``./etc/yum.repos.d/<name>.repo → text``."""
    out: dict[str, str] = {}
    for f in rpm_extractor.list_files(rpm_bytes):
        if f.startswith("./etc/yum.repos.d/") and f.endswith(".repo"):
            out[f] = rpm_extractor.read_file(rpm_bytes, f).decode()
    return out


def _expected_sections(
    rpm_bytes: bytes, *, major: str, arch: str
) -> list[repo_file.RepoSection]:
    """Sections from the package's ``.repo`` files matching the expected
    list for ``(major, arch)``.

    Variants like ``-debug`` / ``-source`` are filtered out by exact
    section_id match — :func:`expected_pkg_sections` returns only the
    primary IDs (``baseos``, not ``baseos-debug``).

    Sections in the expected list but absent from the package are
    silently dropped: the per-section presence contract is enforced by
    ``test_almalinux_repos_pkg_ships_all_expected_sections``, so the
    URL probes here don't need to re-fail for the same reason.
    """
    expected = set(expected_pkg_sections(major, arch))
    by_id: dict[str, repo_file.RepoSection] = {}
    for _fname, text in _repo_files(rpm_bytes).items():
        for sec in repo_file.parse(text):
            if sec.section_id in expected:
                # First occurrence wins — sections are unique per ID
                # across the package, but defensive in case of a dup.
                by_id.setdefault(sec.section_id, sec)
    return list(by_id.values())


@pytest.fixture(autouse=True)
def _skip_for_non_mirrorlist_sources(request, runtime_config):
    """Skip module on sources where the mirrorlist tests are not meaningful.

    Two cases skipped here, both for the same reason — "we'd test
    something that isn't this source's contract":

    * **pungi** — no mirrorlist service, and the package's baseurls
      point at the public mirror layout, not at the per-arch pungi
      compose host the compose actually lives on, so probing those
      URLs from a pungi run yields nothing useful.

    * **pulp** — pulp's named repos reuse the major-aliased stable
      URLs verbatim. Running the mirrorlist contract here would just
      re-test the current GA stable (already covered by
      ``ALMA_SOURCE=stable`` runs). The layered internal-beta on
      ``build.almalinux.org/pulp/content/`` has no mirrorlist by
      design (single canonical pulp URL, not fanned out to mirrors).

    Limited to tests that depend on the ``almalinux_repos_pkg_bytes``
    fixture (i.e. the ones that hit the network). Pure config-contract
    tests below don't take it and run unconditionally on all sources.
    """
    if "almalinux_repos_pkg_bytes" not in request.fixturenames:
        return
    if runtime_config.source == "pungi":
        pytest.skip(
            "pungi: no mirrorlist service, and almalinux-repos baseurls "
            "point at the public layout, not at the pungi compose host"
        )
    if runtime_config.source == "pulp":
        pytest.skip(
            "pulp: named repos reuse stable URLs (covered by stable runs); "
            "internal-beta layer has no mirrorlist by design"
        )


# ---- tests -------------------------------------------------------------------


def test_mirrorlist_returns_200(
    runtime_config, arch, almalinux_repos_pkg_bytes, report_detail
):
    """For every expected section that ships a ``mirrorlist=`` URL,
    the substituted URL returns HTTP 200 with a non-empty body."""
    s = http.session()
    major = runtime_config.version.split(".")[0]
    releasever = major
    bad: list[str] = []
    seen_with_mirrorlist = False
    for sec in _expected_sections(almalinux_repos_pkg_bytes, major=major, arch=arch):
        if not sec.mirrorlist:
            continue
        seen_with_mirrorlist = True
        url = repo_file.substitute(
            sec.mirrorlist, basearch=arch, releasever=releasever
        )
        try:
            r = s.get(url, timeout=30)
        except Exception as e:
            bad.append(f"{sec.section_id}: GET {url} → {e!r}")
            report_detail(f"`[{sec.section_id}]` GET {url} → {e!r}")
            continue
        report_detail(
            f"`[{sec.section_id}]` GET {url} → {r.status_code} "
            f"({len(r.text)} bytes)"
        )
        if r.status_code != 200:
            bad.append(f"{sec.section_id}: GET {url} → {r.status_code}")
        elif not r.text.strip():
            bad.append(f"{sec.section_id}: GET {url} → empty body")
    if not seen_with_mirrorlist:
        pytest.skip(
            "no expected sections in almalinux-repos ship a mirrorlist= URL"
        )
    assert not bad, "Mirrorlist failures:\n" + "\n".join(bad)


def test_mirrorlist_returned_urls_contain_requested_arch(
    runtime_config, arch, almalinux_repos_pkg_bytes, report_detail
):
    """Every URL from every expected section's mirrorlist must mention the
    requested arch.

    Note: the mirrorlist service can legitimately return URLs that still
    contain the ``$basearch`` placeholder (dnf substitutes on the
    client side). We do the same substitution on our side before
    checking, so the test validates the *post-substitution* URL — the
    one a real client would actually fetch.
    """
    s = http.session()
    major = runtime_config.version.split(".")[0]
    releasever = major
    bad: list[str] = []
    seen = False
    for sec in _expected_sections(almalinux_repos_pkg_bytes, major=major, arch=arch):
        if not sec.mirrorlist:
            continue
        seen = True
        url = repo_file.substitute(
            sec.mirrorlist, basearch=arch, releasever=releasever
        )
        r = s.get(url, timeout=30)
        if r.status_code != 200:
            bad.append(f"{sec.section_id}: GET {url} → {r.status_code}")
            continue
        urls = [
            repo_file.substitute(line.strip(), basearch=arch, releasever=releasever)
            for line in r.text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not urls:
            bad.append(f"{sec.section_id}: mirrorlist body is empty")
            continue
        report_detail(
            f"`[{sec.section_id}]` mirrorlist returned {len(urls)} URLs; "
            f"first: {urls[0] if urls else '(none)'}"
        )
        for mirror in urls:
            if arch not in mirror:
                bad.append(
                    f"{sec.section_id}: URL {mirror} does not contain arch={arch}"
                )
    if not seen:
        pytest.skip("no sections ship a mirrorlist= URL")
    assert not bad, "\n".join(bad)


def test_mirrorlist_returned_urls_contain_requested_version(
    runtime_config, arch, almalinux_repos_pkg_bytes, report_detail
):
    """The returned URLs must reference the requested version.

    For beta we want ``vault.almalinux.org/<version>-beta/...`` (e.g.
    ``10.2-beta``) — beta is never fanned out to community mirrors.
    For stable we want the bare ``<version>`` (e.g. ``10.2``) on real
    public mirrors — never ``vault.almalinux.org`` and never the
    ``-beta`` suffix.
    """
    s = http.session()
    version = runtime_config.version
    major = version.split(".")[0]
    releasever = major
    bad: list[str] = []
    seen = False
    for sec in _expected_sections(almalinux_repos_pkg_bytes, major=major, arch=arch):
        if not sec.mirrorlist:
            continue
        seen = True
        url = repo_file.substitute(
            sec.mirrorlist, basearch=arch, releasever=releasever
        )
        r = s.get(url, timeout=30)
        if r.status_code != 200:
            bad.append(f"{sec.section_id}: GET {url} → {r.status_code}")
            continue
        urls = [
            repo_file.substitute(line.strip(), basearch=arch, releasever=releasever)
            for line in r.text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not urls:
            bad.append(f"{sec.section_id}: mirrorlist body is empty")
            continue
        # Surface every URL we actually saw — for a stable run that's
        # the community mirrors picked up by mirrorlist; for beta a
        # single vault URL. Either way, the operator can sanity-check
        # the list straight from the report.
        for mirror in urls:
            report_detail(f"`[{sec.section_id}]` returned mirror: {mirror}")
        if runtime_config.source == "beta":
            expected_segment = f"{version}-beta"
            for mirror in urls:
                if "vault.almalinux.org" not in mirror:
                    bad.append(
                        f"{sec.section_id}: beta must return vault.almalinux.org "
                        f"URLs, got: {mirror}"
                    )
                if expected_segment not in mirror:
                    bad.append(
                        f"{sec.section_id}: beta URL {mirror} missing version "
                        f"segment {expected_segment!r}"
                    )
        elif runtime_config.source == "stable":
            # Some stable arches are vault-only (i686): the mirrorlist
            # legitimately returns a single ``vault.almalinux.org`` URL
            # with no community-mirror fan-out. We skip the
            # "stable must not return vault" / "must not contain -beta"
            # checks for those arches and just verify the version segment
            # is present.
            vault_only = arch_uses_vault_only_mirrorlist(arch)
            for mirror in urls:
                if not vault_only and "vault.almalinux.org" in mirror:
                    bad.append(
                        f"{sec.section_id}: stable must not return vault, got: {mirror}"
                    )
                if not vault_only and f"{version}-beta" in mirror:
                    bad.append(
                        f"{sec.section_id}: stable URL {mirror} contains '{version}-beta'"
                    )
                if version not in mirror:
                    bad.append(
                        f"{sec.section_id}: stable URL {mirror} missing version {version!r}"
                    )
    if not seen:
        pytest.skip("no sections ship a mirrorlist= URL")
    assert not bad, "\n".join(bad)


def test_baseurl_serves_repomd_for_every_section(
    runtime_config, arch, almalinux_repos_pkg_bytes, report_detail
):
    """For every expected section, **every** ``baseurl=`` URL the
    package carries — both live and commented-out as a documented
    fallback — directly serves ``repodata/repomd.xml`` (HEAD = 200).

    Why both, not "one or the other":

    * **live** ``baseurl=`` is what dnf reads on a default install
      (currently the package ships none, but that may change);
    * **commented** ``# baseurl=`` is the published escape hatch:
      a user behind a firewall / with broken DNS can uncomment it and
      hit the canonical upstream URL. If that URL silently rotted
      after a re-host, the escape hatch is broken and nobody notices
      until somebody actually tries it. We verify it so the contract
      stays honest.

    Both are checked independently — when both are present on the
    same section, both must respond 200. The report tags each entry
    so the operator sees which kind passed/failed.
    """
    s = http.session()
    major = runtime_config.version.split(".")[0]
    releasever = major
    expected = set(expected_pkg_sections(major, arch))

    # ``(section_id, kind, url)`` triples — one per URL to probe.
    # Live and commented are independent entries: even if both exist
    # on the same section, both get HEAD-tested.
    checks: list[tuple[str, str, str]] = []
    for _fname, text in _repo_files(almalinux_repos_pkg_bytes).items():
        commented = _extract_commented_baseurls(text)
        for sec in repo_file.parse(text):
            if sec.section_id not in expected:
                continue
            if sec.baseurl:
                checks.append((sec.section_id, "live", sec.baseurl))
            if sec.section_id in commented:
                checks.append(
                    (sec.section_id, "commented fallback", commented[sec.section_id])
                )

    if not checks:
        pytest.skip(
            "no expected section in the package has a baseurl "
            "(neither live nor as a commented fallback)"
        )

    bad: list[str] = []
    for sec_id, kind, baseurl in checks:
        url = repo_file.substitute(baseurl, basearch=arch, releasever=releasever)
        head_url = url.rstrip("/") + "/repodata/repomd.xml"
        try:
            r = s.head(head_url, timeout=15, allow_redirects=True)
        except Exception as e:
            bad.append(f"{sec_id} ({kind}): HEAD {head_url} → {e!r}")
            report_detail(f"`[{sec_id}]` ({kind}) HEAD {head_url} → {e!r}")
            continue
        report_detail(f"`[{sec_id}]` ({kind}) HEAD {head_url} → {r.status_code}")
        if r.status_code != 200:
            bad.append(f"{sec_id} ({kind}): HEAD {head_url} → {r.status_code}")
    assert not bad, "Unreachable baseurls:\n" + "\n".join(bad)


def test_mirrorlist_each_returned_url_has_repomd_xml_reachable(
    runtime_config, almalinux_repos_pkg_bytes, report_detail
):
    """For **every** expected section that ships a mirrorlist (under
    the first arch in ``ALMA_ARCHES``), the majority of returned
    mirrors actually serve ``repodata/repomd.xml``.

    Per-section threshold:

    * For stable, the mirrorlist returns many community mirrors —
      we sample the first 3 and require ≥2 to be live (one mirror
      down for routine maintenance shouldn't fail the suite).
    * For beta, the mirrorlist returns only ``vault.almalinux.org``
      — a single canonical URL — so the "≥2 of 3" rule is meaningless
      there; we drop to "≥1 of N" when N < 3.

    Why every section, not just the first: a community mirror may
    legitimately not host every repo (e.g. mirror BaseOS/AppStream
    but skip SAPHANA). Probing only ``[baseos]`` would miss the
    case where ``[saphana]``'s mirrorlist hands back URLs that
    no mirror actually serves — yet a user trying ``dnf install``
    from SAPHANA would hit it immediately.
    """
    arch = runtime_config.arches[0]
    major = runtime_config.version.split(".")[0]
    releasever = major
    sections = [
        sec
        for sec in _expected_sections(
            almalinux_repos_pkg_bytes, major=major, arch=arch
        )
        if sec.mirrorlist
    ]
    if not sections:
        pytest.skip("no expected sections ship a mirrorlist= URL")

    s = http.session()
    bad: list[str] = []
    for section in sections:
        url = repo_file.substitute(
            section.mirrorlist, basearch=arch, releasever=releasever
        )
        r = s.get(url, timeout=30)
        if r.status_code != 200:
            bad.append(
                f"{section.section_id}: GET {url} → {r.status_code} "
                f"(can't sample mirrors)"
            )
            report_detail(
                f"`[{section.section_id}]` GET {url} → {r.status_code}"
            )
            continue
        urls = [
            repo_file.substitute(line.strip(), basearch=arch, releasever=releasever)
            for line in r.text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ][:3]
        if not urls:
            bad.append(f"{section.section_id}: mirrorlist body is empty")
            continue
        report_detail(
            f"`[{section.section_id}]` sampling first {len(urls)} mirror(s) on {arch}"
        )
        reachable = 0
        for mirror in urls:
            repomd = mirror.rstrip("/") + "/repodata/repomd.xml"
            try:
                resp = s.head(repomd, timeout=10, allow_redirects=True)
                status = resp.status_code
            except Exception as e:
                report_detail(
                    f"`[{section.section_id}]` HEAD {repomd} → ERROR ({e!r})"
                )
                continue
            report_detail(f"`[{section.section_id}]` HEAD {repomd} → {status}")
            if status == 200:
                reachable += 1
        # ≥2 when fan-out exists (stable), ≥1 when single canonical URL (beta).
        required = 2 if len(urls) >= 3 else 1
        if reachable < required:
            bad.append(
                f"{section.section_id}: out of {len(urls)} mirrors, "
                f"repomd.xml is reachable on only {reachable} "
                f"(required: {required}): {urls}"
            )
    assert not bad, "Mirror-reachability failures:\n" + "\n".join(bad)


# Mirrorlist URLs aren't kept in any local registry — they're
# authoritative only inside the ``almalinux-repos`` package's ``.repo``
# files, which the tests above read directly. So there's nothing to
# unit-test here offline; the source-registry contract used to live
# in this file but was removed alongside ``RepoURL.mirrorlist_url``.
