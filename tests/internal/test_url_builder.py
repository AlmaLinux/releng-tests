"""Unit tests for post_check.helpers.url_builder.RepoURL.

Covers the stable, beta and pungi sources. RepoURL is a pure builder with
no I/O, so the tests run without network/disk too.
"""

from __future__ import annotations

import pytest

from post_check.helpers.url_builder import RepoURL, pulp_internal_beta_repo_base


def test_url_builder_stable_baseos_x86_64():
    u = RepoURL.from_config(
        source="stable", version="10.1", arch="x86_64", repo="BaseOS"
    )
    assert (
        u.repomd_xml()
        == "https://repo.almalinux.org/almalinux/10.1/BaseOS/x86_64/os/repodata/repomd.xml"
    )


def test_url_builder_stable_i686_routes_to_vault():
    """i686 is never published on repo.almalinux.org — its stable
    URLs must point at vault.almalinux.org instead, AND vault uses
    a flatter layout (no ``/almalinux/`` prefix:
    ``vault.almalinux.org/9.5/BaseOS/i686/os``, not
    ``…/almalinux/9.5/…``).

    Without this routing, the parity test would silently 404-skip
    the i686 drift on stable runs (the conftest safety-net treats
    404 as "not published yet, skip"), masking real release bugs
    of the kind we already see in beta-10.2 and pungi-10
    (gnome-shell-extensions-49.0-3.el10 on i686 vs -2.el10 on
    every other arch).
    """
    u = RepoURL.from_config(
        source="stable", version="9.5", arch="i686", repo="BaseOS"
    )
    assert u.repo_base() == (
        "https://vault.almalinux.org/9.5/BaseOS/i686/os"
    )
    assert u.iso_dir() == "https://vault.almalinux.org/9.5/isos/i686"


def test_url_builder_stable_default_arches_keep_repo_host():
    """Sanity: arches without ``stable_repo_host`` keep going to
    ``repo.almalinux.org`` (the historic default)."""
    for arch in ("x86_64", "aarch64", "s390x", "ppc64le", "x86_64_v2"):
        u = RepoURL.from_config(
            source="stable", version="10.1", arch=arch, repo="BaseOS"
        )
        assert u.repo_base().startswith("https://repo.almalinux.org/"), (
            f"{arch}: stable URL must stay on repo.almalinux.org, "
            f"got {u.repo_base()}"
        )


def test_url_builder_stable_repomd_xml_asc():
    u = RepoURL.from_config(
        source="stable", version="10.1", arch="x86_64", repo="BaseOS"
    )
    assert (
        u.repomd_xml_asc()
        == "https://repo.almalinux.org/almalinux/10.1/BaseOS/x86_64/os/repodata/repomd.xml.asc"
    )


def test_url_builder_substitutes_major_minor_version_for_stable():
    u = RepoURL.from_config(
        source="stable", version="9.6", arch="aarch64", repo="AppStream"
    )
    assert "9.6" in u.repomd_xml()
    assert u.major() == "9"


def test_url_builder_repo_base_no_trailing_slash():
    u = RepoURL.from_config(
        source="stable", version="10.1", arch="x86_64", repo="BaseOS"
    )
    assert u.repo_base() == (
        "https://repo.almalinux.org/almalinux/10.1/BaseOS/x86_64/os"
    )
    assert not u.repo_base().endswith("/")


def test_url_builder_raises_on_unknown_source():
    with pytest.raises(ValueError, match="unknown source"):
        RepoURL.from_config(
            source="foo", version="10.1", arch="x86_64", repo="BaseOS"
        )


def test_url_builder_raises_on_missing_version():
    with pytest.raises(ValueError, match="version"):
        RepoURL.from_config(source="stable", version="", arch="x86_64", repo="BaseOS")


def test_url_builder_raises_on_unknown_arch():
    with pytest.raises(ValueError, match="unknown arch"):
        RepoURL.from_config(
            source="stable", version="10.1", arch="riscv64", repo="BaseOS"
        )


def test_url_builder_raises_on_major_only_version_for_stable():
    # stable requires major.minor, "10" must be rejected
    with pytest.raises(ValueError, match="major.minor"):
        RepoURL.from_config(
            source="stable", version="10", arch="x86_64", repo="BaseOS"
        )


def test_url_builder_stable_iso_dir():
    u = RepoURL.from_config(
        source="stable", version="10.1", arch="x86_64", repo="BaseOS"
    )
    assert u.iso_dir() == "https://repo.almalinux.org/almalinux/10.1/isos/x86_64"


def test_url_builder_is_frozen_dataclass():
    u = RepoURL.from_config(
        source="stable", version="10.1", arch="x86_64", repo="BaseOS"
    )
    with pytest.raises(Exception):
        u.version = "10.2"  # type: ignore[misc]


# ---------------------------------------------------------------- pungi
def test_url_builder_pungi_arch_dash_substitution_x86_64():
    u = RepoURL.from_config(
        source="pungi", version="10", arch="x86_64", repo="BaseOS"
    )
    # The hostname uses pungi_host (x86-64), while path segments — the actual arch (x86_64)
    assert "x86-64-pungi-10.almalinux.dev" in u.repomd_xml()
    assert "/almalinux/10/x86_64/" in u.repomd_xml()


def test_url_builder_pungi_aarch64_keeps_underscores():
    u = RepoURL.from_config(
        source="pungi", version="10", arch="aarch64", repo="BaseOS"
    )
    assert "aarch64-pungi-10.almalinux.dev" in u.repomd_xml()


def test_url_builder_pungi_uses_major_only():
    u = RepoURL.from_config(
        source="pungi", version="10", arch="x86_64", repo="BaseOS"
    )
    url = u.repomd_xml()
    assert "/10/" in url
    assert "/10.1/" not in url


def test_url_builder_pungi_uses_compose_subpath():
    u = RepoURL.from_config(
        source="pungi", version="10", arch="x86_64", repo="BaseOS"
    )
    assert "/compose/BaseOS/" in u.repomd_xml()


def test_url_builder_pungi_kitten_uses_alt_result_dir():
    u = RepoURL.from_config(
        source="pungi", version="10", arch="x86_64", repo="BaseOS", kitten=True
    )
    assert "latest_result_almalinux-kitten" in u.repomd_xml()


def test_url_builder_pungi_default_uses_default_result_dir():
    u = RepoURL.from_config(
        source="pungi", version="10", arch="x86_64", repo="BaseOS"
    )
    url = u.repomd_xml()
    assert "latest_result_almalinux/" in url
    assert "latest_result_almalinux-kitten" not in url


def test_url_builder_pungi_strips_minor_version():
    """pungi only has a major; pass-through of a major.minor must be normalised
    to the major so callers can reuse runtime_config.version unchanged."""
    u = RepoURL.from_config(
        source="pungi", version="10.1", arch="x86_64", repo="BaseOS"
    )
    assert u.version == "10"
    assert u.major() == "10"
    assert "/almalinux/10/x86_64/" in u.repomd_xml()


def test_url_builder_pungi_iso_dir():
    u = RepoURL.from_config(
        source="pungi", version="10", arch="x86_64", repo="BaseOS"
    )
    assert u.iso_dir() == (
        "https://x86-64-pungi-10.almalinux.dev/almalinux/10/x86_64/"
        "latest_result_almalinux/compose/isos/x86_64"
    )


# ---------------------------------------------------------------- beta
def test_url_builder_beta_uses_vault_with_beta_suffix():
    u = RepoURL.from_config(
        source="beta", version="10.2", arch="x86_64", repo="BaseOS"
    )
    assert "vault.almalinux.org/10.2-beta" in u.repomd_xml()


def test_url_builder_beta_baseos_x86_64_full_url():
    u = RepoURL.from_config(
        source="beta", version="10.2", arch="x86_64", repo="BaseOS"
    )
    assert (
        u.repomd_xml()
        == "https://vault.almalinux.org/10.2-beta/BaseOS/x86_64/os/repodata/repomd.xml"
    )


def test_url_builder_beta_iso_dir():
    u = RepoURL.from_config(
        source="beta", version="10.2", arch="aarch64", repo="BaseOS"
    )
    assert u.iso_dir() == "https://vault.almalinux.org/10.2-beta/isos/aarch64"


def test_url_builder_raises_on_beta_with_major_only():
    with pytest.raises(ValueError, match="major.minor"):
        RepoURL.from_config(
            source="beta", version="10", arch="x86_64", repo="BaseOS"
        )


# ---------------------------------------------------------------- pulp
def test_url_builder_pulp_uses_major_only_stable_alias():
    """pulp's named-repo URLs use the **major-only** stable alias
    (``…/almalinux/10/…``), not the per-minor stable URL.

    Reason: ``ALMA_VERSION`` for pulp is the *upgrade target* (what
    should land in ``/etc/os-release``), not the base — the base is
    the current GA in that major, which the major alias on
    ``repo.almalinux.org`` resolves to. See the ``url_builder`` module
    docstring for the full rationale.
    """
    u = RepoURL.from_config(
        source="pulp", version="10.2", arch="x86_64", repo="BaseOS"
    )
    assert u.repo_base() == (
        "https://repo.almalinux.org/almalinux/10/BaseOS/x86_64/os"
    )
    assert u.repomd_xml() == (
        "https://repo.almalinux.org/almalinux/10/BaseOS/x86_64/os/repodata/repomd.xml"
    )


def test_url_builder_pulp_strips_minor_version():
    """pulp accepts ``major.minor`` (so ``ALMA_VERSION=10.2`` round-trips
    untouched as ``runtime_config.version``) but drops the minor inside
    :class:`RepoURL` — same convention as pungi.
    """
    u = RepoURL.from_config(
        source="pulp", version="10.2", arch="x86_64", repo="BaseOS"
    )
    assert u.version == "10"
    assert u.major() == "10"
    assert "/almalinux/10/" in u.repo_base()
    assert "/10.2/" not in u.repo_base()


def test_url_builder_pulp_accepts_major_only():
    """``ALMA_VERSION=10`` (no minor) is also accepted — pulp must not
    require a minor when the operator already passed major-only.
    """
    u = RepoURL.from_config(
        source="pulp", version="10", arch="x86_64", repo="BaseOS"
    )
    assert "/almalinux/10/" in u.repo_base()


# ---------------------------------------------------------------- common invariants
def test_url_builder_raises_on_stable_with_major_only():
    with pytest.raises(ValueError, match="major.minor"):
        RepoURL.from_config(
            source="stable", version="10", arch="x86_64", repo="BaseOS"
        )


def test_url_builder_baseurl_does_not_double_slash():
    cases = [("stable", "10.1"), ("beta", "10.2"), ("pungi", "10"), ("pulp", "10.2")]
    for src, ver in cases:
        u = RepoURL.from_config(
            source=src, version=ver, arch="x86_64", repo="BaseOS"
        )
        url_without_scheme = u.repomd_xml().replace("https://", "")
        assert "//" not in url_without_scheme, (
            f"double-slash in {src} URL: {u.repomd_xml()}"
        )


def test_url_builder_baseurl_no_trailing_slash():
    for src, ver in [("stable", "10.1"), ("beta", "10.2"), ("pungi", "10"), ("pulp", "10.2")]:
        u = RepoURL.from_config(
            source=src, version=ver, arch="x86_64", repo="BaseOS"
        )
        assert not u.repo_base().endswith("/"), (
            f"{src}: repo_base must have no trailing slash"
        )


def test_url_builder_iso_dir_x86_64():
    u = RepoURL.from_config(
        source="stable", version="10.1", arch="x86_64", repo="BaseOS"
    )
    assert u.iso_dir() == "https://repo.almalinux.org/almalinux/10.1/isos/x86_64"


def test_url_builder_kitten_ignored_for_non_pungi_sources():
    # kitten=True for stable should be silently ignored — kitten only
    # makes sense for the pungi result_dir.
    u = RepoURL.from_config(
        source="stable", version="10.1", arch="x86_64", repo="BaseOS", kitten=True
    )
    assert "kitten" not in u.repomd_xml()
    u_beta = RepoURL.from_config(
        source="beta", version="10.2", arch="x86_64", repo="BaseOS", kitten=True
    )
    assert "kitten" not in u_beta.repomd_xml()


# --- pulp_internal_beta_repo_base -----------------------------------------
#
# This URL is what the cross-arch parity test uses on ``ALMA_SOURCE=pulp``.
# Going through ``RepoURL`` for pulp would land on the major-aliased stable
# URL (current GA), which both duplicates stable runs and breaks the
# ``version == ALMA_VERSION`` invariant by construction. See the module
# docstring of ``tests/release/test_release_parity.py`` for the full reasoning.


def test_pulp_internal_beta_repo_base_strips_minor():
    """``ALMA_VERSION`` is the upgrade target (``10.2``); the URL
    template is keyed by major (``10``). Passing the full minor version
    must still resolve to the major key — same contract as
    ``RepoURL`` for the ``pulp`` source.
    """
    u = pulp_internal_beta_repo_base(version="10.2", arch="x86_64")
    assert u == (
        "https://build.almalinux.org/pulp/content/copr/"
        "eabdullin1-almalinux10-beta-almalinux-10-x86_64-dr"
    )


def test_pulp_internal_beta_repo_base_per_arch():
    """``{arch}`` placeholder is substituted, no leftover braces."""
    for arch in ("x86_64", "aarch64", "ppc64le", "s390x"):
        u = pulp_internal_beta_repo_base(version="10", arch=arch)
        assert "{arch}" not in u
        assert u.endswith(f"-{arch}-dr")


def test_pulp_internal_beta_repo_base_no_trailing_slash():
    """Must match :meth:`RepoURL.repo_base` convention so callers can
    interpolate ``/repodata/repomd.xml`` uniformly across sources."""
    u = pulp_internal_beta_repo_base(version="10.2", arch="x86_64")
    assert not u.endswith("/")


def test_pulp_internal_beta_repo_base_unknown_major_raises():
    with pytest.raises(ValueError, match="not defined for AlmaLinux 7"):
        pulp_internal_beta_repo_base(version="7", arch="x86_64")
