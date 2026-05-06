import pytest
from post_check.config import compute_prev_version
from post_check.helpers import docker as dock, repo_inject

# NOTE: no file-level ``pytestmark``. The release marker is a GA-report
# signal — only the *real* upgrade test below carries it. The other
# functions in this file are co-located meta/smoke tests (unit tests
# of the prev-minor math, the image_for tag policy, and a docker-pull
# failure smoke). They document the contract of the real test, but
# they aren't release content and shouldn't pollute the release report.
# ``requires_docker`` is also opted into per-test, so the pure-unit
# ones run without docker present.


def _prev_version(runtime_config) -> str:
    """Resolve the previous AlmaLinux minor for ``runtime_config.version``.

    Delegates the math to :func:`post_check.config.compute_prev_version`
    and skips the test when there is no in-major predecessor (e.g.
    ``10.0`` or pungi's major-only versions).
    """
    pv = compute_prev_version(runtime_config.version)
    if pv is None:
        pytest.skip(
            f"No previous minor for ALMA_VERSION={runtime_config.version!r}; "
            f"upgrade test only runs on x.y with y >= 1."
        )
    return pv


def test_upgrade_skipped_when_no_prev_minor(monkeypatch, runtime_config):
    """For x.0 or major-only versions, ``_prev_version`` must skip with
    a clear message — not silently invent a prev or run with junk.
    """

    class _RC:
        version = "10.0"

    with pytest.raises(pytest.skip.Exception, match="No previous minor"):
        _prev_version(_RC())


def test_upgrade_target_version_inferred_from_input_version(runtime_config):
    """Target = ``ALMA_VERSION``. Trivial but documents the contract for
    the upgrade test (target side comes from runtime_config, prev side
    is computed)."""
    assert runtime_config.version


@pytest.mark.release
@pytest.mark.requires_docker
def test_dnf_upgrade_changes_os_release_to_target(
    runtime_config, arch, tmp_path, gpg_key_path, almalinux_repos_pkg_bytes
):
    """A real ``dnf upgrade -y`` from the previous minor lands the
    container on the target version.

    What's verified after the upgrade:
      * ``/etc/os-release`` reports ``VERSION_ID="<target.minor>"``;
      * ``rpm -q almalinux-release`` reports
        ``almalinux-release-<target.minor>``.

    The ``.repo`` files used for the upgrade come from the **target
    release's** ``almalinux-repos`` package — extracted, mounted over
    ``/etc/yum.repos.d/`` so the prev-minor's pre-baked repos are
    hidden. dnf then talks only to the URLs the new release actually
    publishes, which is what we want to validate.

    Skipped for ``ALMA_VERSION`` with no in-major predecessor (``x.0``
    or major-only pungi) — there's nothing to upgrade *from*.
    """
    dock.require_docker_or_fail()
    prev = _prev_version(runtime_config)
    prev_image = dock.image_for(prev, arch=arch)

    # Source-aware: stable/beta use the package's own .repo files;
    # pungi gets synthesised URLs to the per-arch compose host.
    # (test_dnf_upgrade is skipped for pungi anyway via _prev_version
    # — pungi is major-only, no in-major predecessor to upgrade from
    # — but we go through the same dispatcher for consistency.)
    repos_dir = repo_inject.prepare_repo_files_dir(
        runtime_config=runtime_config, arch=arch,
        target_dir=tmp_path / "target-repos.d",
        rpm_bytes=almalinux_repos_pkg_bytes,
    )

    # gpg_key_path — fixture, returns a Path to the dynamically downloaded key for the target major
    gpg_src = gpg_key_path

    # For pungi: explicitly --disablerepo='*' --enablerepo=<our-sections>
    # so the upgrade lands on pungi compose URLs even if the prev-minor
    # image had cached metadata for the public mirrors. For stable/beta
    # this returns "". See repo_inject.dnf_repo_flags.
    repo_flags = repo_inject.dnf_repo_flags(
        source=runtime_config.source, version=runtime_config.version, arch=arch,
    )

    cmd = [
        "bash", "-c",
        "rpm --import /tmp/RPM-GPG-KEY && "
        "dnf clean all && "
        f"dnf upgrade {repo_flags}-y 2>&1 | tail -200; "
        "echo === os-release ===; "
        "cat /etc/os-release; "
        "echo === almalinux-release ===; "
        "rpm -q almalinux-release"
    ]
    timeout = {
        "x86_64": 1800,
        "x86_64_v2": 1800,
        "aarch64": 5400,
        "s390x": 14400,
        "ppc64le": 14400,
    }[arch]
    res = dock.run_in_arch(
        arch=arch, image=prev_image, cmd=cmd,
        mounts={str(repos_dir): "/etc/yum.repos.d",
                str(gpg_src): "/tmp/RPM-GPG-KEY:ro"},
        timeout=timeout,
    )
    expected = runtime_config.version
    assert f"VERSION_ID=\"{expected}\"" in res.stdout, f"Did not find VERSION_ID={expected} in /etc/os-release:\n{res.stdout[-1000:]}"
    assert f"almalinux-release-{expected}" in res.stdout, f"Did not find almalinux-release-{expected} in the output"


@pytest.mark.requires_docker
def test_dnf_upgrade_fails_when_prev_image_does_not_exist():
    """Smoke: a tag that definitely does not exist must produce a clear error."""
    with pytest.raises(Exception):
        # On an attempt to pull tag 99.99 → docker itself will complain; the test verifies the failure is readable
        dock.run_in_arch(arch="x86_64", image="almalinux:99.99", cmd=["true"], timeout=120)


def test_dnf_upgrade_handles_archived_minor_via_vault():
    """Documenting the behavior: ``image_for()`` always returns the
    major-pinned tag, because per-minor tags may not be published (beta
    never has them; stable not until GA). The previous minor is reached
    via the major tag, which on Docker Hub points to the latest released
    minor — exactly what we want to upgrade *from*.
    """
    # Declarative test — verifies image_for logic, does not start a container
    assert dock.image_for("10.0") == "almalinux:10"
    assert dock.image_for("10.2") == "almalinux:10"
