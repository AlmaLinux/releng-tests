"""Inside-container assertions for ``/etc/os-release`` and
``/etc/almalinux-release``.

The version-under-test is reached **by upgrading** an
``almalinux:<major>`` container into the new repos before reading
either file. Reading them on the bare image would compare against
"latest released minor", which won't match the version under test
during a pre-GA run (the ``almalinux:<minor>`` Docker tag isn't
published until GA, and is never published for beta).

The repos used for the upgrade come straight out of the
``almalinux-repos`` package shipped *for the target version* — no
URL is generated locally, so a contract drift between our test and
the package is impossible by construction.
"""
import pytest

from post_check.helpers import docker as dock
from post_check.helpers import repo_inject

pytestmark = [pytest.mark.requires_docker]


def _ensure_docker():
    """Fail (not skip) when docker is missing — this is a release test.

    Skipping here would silently turn an unverified
    container/upgrade invariant into a "PASS" row in the release
    report. The shared ``require_docker_or_fail`` helper carries the
    standard operator-actionable message.
    """
    dock.require_docker_or_fail()


# Per-arch upgrade timeouts — same scale as test_dnf_upgrade since
# we're doing the same thing (full ``dnf upgrade -y`` under QEMU on
# non-x86 arches).
_UPGRADE_TIMEOUTS = {
    "x86_64": 1800,
    "x86_64_v2": 1800,
    "aarch64": 5400,
    "s390x": 14400,
    "ppc64le": 14400,
}


def test_run_in_arch_executes_uname_m(arch):
    """Sanity smoke: ``uname -m`` inside ``almalinux:10`` running under
    ``--platform=linux/<arch>`` reports exactly ``<arch>``.

    Verifies the binfmt + QEMU plumbing is working for non-x86 arches
    *before* we spend half an hour on a real upgrade test on the same
    arch.

    Not marked ``release`` — this is a harness smoke (verifies QEMU
    plumbing in CI), not release content. Keeping it out of the GA
    report avoids cluttering the engineer-facing artifact with rows
    that are about *the test runner* rather than AlmaLinux itself.
    """
    _ensure_docker()
    image = dock.image_for("10", arch=arch)
    res = dock.run_in_arch(arch=arch, image=image, cmd=["uname", "-m"], timeout=180)
    # ``uname -m`` reports the kernel/CPU arch — for x86_64_v2 the
    # kernel still prints ``x86_64`` (v2 is a CPU-feature baseline,
    # not a separate arch ID). Treat both as "matches the requested
    # x86_64 family"; every other arch must report itself verbatim.
    expected = "x86_64" if arch == "x86_64_v2" else arch
    assert res.stdout.strip() == expected, f"Expected {expected}, got {res.stdout!r}"


@pytest.mark.release
def test_release_files_match_target_after_upgrade(
    runtime_config, arch, tmp_path, gpg_key_path, almalinux_repos_pkg_bytes
):
    """After ``dnf upgrade`` into the target release's repos, both
    ``/etc/os-release`` and ``/etc/almalinux-release`` report the
    target version exactly.

    One container invocation, two assertions — cheaper than two
    separate tests (each upgrade can take 30+ minutes under QEMU
    emulation on s390x/ppc64le), and the two files are tightly
    coupled invariants of the same upgrade outcome.

    For pungi (major-only ``runtime_config.version``) we still assert
    *startswith*: pungi composes don't carry a "minor", so the file
    just needs to start with the right major.
    """
    _ensure_docker()

    # Source-aware: stable/beta extract the package's own .repo
    # files; pungi gets synthesised URLs to the per-arch compose
    # host (its package's .repo files point at the public layout,
    # where the compose hasn't been published yet).
    repos_dir = repo_inject.prepare_repo_files_dir(
        runtime_config=runtime_config, arch=arch,
        target_dir=tmp_path / "target-repos.d",
        rpm_bytes=almalinux_repos_pkg_bytes,
    )

    image = dock.image_for(runtime_config.version, arch=arch)
    cmd = [
        "bash", "-c",
        "rpm --import /tmp/RPM-GPG-KEY && "
        "dnf clean all && "
        "dnf upgrade -y 2>&1 | tail -200; "
        "echo === os-release ===; "
        "cat /etc/os-release; "
        "echo === almalinux-release ===; "
        "cat /etc/almalinux-release"
    ]
    res = dock.run_in_arch(
        arch=arch, image=image, cmd=cmd,
        mounts={str(repos_dir): "/etc/yum.repos.d",
                str(gpg_key_path): "/tmp/RPM-GPG-KEY:ro"},
        timeout=_UPGRADE_TIMEOUTS[arch],
    )

    # ---- /etc/os-release ----
    try:
        os_release_block = (
            res.stdout
            .split("=== os-release ===", 1)[1]
            .split("=== almalinux-release ===", 1)[0]
        )
    except IndexError:
        pytest.fail(f"upgrade did not produce an os-release block:\n{res.stdout[-1000:]}")

    lines = {
        ln.split("=", 1)[0]: ln.split("=", 1)[1].strip('"')
        for ln in os_release_block.splitlines()
        if "=" in ln and not ln.lstrip().startswith("#")
    }
    assert "VERSION_ID" in lines, f"VERSION_ID missing:\n{os_release_block}"
    actual_version = lines["VERSION_ID"]
    expected = runtime_config.version
    if "." in expected:
        assert actual_version == expected, (
            f"After upgrade, VERSION_ID={actual_version!r} != expected {expected!r}.\n"
            f"upgrade output (tail):\n{res.stdout[-1000:]}"
        )
    else:
        assert actual_version.startswith(expected), (
            f"After upgrade, VERSION_ID={actual_version!r} does not start with {expected!r}"
        )
    assert lines.get("ID") == "almalinux"

    # ---- /etc/almalinux-release ----
    al_release_block = res.stdout.split("=== almalinux-release ===", 1)[1]
    assert "AlmaLinux" in al_release_block
    if "." in expected:
        assert expected in al_release_block, (
            f"/etc/almalinux-release does not mention {expected!r}:\n{al_release_block}"
        )
    else:
        assert f"AlmaLinux release {expected}" in al_release_block


def test_qemu_binfmt_check_documents_required_setup(arch):
    """If arch is not x86_64, check that binfmt is registered -- otherwise skip with instructions."""
    if arch in ("x86_64", "x86_64_v2"):
        # x86_64_v2 is a micro-arch baseline of x86_64 — no QEMU needed
        # on an x86_64 host (the CPU just has to support v2 features,
        # which every host built in the last decade does).
        pytest.skip("native, binfmt not needed")
    _ensure_docker()
    image = dock.image_for("10", arch=arch)
    # Smoke: attempt a trivial command; if QEMU is not registered, docker itself will complain
    try:
        dock.run_in_arch(arch=arch, image=image, cmd=["true"], timeout=60)
    except Exception as e:
        pytest.fail(
            f"docker run --platform=linux/{arch} does not work: {e}\n"
            f"Run: docker run --privileged --rm tonistiigi/binfmt --install all\n"
            f"Or ./scripts/setup-qemu.sh"
        )
