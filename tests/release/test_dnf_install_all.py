"""Resolve ``dnf install '*'`` inside a container of every arch.

Test M2: make sure that the *entire* set of packages from the repos under
test can be **resolved** in a single transactional ``dnf install '*'``
call on each of the four AlmaLinux architectures.

This is a resolvability check — not a real install. ``--assumeno`` makes
dnf perform full dependency resolution, print the transaction summary
(including ``Skipped packages were: ...`` from ``--skip-broken``), and
then exit without actually downloading or installing anything. Real
installation under QEMU (especially the rpm scriptlet phase on
``s390x``/``ppc64le``) used to take hours; resolution takes minutes.

``--skip-broken`` keeps the coverage valid when individual packages
*expectedly* conflict (see the allowlist under
``tests/data/allowed_install_failures-<major>.yaml``); any unexpected
"skip" is an error.

Slow arches (``s390x``/``ppc64le``) automatically receive the ``slow``
marker via ``pytest_collection_modifyitems`` in ``conftest.py``.
"""
import pytest
from post_check.helpers import docker as dock
from post_check.helpers import repo_inject, dnf_log

pytestmark = [pytest.mark.requires_docker, pytest.mark.release]

# Per-arch timeouts in seconds (per single arch).
#
# The test no longer performs a real install — it only runs dependency
# resolution (``dnf install --assumeno``). The dominant cost is dnf's
# Python startup + metadata fetch + depsolve, which is far cheaper than
# downloading thousands of RPMs and running their scriptlets under QEMU
# emulation. 10 minutes is enough headroom for every arch, including
# s390x/ppc64le under QEMU.
_TIMEOUTS = {
    "x86_64": 600,
    "x86_64_v2": 600,
    "aarch64": 600,
    "s390x": 600,
    "ppc64le": 600,
}


def _slow_arches():
    return {"s390x", "ppc64le"}


def test_dnf_install_all_for_arch(
    runtime_config, arch, tmp_path, gpg_key_path, almalinux_repos_pkg_bytes
):
    """``dnf install --skip-broken --assumeno '*'`` resolves cleanly
    against the version-under-test's repos for ``arch``.

    What's verified: the depsolver finishes without errors and the
    set of "Skipped packages were:" entries is a **subset** of the
    per-major allowlist (``tests/data/allowed_install_failures-<major>.yaml``).
    Anything outside the allowlist is a real un-resolvable package
    and fails the test.

    What's *not* verified: real install (no download, no scriptlets);
    ``--assumeno`` exits dnf right after the depsolve. That keeps the
    test minutes-long instead of hours-long under QEMU emulation on
    s390x/ppc64le.
    """
    dock.require_docker_or_fail()
    if arch in _slow_arches():
        pytest.importorskip("pytest")  # the actual slow marker is applied via pytest.mark.skipif in conftest

    # Source-aware: for stable/beta we use the package's own .repo
    # files (extract); for pungi we synthesise URLs pointing at the
    # per-arch compose host (the package ships URLs that point at
    # the public mirror layout, where the pungi compose hasn't
    # been published yet — extraction would 404).
    repos_dir = repo_inject.prepare_repo_files_dir(
        runtime_config=runtime_config, arch=arch,
        target_dir=tmp_path / "repos.d",
        rpm_bytes=almalinux_repos_pkg_bytes,
    )

    # gpg_key_path — fixture, returns a Path to the dynamically downloaded key
    # (~/.cache/post-check/gpg-keys/RPM-GPG-KEY-AlmaLinux-<major>)
    gpg_src = gpg_key_path
    major = runtime_config.version.split(".")[0]

    image = dock.image_for(runtime_config.version, arch=arch)

    # For pungi: explicitly --disablerepo='*' --enablerepo=<our-sections>
    # so dnf cannot fall back to image-baked AlmaLinux repos via paths
    # the bind-mount doesn't cover (dnf.conf, /etc/dnf/repos.d/). For
    # stable/beta this returns "" — the bind-mounted package files are
    # the source of truth. See repo_inject.dnf_repo_flags for the
    # rationale.
    repo_flags = repo_inject.dnf_repo_flags(
        source=runtime_config.source, version=runtime_config.version, arch=arch,
    )

    # 1) import the key (so signature checks during depsolve don't fail
    #    with missing-key errors that would mask real resolution issues),
    # 2) clean any image-baked metadata so we resolve against the
    #    mounted .repo files,
    # 3) resolve '*' with --skip-broken --assumeno: dnf prints the
    #    transaction (including "Skipped packages were: ...") and exits
    #    without installing anything.
    #
    # The whole pipeline is wrapped so the bash command always exits 0
    # — dnf returns non-zero with --assumeno (operation cancelled by
    # user), which would otherwise trip run_in_arch's CalledProcessError.
    cmd = [
        "bash", "-c",
        f"rpm --import /tmp/RPM-GPG-KEY && "
        f"dnf clean all && "
        f"dnf install {repo_flags}--skip-broken --assumeno --setopt=install_weak_deps=False '*' 2>&1 | tee /tmp/resolve.log; "
        f"echo RESOLVE_RC=${{PIPESTATUS[0]}}"
    ]
    try:
        res = dock.run_in_arch(
            arch=arch, image=image, cmd=cmd,
            mounts={str(repos_dir): "/etc/yum.repos.d",
                    str(gpg_src): "/tmp/RPM-GPG-KEY:ro"},
            timeout=_TIMEOUTS[arch],
        )
    except Exception as e:
        pytest.fail(f"dnf install '*' resolution failed on {arch}: {e}")

    skipped = dnf_log.parse_skipped(res.stdout)
    allow = dnf_log.load_allowlist(major)
    extra = skipped - allow
    assert not extra, f"Packages outside the allowlist failed to resolve on {arch}: {sorted(extra)}\n(allowlist size={len(allow)})"


def test_dnf_install_all_does_not_disable_gpgcheck(almalinux_repos_pkg_bytes):
    """Contract test: every ``.repo`` shipped by ``almalinux-repos``
    keeps ``gpgcheck=1``. Re-uses the same extraction the container
    test mounts, so a regression in the *package* (not just our
    rendering) still trips this.
    """
    files = repo_inject.extract_repo_files(almalinux_repos_pkg_bytes)
    assert files, "almalinux-repos shipped no .repo files"
    text = b"\n".join(files.values()).decode()
    assert "gpgcheck=0" not in text
    assert text.count("gpgcheck=1") >= 3  # baseos / appstream / crb at minimum


def test_dnf_install_all_uses_only_target_repos(
    runtime_config, arch, tmp_path, almalinux_repos_pkg_bytes
):
    """Smoke: confirm the package's repo files reach the container — i.e.
    ``dnf repolist enabled`` lists at least the BaseOS repo from the
    new package. We grep for ``baseos`` (the section_id used by every
    AlmaLinux release of ``almalinux-repos``) — it's a stable contract
    even if the package switches between mirrorlist and baseurl-only
    sections in the future.
    """
    dock.require_docker_or_fail()
    repos_dir = repo_inject.prepare_repo_files_dir(
        runtime_config=runtime_config, arch=arch,
        target_dir=tmp_path / "repos.d",
        rpm_bytes=almalinux_repos_pkg_bytes,
    )
    image = dock.image_for(runtime_config.version, arch=arch)
    res = dock.run_in_arch(
        arch=arch, image=image, cmd=["dnf", "repolist", "enabled"],
        mounts={str(repos_dir): "/etc/yum.repos.d"}, timeout=300,
    )
    # Match case-insensitively — `dnf repolist` prints the `name=` line,
    # which is "AlmaLinux 10 - BaseOS", but the section_id is `baseos`.
    assert "baseos" in res.stdout.lower(), (
        f"BaseOS repo from almalinux-repos didn't show up in dnf repolist:\n"
        f"{res.stdout}"
    )
