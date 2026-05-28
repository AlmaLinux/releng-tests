"""Tests for ``post_check.helpers.repo_inject``.

Two paths under test:

* ``extract_repo_files`` — for stable/beta. Returns the package's
  own ``.repo`` files **unmodified** (placeholders preserved for
  dnf to expand inside the container).
* ``render_pungi_repo_files`` — for pungi. The only place we
  generate URLs ourselves: a pungi compose's package references
  the public layout (where the compose hasn't been published yet),
  so we synthesise URLs to the per-arch compose host.

``prepare_repo_files_dir`` is the dispatcher every docker test goes
through; tests below pin its source-based dispatch contract.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from post_check.helpers.repo_inject import (
    build_install_command,
    dnf_repo_flags,
    extract_repo_files,
    prepare_repo_files_dir,
    render_pulp_internal_beta_repo_file,
    render_pungi_repo_files,
)


# Lightweight stand-in for RuntimeConfig — enough for the dispatcher
# to read .source and .version. The real dataclass is frozen and
# requires more fields than these tests need.
@dataclass
class _RC:
    source: str
    version: str


# ============================================================ extract_repo_files


def test_extract_repo_files_returns_only_etc_yum_repos_d(synthetic_repos_rpm):
    files = extract_repo_files(synthetic_repos_rpm)
    assert files, "synthetic RPM ships at least one .repo"
    for name in files:
        assert "/" not in name, f"{name!r} should be a basename, not a path"
        assert name.endswith(".repo")


def test_extract_repo_files_preserves_placeholders(synthetic_repos_rpm):
    """``$releasever`` / ``$basearch`` are left intact — they must
    reach dnf inside the container, which substitutes per-(major,
    arch). Substituting on our side would hide drift between
    expected and actual URLs in the package.
    """
    files = extract_repo_files(synthetic_repos_rpm)
    text = b"\n".join(files.values()).decode()
    assert "$releasever" in text, (
        f"$releasever was substituted during extraction:\n{text}"
    )
    assert "$basearch" in text, (
        f"$basearch was substituted during extraction:\n{text}"
    )


# ============================================================ render_pungi_repo_files


def test_render_pungi_includes_only_existing_repos_for_al10():
    """AL10 has no ResilientStorage. The synthesised .repo for AL10
    must omit it, otherwise dnf inside the pungi container 404s on
    metadata fetch and aborts the whole transaction.
    """
    files = render_pungi_repo_files(version="10", arch="x86_64")
    text = b"\n".join(files.values()).decode()
    assert "[baseos]" in text
    assert "[appstream]" in text
    assert "[resilientstorage]" not in text
    assert "ResilientStorage" not in text


def test_render_pungi_includes_resilientstorage_for_al9():
    """AL9 still ships ResilientStorage on every arch — the
    synthesised .repo for AL9 must include it."""
    files = render_pungi_repo_files(version="9", arch="aarch64")
    text = b"\n".join(files.values()).decode()
    assert "[resilientstorage]" in text


def test_render_pungi_omits_rt_and_nfv_on_non_x86():
    """RT/NFV are x86_64-only across both majors — the synthesised
    .repo for non-x86 arches must skip them so dnf doesn't 404."""
    text = b"\n".join(render_pungi_repo_files(version="10", arch="aarch64").values()).decode()
    assert "[rt]" not in text
    assert "[nfv]" not in text


def test_render_pungi_includes_rt_and_nfv_on_x86():
    text = b"\n".join(render_pungi_repo_files(version="10", arch="x86_64").values()).decode()
    assert "[rt]" in text
    assert "[nfv]" in text


def test_render_pungi_baseurl_points_at_pungi_compose_host():
    """The whole point of this code path: URLs reach the per-arch
    pungi compose host, **not** the public mirror layout."""
    text = b"\n".join(render_pungi_repo_files(version="10", arch="x86_64").values()).decode()
    assert "x86-64-pungi-10.almalinux.dev" in text
    assert "repo.almalinux.org" not in text
    assert "vault.almalinux.org" not in text


def test_render_pungi_pins_gpgcheck_on():
    text = b"\n".join(render_pungi_repo_files(version="10", arch="x86_64").values()).decode()
    assert "gpgcheck=0" not in text
    assert "gpgcheck=1" in text


# ============================================================ render_pulp_internal_beta_repo_file


def test_render_pulp_internal_beta_al10_x86_64_uses_copr_template():
    """AL10's pulp internal beta lives under ``copr/eabdullin1-…``;
    the synthesised .repo must point dnf there, with the requested arch
    embedded in the URL.
    """
    files = render_pulp_internal_beta_repo_file(version="10.1", arch="x86_64")
    text = b"\n".join(files.values()).decode()
    assert (
        "https://build.almalinux.org/pulp/content/copr/"
        "eabdullin1-almalinux10-beta-almalinux-10-x86_64-dr/"
    ) in text
    assert "[pulp-internal-beta]" in text


def test_render_pulp_internal_beta_al9_aarch64_uses_builds_template():
    """AL9 uses a different prefix (``builds/AlmaLinux-9-…``); check
    the template pivots correctly per major and that ``{arch}``
    substitution honours non-x86 arches.
    """
    files = render_pulp_internal_beta_repo_file(version="9.6", arch="aarch64")
    text = b"\n".join(files.values()).decode()
    assert (
        "https://build.almalinux.org/pulp/content/builds/"
        "AlmaLinux-9-beta-AlmaLinux-9-aarch64-dr/"
    ) in text


def test_render_pulp_internal_beta_includes_debug_and_source_disabled():
    """Debug + source companions ship in the .repo but default to
    enabled=0 — same convention the stock almalinux-repos uses for its
    own ``-debug`` / ``-source`` variants. Lets operators opt in
    without those repos being fetched on every dnf transaction.
    """
    text = b"\n".join(
        render_pulp_internal_beta_repo_file(version="10.1", arch="x86_64").values()
    ).decode()
    assert "[pulp-internal-beta-debug]" in text
    assert "[pulp-internal-beta-source]" in text
    # main is enabled, debug/source are not
    main_block = text.split("[pulp-internal-beta]\n", 1)[1].split("[", 1)[0]
    debug_block = text.split("[pulp-internal-beta-debug]\n", 1)[1].split("[", 1)[0]
    source_block = text.split("[pulp-internal-beta-source]\n", 1)[1]
    assert "enabled=1" in main_block
    assert "enabled=0" in debug_block
    assert "enabled=0" in source_block


def test_render_pulp_internal_beta_pins_gpgcheck_off():
    """The internal-beta repos are NOT signed — the contract for this
    source is ``gpgcheck=0`` everywhere. This is intentional, not a
    regression. (The stable repos layered underneath still have
    ``gpgcheck=1`` because we extract them from the package as-is.)
    """
    text = b"\n".join(
        render_pulp_internal_beta_repo_file(version="10.1", arch="x86_64").values()
    ).decode()
    assert "gpgcheck=1" not in text
    assert text.count("gpgcheck=0") >= 3  # main + debug + source


def test_render_pulp_internal_beta_source_is_arch_less():
    """The source URL has no per-arch component (sources are arch-less
    by design — same SRPM serves every arch). The template carries no
    ``{arch}`` placeholder, and the rendered URL must not contain the
    requested arch.
    """
    text = b"\n".join(
        render_pulp_internal_beta_repo_file(version="9.6", arch="aarch64").values()
    ).decode()
    source_block = text.split("[pulp-internal-beta-source]\n", 1)[1]
    assert "aarch64" not in source_block
    assert "AlmaLinux-9-beta-AlmaLinux-9-src-dr/" in source_block


def test_render_pulp_internal_beta_accepts_major_only_version():
    """Caller can pass a bare major (``"10"``) — the renderer must not
    blow up on the missing minor; only the major is consulted to pick
    the URL template. Stays consistent with how render_pungi_repo_files
    handles the same case.
    """
    files = render_pulp_internal_beta_repo_file(version="10", arch="x86_64")
    assert files, "must produce at least one file"


def test_render_pulp_internal_beta_raises_on_unknown_major():
    """An AL major without a defined URL template must be rejected
    loudly, not silently produce an empty .repo. AL8 is out of scope
    for post-check, so it's a good test case here.
    """
    with pytest.raises(ValueError, match="not defined for AlmaLinux"):
        render_pulp_internal_beta_repo_file(version="8.10", arch="x86_64")


# ============================================================ prepare_repo_files_dir


def test_prepare_repo_files_dir_extracts_for_stable(synthetic_repos_rpm, tmp_path):
    """stable goes through the extract path — basenames + section
    structure match what the package shipped.

    The byte-for-byte preservation contract no longer holds: the
    bind-mount path now flips ``mirrorlist=`` → ``baseurl=`` so dnf
    inside the container doesn't hit a mirrorlist service that lags
    GA. Strict equality with ``extract_repo_files`` would mask that
    flip; the dedicated tests below pin its shape explicitly.
    """
    rc = _RC(source="stable", version="10.1")
    out = prepare_repo_files_dir(
        runtime_config=rc, arch="x86_64",
        target_dir=tmp_path / "repos.d", rpm_bytes=synthetic_repos_rpm,
    )
    assert out == tmp_path / "repos.d"
    files_on_disk = {p.name: p.read_bytes() for p in out.iterdir()}
    # Same set of basenames as the package.
    assert set(files_on_disk) == set(extract_repo_files(synthetic_repos_rpm))
    # Section structure is preserved (only mirrorlist/baseurl lines flip).
    for name, content in files_on_disk.items():
        assert b"[almalinux-baseos]" in content, name


def test_prepare_repo_files_dir_stable_comments_out_mirrorlist(
    synthetic_repos_rpm, tmp_path
):
    """Container-side dnf must go via baseurl, not mirrorlist —
    mirrors.almalinux.org propagates a fresh GA with a lag, the
    canonical baseurl on repo.almalinux.org does not. Every live
    ``mirrorlist=`` line in the package's ``.repo`` files must be
    commented out in the bind-mount directory."""
    rc = _RC(source="stable", version="10.1")
    out = prepare_repo_files_dir(
        runtime_config=rc, arch="x86_64",
        target_dir=tmp_path / "repos.d", rpm_bytes=synthetic_repos_rpm,
    )
    for p in out.iterdir():
        text = p.read_bytes().decode()
        # No live ``mirrorlist=`` survives — ``\n`` anchor avoids matching
        # an already-commented ``#mirrorlist=`` from a previous pass.
        assert "\nmirrorlist=" not in "\n" + text, (
            f"{p.name} still has a live mirrorlist=:\n{text}"
        )
        # The original URL stays intact, just commented.
        assert "#mirrorlist=https://mirrors.almalinux.org/" in text, (
            f"{p.name} missing the commented mirrorlist line:\n{text}"
        )


def test_prepare_repo_files_dir_stable_activates_commented_baseurl(
    synthetic_repos_rpm, tmp_path
):
    """The package ships ``# baseurl=...`` as a documented fallback.
    On the bind-mount path it must become live ``baseurl=...`` so dnf
    actually uses it. URL bytes stay identical — only the leading
    ``# `` is dropped."""
    rc = _RC(source="stable", version="10.1")
    out = prepare_repo_files_dir(
        runtime_config=rc, arch="x86_64",
        target_dir=tmp_path / "repos.d", rpm_bytes=synthetic_repos_rpm,
    )
    for p in out.iterdir():
        text = p.read_bytes().decode()
        assert "baseurl=https://repo.almalinux.org/" in text, (
            f"{p.name} missing live baseurl=:\n{text}"
        )
        # And the placeholders ride through — dnf substitutes them
        # inside the container, not us.
        assert "$releasever" in text and "$basearch" in text, text


def test_prepare_repo_files_dir_stable_preserves_extract_repo_files(synthetic_repos_rpm):
    """``extract_repo_files`` itself stays verbatim — ``test_mirrorlist.py``
    asserts the package's live ``mirrorlist=`` and commented
    ``# baseurl=`` as a contract of the package. The transform must live
    in the bind-mount dispatcher, not in the extractor."""
    raw = extract_repo_files(synthetic_repos_rpm)
    for content in raw.values():
        text = content.decode()
        assert "mirrorlist=https://mirrors.almalinux.org/" in text
        assert "# baseurl=https://repo.almalinux.org/" in text


def test_prepare_repo_files_dir_generates_for_pungi(tmp_path):
    """pungi goes through the render path — URLs come from RepoURL,
    not from any rpm. ``rpm_bytes`` is ignored even if provided."""
    rc = _RC(source="pungi", version="10")
    out = prepare_repo_files_dir(
        runtime_config=rc, arch="x86_64",
        target_dir=tmp_path / "repos.d", rpm_bytes=None,
    )
    files_on_disk = list(out.iterdir())
    assert files_on_disk, "pungi render must produce at least one file"
    text = b"\n".join(p.read_bytes() for p in files_on_disk).decode()
    assert "pungi-10.almalinux.dev" in text


def test_prepare_repo_files_dir_pungi_accepts_major_minor(tmp_path):
    """Regression: the dispatcher must keep using the pungi compose
    layout when callers pass ``runtime_config.version`` verbatim
    (``10.2``) instead of pre-normalising it to the major. The minor
    is dropped inside :class:`RepoURL`, so URLs still point at
    ``*-pungi-10.almalinux.dev`` and never at the public mirror.
    """
    rc = _RC(source="pungi", version="10.2")
    out = prepare_repo_files_dir(
        runtime_config=rc, arch="x86_64",
        target_dir=tmp_path / "repos.d", rpm_bytes=None,
    )
    text = b"\n".join(p.read_bytes() for p in out.iterdir()).decode()
    assert "x86-64-pungi-10.almalinux.dev" in text
    assert "/almalinux/10/x86_64/" in text
    # must NOT silently fall back to the public mirror layout
    assert "repo.almalinux.org" not in text
    assert "vault.almalinux.org" not in text


def test_prepare_repo_files_dir_requires_rpm_bytes_for_stable_or_beta(tmp_path):
    """Caller forgetting ``rpm_bytes`` for stable/beta must get a
    clear error rather than producing an empty repos dir."""
    rc = _RC(source="beta", version="10.2")
    with pytest.raises(ValueError, match="rpm_bytes"):
        prepare_repo_files_dir(
            runtime_config=rc, arch="x86_64",
            target_dir=tmp_path / "repos.d", rpm_bytes=None,
        )


def test_prepare_repo_files_dir_pulp_extracts_and_layers_internal_beta(
    synthetic_repos_rpm, tmp_path
):
    """pulp goes through the extract path AND layers the unsigned
    internal-beta on top — operators see both the package's stable
    .repo files (with the same mirrorlist→baseurl flip stable gets)
    and ``post-check-pulp-internal-beta.repo`` in the bind-mounted dir.
    """
    rc = _RC(source="pulp", version="10.1")
    out = prepare_repo_files_dir(
        runtime_config=rc, arch="x86_64",
        target_dir=tmp_path / "repos.d", rpm_bytes=synthetic_repos_rpm,
    )
    files_on_disk = {p.name: p.read_bytes() for p in out.iterdir()}
    # The package's own .repo files are still there (extract path),
    # with the mirrorlist→baseurl flip applied (same as stable —
    # mirrorlist lags GA, baseurl doesn't).
    package_files = extract_repo_files(synthetic_repos_rpm)
    for fname in package_files:
        assert fname in files_on_disk, f"pulp dropped the package's {fname}"
        text = files_on_disk[fname].decode()
        assert "\nmirrorlist=" not in "\n" + text, (
            f"pulp must flip mirrorlist→baseurl on {fname}:\n{text}"
        )
        assert "baseurl=https://repo.almalinux.org/" in text, (
            f"pulp must activate the commented baseurl in {fname}:\n{text}"
        )
    # plus the synthesised internal-beta layered on top.
    assert "post-check-pulp-internal-beta.repo" in files_on_disk
    text = files_on_disk["post-check-pulp-internal-beta.repo"].decode()
    assert "build.almalinux.org/pulp/content" in text
    assert "gpgcheck=0" in text


def test_prepare_repo_files_dir_requires_rpm_bytes_for_pulp(tmp_path):
    """pulp uses the same extraction path as stable/beta — forgetting
    ``rpm_bytes`` must fail loudly so the unsigned-only file isn't
    silently bind-mounted in isolation.
    """
    rc = _RC(source="pulp", version="10.1")
    with pytest.raises(ValueError, match="rpm_bytes"):
        prepare_repo_files_dir(
            runtime_config=rc, arch="x86_64",
            target_dir=tmp_path / "repos.d", rpm_bytes=None,
        )


def test_prepare_repo_files_dir_creates_missing_target(synthetic_repos_rpm, tmp_path):
    rc = _RC(source="stable", version="10.1")
    target = tmp_path / "deeply" / "nested" / "repos"
    prepare_repo_files_dir(
        runtime_config=rc, arch="x86_64",
        target_dir=target, rpm_bytes=synthetic_repos_rpm,
    )
    assert target.is_dir()


# ============================================================ build_install_command


def test_install_all_command_uses_skip_broken_and_no_weak_deps():
    cmd = build_install_command()
    flat = " ".join(cmd)
    assert "--skip-broken" in flat
    assert "install_weak_deps=False" in flat


# ============================================================ dnf_repo_flags


def test_dnf_repo_flags_empty_for_stable():
    """stable trusts the package's bind-mounted .repo files; no
    extra dnf flags should be added so the per-section enabled flags
    inside those files (incl. ``enabled=0`` for debug/source) are
    honoured verbatim.
    """
    assert dnf_repo_flags(source="stable", version="10.2", arch="x86_64") == ""


def test_dnf_repo_flags_empty_for_beta():
    assert dnf_repo_flags(source="beta", version="10.2", arch="x86_64") == ""


def test_dnf_repo_flags_empty_for_pulp():
    """pulp also trusts the bind-mounted .repo files — both the
    package's stable repos AND the layered internal-beta — so dnf
    needs no extra ``--enablerepo``/``--disablerepo`` flags. Returning
    "" keeps every section's ``enabled`` flag honoured (so the debug
    and source companions stay opt-in).
    """
    assert dnf_repo_flags(source="pulp", version="10.1", arch="x86_64") == ""


def test_dnf_repo_flags_pungi_disables_all_and_enables_our_sections():
    """For pungi: dnf gets ``--disablerepo='*' --enablerepo=<sections>``
    so it cannot fall back to image-baked AlmaLinux repos via paths the
    /etc/yum.repos.d bind-mount doesn't cover. The enabled set must be
    exactly what render_pungi_repo_files synthesised — same source of
    truth (``expected_pkg_sections``).
    """
    flags = dnf_repo_flags(source="pungi", version="10.2", arch="x86_64")
    assert "--disablerepo='*'" in flags
    assert "--enablerepo=" in flags
    # baseos/appstream/crb are present on every (major, arch).
    assert "baseos" in flags
    assert "appstream" in flags
    assert "crb" in flags
    # x86_64 carries RT/NFV.
    assert "rt" in flags
    assert "nfv" in flags
    # trailing space so callers can interpolate without extra logic.
    assert flags.endswith(" ")


def test_dnf_repo_flags_pungi_omits_rt_nfv_on_non_x86():
    """RT/NFV are x86_64-only; for aarch64 the enable list must not
    mention them (otherwise dnf errors out on an unknown repo id).
    """
    flags = dnf_repo_flags(source="pungi", version="10", arch="aarch64")
    assert "baseos" in flags
    # word-boundary check: substring "nfv"/"rt" can appear inside other
    # ids, so check the comma-separated list rather than the raw flag.
    enabled = flags.split("--enablerepo='", 1)[1].split("'", 1)[0].split(",")
    assert "rt" not in enabled
    assert "nfv" not in enabled


def test_dnf_repo_flags_pungi_accepts_major_minor():
    """Regression: ``runtime_config.version`` is passed through verbatim
    (e.g. ``"10.2"``); pungi has no minor, so the helper must strip it
    before consulting expected_pkg_sections (which is keyed by major).
    """
    flags = dnf_repo_flags(source="pungi", version="10.2", arch="x86_64")
    assert "--disablerepo='*'" in flags
    assert "baseos" in flags
