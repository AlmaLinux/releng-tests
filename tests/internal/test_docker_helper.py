"""Tests for ``post_check.helpers.docker``.

Unit tests don't require docker to be installed — they only verify that
``build_command`` assembles a correct argv. To avoid depending on
``post_check.config`` (which is created in M0/Slice0/Task01 and may not
yet exist), we patch ``load_architectures`` via ``monkeypatch`` directly
in the ``post_check.helpers.docker`` module.

Integration tests are marked ``requires_docker`` (see pytest.ini) and
check ``docker_available()`` themselves so they don't fail in
environments without docker (for example, on a developer's laptop
without Docker Desktop).
"""

from __future__ import annotations

import subprocess

import pytest

from post_check.helpers import docker as docker_helper
from post_check.helpers.docker import (
    build_command,
    docker_available,
    image_for,
    run_in_arch,
)


# Minimal copy of data from config/architectures.yaml — duplicated here
# so unit tests don't depend on the YAML being present.
FAKE_ARCHES = {
    "x86_64":  {"docker_platform": "linux/amd64"},
    "aarch64": {"docker_platform": "linux/arm64"},
    "s390x":   {"docker_platform": "linux/s390x"},
    "ppc64le": {"docker_platform": "linux/ppc64le"},
    "i686":    {"docker_platform": "linux/386"},
    "x86_64_v2": {
        "docker_platform": "linux/amd64/v2",
        "docker_image": "quay.io/almalinuxorg/almalinux:{major}",
    },
}


@pytest.fixture(autouse=True)
def _patch_load_architectures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaces ``load_architectures`` in the docker module with fixture data."""
    monkeypatch.setattr(docker_helper, "load_architectures", lambda: FAKE_ARCHES)


# --- unit ----------------------------------------------------------------

def test_docker_runner_constructs_platform_flag_per_arch() -> None:
    cmd = build_command(arch="aarch64", image="almalinux:10", cmd=["uname", "-m"])
    assert "--platform=linux/arm64" in cmd
    # The image name always comes before the command and after all flags.
    assert cmd.index("almalinux:10") < cmd.index("uname")


def test_docker_runner_constructs_platform_flag_for_s390x() -> None:
    cmd = build_command(arch="s390x", image="almalinux:9", cmd=["true"])
    assert "--platform=linux/s390x" in cmd


def test_docker_runner_constructs_platform_flag_for_ppc64le() -> None:
    cmd = build_command(arch="ppc64le", image="almalinux:9", cmd=["true"])
    assert "--platform=linux/ppc64le" in cmd


def test_docker_runner_propagates_env_vars_via_minus_e() -> None:
    cmd = build_command(
        arch="x86_64", image="almalinux:10", cmd=["echo", "x"], env={"FOO": "BAR"}
    )
    assert "-e" in cmd and "FOO=BAR" in cmd


def test_docker_runner_propagates_mounts_via_minus_v() -> None:
    cmd = build_command(
        arch="x86_64",
        image="almalinux:10",
        cmd=["ls", "/data"],
        mounts={"/host/path": "/data"},
    )
    assert "-v" in cmd and "/host/path:/data" in cmd


def test_docker_runner_uses_rm_flag() -> None:
    """``--rm`` is required — otherwise stale containers are left behind in CI."""
    cmd = build_command(arch="x86_64", image="almalinux:10", cmd=["true"])
    assert "--rm" in cmd


def test_docker_runner_does_not_use_privileged() -> None:
    """``--privileged`` is only needed in setup-qemu.sh, not in run_in_arch."""
    cmd = build_command(arch="x86_64", image="almalinux:10", cmd=["true"])
    assert "--privileged" not in cmd


def test_docker_runner_uses_correct_image_for_version() -> None:
    # image_for() pins to the major only — the per-minor tag may not
    # exist yet (beta never has it; stable doesn't until the release ships).
    assert image_for("10.1") == "almalinux:10"
    assert image_for("9.6") == "almalinux:9"
    assert image_for("10") == "almalinux:10"


def test_docker_runner_image_for_kitten() -> None:
    assert image_for("10", kitten=True) == "almalinuxorg/almalinux-kitten:10"
    assert image_for("10.2", kitten=True) == "almalinuxorg/almalinux-kitten:10"


def test_docker_runner_image_for_arch_uses_yaml_override() -> None:
    """An arch with a ``docker_image`` template overrides the default.

    For ``x86_64_v2`` the YAML pins us at ``quay.io`` instead of the
    Docker Hub library image — this is the only way to get the v2
    micro-arch container, and the test locks in that ``image_for``
    actually consults the per-arch override.
    """
    assert (
        image_for("10.1", arch="x86_64_v2")
        == "quay.io/almalinuxorg/almalinux:10"
    )
    assert image_for("10", arch="x86_64_v2") == "quay.io/almalinuxorg/almalinux:10"


def test_docker_runner_image_for_arch_default_unchanged() -> None:
    """Arches without a ``docker_image`` template fall back to the
    historic ``almalinux:<major>``."""
    assert image_for("10.1", arch="x86_64") == "almalinux:10"
    assert image_for("9.6", arch="aarch64") == "almalinux:9"
    assert image_for("10.0", arch="i686") == "almalinux:10"


def test_docker_runner_constructs_v2_platform_flag() -> None:
    """``x86_64_v2`` propagates as ``--platform=linux/amd64/v2``.

    This is the second half of the v2 contract — the registry override
    above gets us the right image, but without the platform flag
    docker would still pull amd64-baseline (v1) and the test wouldn't
    actually exercise v2.
    """
    cmd = build_command(arch="x86_64_v2", image="quay.io/x:10", cmd=["true"])
    assert "--platform=linux/amd64/v2" in cmd


def test_docker_runner_constructs_i686_platform_flag() -> None:
    cmd = build_command(arch="i686", image="almalinux:10", cmd=["true"])
    assert "--platform=linux/386" in cmd


# --- integration: requires docker + qemu binfmt --------------------------


@pytest.mark.requires_docker
@pytest.mark.parametrize("arch", ["x86_64", "aarch64", "s390x", "ppc64le"])
def test_run_in_arch_executes_uname_m(arch: str) -> None:
    if not docker_available():
        pytest.skip("docker not available")
    res = run_in_arch(arch=arch, image="almalinux:10", cmd=["uname", "-m"], timeout=120)
    assert res.stdout.strip() == arch


@pytest.mark.requires_docker
def test_run_in_arch_raises_on_command_timeout() -> None:
    if not docker_available():
        pytest.skip("docker")
    with pytest.raises(subprocess.TimeoutExpired):
        run_in_arch(arch="x86_64", image="almalinux:10", cmd=["sleep", "999"], timeout=2)


@pytest.mark.requires_docker
def test_run_in_arch_raises_on_nonzero_exit() -> None:
    if not docker_available():
        pytest.skip("docker")
    with pytest.raises(subprocess.CalledProcessError):
        run_in_arch(arch="x86_64", image="almalinux:10", cmd=["false"])
