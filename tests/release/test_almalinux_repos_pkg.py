"""Online checks of the contents of the ``almalinux-repos`` package.

Downloads the ``almalinux-repos`` package **of the version under
test** (plus the companion ``almalinux-release`` /
``almalinux-gpg-keys`` for cross-checks), extracts them in-memory via
:mod:`post_check.helpers.rpm_extractor`, and validates every ``.repo``
file under ``/etc/yum.repos.d/`` shipped by the package:

* the package actually drops at least one ``.repo`` file;
* every section has ``gpgcheck=1``;
* nowhere is ``sslverify=0`` set;
* every ``baseurl`` of an enabled section actually responds to
  ``repodata/repomd.xml`` (only for stable/beta — pungi composes ship
  ``.repo`` files that point at the public mirror layout, not at the
  per-arch pungi hostnames the compose itself lives on, so the HEAD
  probe would always fail; this single test skips for pungi. On pulp
  the upgrade-target package uses mirrorlists rather than baseurls,
  so the loop body finds no ``baseurl=`` to probe — vacuous pass; the
  mirrorlist contract for pulp is the job of ``test_mirrorlist``);
* every ``gpgkey=file://...`` references a file that is physically shipped
  by ``almalinux-release`` or ``almalinux-gpg-keys`` (in AlmaLinux 10 the
  keys were split into a separate package).

Per-source where the package is downloaded from:

* stable / beta / pungi — BaseOS of the version-under-test (the
  package shipped there *is* the artefact under test).
* pulp — the layered internal-beta repo on
  ``build.almalinux.org/pulp/content/...``, picking the highest
  E-V-R available there. The major-aliased stable URL would carry
  the current GA's package (e.g. ``almalinux-repos-10.1-...``)
  rather than the upgrade target (``...-10.2-...``); validating
  the wrong artefact would silently turn a real "we forgot to
  publish updated ``.repo`` files for 10.2" regression into a
  green report.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from post_check.config import expected_pkg_sections
from post_check.helpers import http, repo_file, rpm_extractor

# The version-under-test package fixtures (target_repos_pkg_bytes,
# target_release_pkg_bytes, target_gpg_keys_pkg_bytes) live in
# tests/conftest.py.

pytestmark = [pytest.mark.online, pytest.mark.release]


def _repo_files(rpm_bytes: bytes) -> dict[str, str]:
    """Return mapping ``./etc/yum.repos.d/<name>.repo → text``."""
    out: dict[str, str] = {}
    for f in rpm_extractor.list_files(rpm_bytes):
        if f.startswith("./etc/yum.repos.d/") and f.endswith(".repo"):
            out[f] = rpm_extractor.read_file(rpm_bytes, f).decode()
    return out


def test_almalinux_repos_pkg_writes_to_etc_yum_repos_d(
    runtime_config, target_repos_pkg_bytes, arch, report_detail
):
    """The ``almalinux-repos`` package ships a ``.repo``-file
    ``[section]`` for **every primary repo** expected on the current
    ``(major, arch)`` combination.

    Source of truth for "expected" is
    :func:`post_check.config.expected_pkg_sections`:
      * AL9 → 10 sections (incl. ``resilientstorage``); on non-x86 →
        8 (no ``rt``/``nfv``).
      * AL10 → 9 sections (no ``resilientstorage``); on non-x86 → 7.

    A failure here means the package is missing a primary repo a real
    user would expect on a fresh install. Failing here surfaces the
    drift more loudly than letting the URL probes 404 downstream.
    """
    major = runtime_config.version.split(".")[0]
    expected = set(expected_pkg_sections(major, arch))
    found: dict[str, list[str]] = {}  # section_id -> [filenames containing it]
    for fname, text in _repo_files(target_repos_pkg_bytes).items():
        for sec in repo_file.parse(text):
            found.setdefault(sec.section_id, []).append(Path(fname).name)
    missing = expected - set(found)

    # Surface the concrete .repo files seen plus the per-expected-section
    # file mapping so the report shows what was actually validated, not
    # just "passed".
    repo_files = sorted(Path(fn).name for fn in _repo_files(target_repos_pkg_bytes))
    report_detail(f"`.repo` files in package: {repo_files}")
    for sec_id in sorted(expected):
        if sec_id in found:
            report_detail(
                f"`[{sec_id}]` ✓ found in {found[sec_id]}"
            )
        else:
            report_detail(f"`[{sec_id}]` ✗ MISSING")

    assert not missing, (
        f"almalinux-repos missing expected sections for major={major}, "
        f"arch={arch}: {sorted(missing)}\n"
        f"sections actually shipped: {sorted(found)}"
    )


def test_almalinux_repos_pkg_gpgcheck_is_1_for_every_repo(
    target_repos_pkg_bytes, report_detail
):
    """Every ``[section]`` in every ``.repo`` file shipped by the
    package has ``gpgcheck=1``.

    This is the security contract: a fresh AlmaLinux install must
    refuse to install unsigned RPMs by default. A regression here
    would let users silently consume unsigned packages from any
    repo configured by ``almalinux-repos``.
    """
    bad: list[str] = []
    sections_checked = 0
    for fname, text in _repo_files(target_repos_pkg_bytes).items():
        for sec in repo_file.parse(text):
            sections_checked += 1
            if not sec.gpgcheck:
                bad.append(f"{fname}::{sec.section_id}: gpgcheck=0")
    report_detail(f"checked `gpgcheck=1` on {sections_checked} sections")
    assert not bad, "\n".join(bad)


def test_almalinux_repos_pkg_no_repo_with_sslverify_disabled(
    target_repos_pkg_bytes, report_detail
):
    """No section anywhere in the shipped ``.repo`` files sets
    ``sslverify=0``.

    Companion to the ``gpgcheck`` test above: dropping certificate
    validation would silently downgrade transport security and we
    never want it in a published package.
    """
    bad: list[str] = []
    sections_checked = 0
    for fname, text in _repo_files(target_repos_pkg_bytes).items():
        for sec in repo_file.parse(text):
            sections_checked += 1
            if not sec.sslverify:
                bad.append(f"{fname}::{sec.section_id}")
    report_detail(f"checked `sslverify` on {sections_checked} sections (none disabled)")
    assert not bad, f"sslverify=0 in: {bad}"


def test_almalinux_repos_pkg_each_baseurl_returns_repomd(
    runtime_config, target_repos_pkg_bytes, arch, report_detail
):
    """For every enabled section that ships a ``baseurl=``, the
    substituted URL serves ``repodata/repomd.xml`` (HEAD = 200).

    Catches the case where the package references a repo that
    doesn't physically exist for this ``(release, arch)`` — users
    would hit a 404 and ``dnf`` would abort the transaction.

    Skipped for pungi: the package's ``.repo`` files in pungi
    composes point at the public mirror layout, not at the per-arch
    pungi compose host where the compose actually lives — so the
    HEAD probe would always 404 there for legitimate reasons.

    Skipped for pulp: the package's baseurls point at the public
    stable mirror at the major-only alias (e.g. ``…/almalinux/10/…``),
    while the target-minor packages a pulp run actually validates
    come from the layered internal-beta repo on
    ``build.almalinux.org/pulp/content/...``. Probing the package's
    own baseurls is orthogonal to what pulp is shipping — the
    internal-beta layer is what matters, and it has its own
    coverage.
    """
    if runtime_config.source == "pungi":
        # The .repo files in the package point at the public mirror
        # layout (repo.almalinux.org / vault), not at the per-arch
        # pungi hostnames the compose itself lives on. Probing those
        # baseurls from a pungi run is meaningless.
        pytest.skip("pungi: package baseurls are not the pungi compose URLs")
    if runtime_config.source == "pulp":
        # Same shape of mismatch as pungi: the package baseurls are
        # the public stable URLs at the major alias, not the
        # internal-beta URLs that carry the target-minor packages.
        pytest.skip(
            "pulp: package baseurls are the stable-major URLs, not the "
            "internal-beta layer that pulp actually validates"
        )
    s = http.session()
    bad: list[str] = []
    releasever = runtime_config.version.split(".")[0]
    for fname, text in _repo_files(target_repos_pkg_bytes).items():
        for sec in repo_file.parse(text):
            if not sec.enabled:
                continue
            if not sec.baseurl:
                # mirrorlist-only sections are checked by a separate test
                continue
            url = repo_file.substitute(
                sec.baseurl, basearch=arch, releasever=releasever
            )
            head_url = url.rstrip("/") + "/repodata/repomd.xml"
            r = s.head(head_url, timeout=15, allow_redirects=True)
            report_detail(f"HEAD {head_url} → {r.status_code}")
            if r.status_code != 200:
                bad.append(f"{sec.section_id}: {head_url} → {r.status_code}")
    assert not bad, "Some baseurls are unreachable:\n" + "\n".join(bad)


def test_almalinux_repos_pkg_each_gpgkey_path_exists_in_release_pkg(
    target_repos_pkg_bytes,
    target_release_pkg_bytes,
    target_gpg_keys_pkg_bytes,
):
    """Cross-check: every ``gpgkey=file:///etc/pki/rpm-gpg/X`` must be
    physically shipped by ``almalinux-release`` or ``almalinux-gpg-keys``
    (in AL10 the keys were moved into a separate package).
    """
    shipped: set[str] = set(rpm_extractor.list_files(target_release_pkg_bytes))
    if target_gpg_keys_pkg_bytes is not None:
        shipped |= set(rpm_extractor.list_files(target_gpg_keys_pkg_bytes))
    bad: list[str] = []
    for _fname, text in _repo_files(target_repos_pkg_bytes).items():
        for sec in repo_file.parse(text):
            if not sec.gpgkey:
                continue
            if not sec.gpgkey.startswith("file://"):
                continue
            path = sec.gpgkey[len("file://") :]
            # rpmfile stores paths with a "./" prefix, not "/"
            expected = "." + path
            if expected not in shipped:
                bad.append(
                    f"{sec.section_id}: gpgkey={path} not found in either "
                    "almalinux-release or almalinux-gpg-keys"
                )
    assert not bad, "\n".join(bad)
