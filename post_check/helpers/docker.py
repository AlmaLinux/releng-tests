"""Thin wrapper over ``docker run --platform=<arch>``.

Used for container-based post-checks (M2): run ``dnf``/``rpm``
inside an AlmaLinux image of any of the four supported architectures via
QEMU binfmt_misc. We don't use the PyPI ``docker`` package — subprocess is simpler
and not tied to a Docker API version. QEMU registration is moved out to
``scripts/setup-qemu.sh`` (requires ``--privileged``); ``run_in_arch`` itself
never requests privileges.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from post_check.config import load_architectures


@dataclass(frozen=True)
class DockerResult:
    """Result of a successful ``docker run``.

    Returned only if the process exited with code 0 — otherwise
    ``run_in_arch`` raises :class:`subprocess.CalledProcessError`.
    """

    returncode: int
    stdout: str
    stderr: str


def docker_available() -> bool:
    """``True`` if ``docker`` is in ``PATH``."""
    return shutil.which("docker") is not None


def require_docker_or_fail() -> None:
    """Fail (NOT skip) the calling test if ``docker`` is not in ``PATH``.

    Used by release-marked, ``requires_docker``-marked tests where a
    skip would silently turn an unverified upgrade/install invariant
    into a "PASS" row in the release report. Release validation needs
    docker by definition (we test the upgrade target inside a real
    container) — a missing docker is a verification gap, not a
    legitimate skip, and the report should surface it loudly.

    Lazy ``pytest`` import keeps this module importable from non-test
    code paths (helpers don't have pytest as a runtime dependency).
    """
    if docker_available():
        return
    import pytest

    pytest.fail(
        "docker is required for this release test but is not available. "
        "Install docker (and on a non-x86_64 host run "
        "./scripts/setup-qemu.sh to register QEMU binfmt_misc) and re-run. "
        "A skip here would falsely show the invariant as validated."
    )


def image_for(version: str, *, arch: str = "x86_64", kitten: bool = False) -> str:
    """Image name for the specified AlmaLinux version on ``arch``.

    Always pins to the **major** tag (``almalinux:<major>`` or
    ``almalinuxorg/almalinux-kitten:<major>``) because the per-minor tag
    does not exist for two cases we care about:

    * **beta** — minor-pinned containers (e.g. ``almalinux:10.2``) are
      never published.
    * **stable, pre-GA** — the post-check suite is the *gate* that runs
      *before* the new minor ships, so its image isn't on Docker Hub yet.

    Using the major tag means we install/upgrade *into* the latest
    released base image and exercise the new repos against it — which
    is what we want to validate anyway.

    ``arch`` lets the per-arch ``docker_image`` template in
    ``config/architectures.yaml`` override the registry/tag — needed
    for ``x86_64_v2``, which is published only as
    ``quay.io/almalinuxorg/almalinux:<major>`` (not on Docker Hub).
    The template supports a single placeholder, ``{major}``. ``kitten``
    overrides both — kitten images live in their own namespace and
    aren't arch-published the same way as the release images.
    """
    major = version.split(".", 1)[0]
    if kitten:
        return f"almalinuxorg/almalinux-kitten:{major}"
    cfg = load_architectures().get(arch, {})
    template = cfg.get("docker_image")
    if template:
        return template.format(major=major)
    return f"almalinux:{major}"


def build_command(
    *,
    arch: str,
    image: str,
    cmd: list[str],
    env: dict | None = None,
    mounts: dict | None = None,
) -> list[str]:
    """Build ``argv`` for ``docker run`` under the required architecture.

    Parses ``config/architectures.yaml`` to obtain
    ``docker_platform`` (e.g. ``linux/arm64`` for ``aarch64``). This
    step is a separate function so unit tests can verify argv without
    actually running docker.
    """
    arches = load_architectures()
    platform = arches[arch]["docker_platform"]
    args: list[str] = ["docker", "run", "--rm", f"--platform={platform}"]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    for src, dst in (mounts or {}).items():
        args += ["-v", f"{src}:{dst}"]
    args.append(image)
    args += cmd
    return args


def run_in_arch(
    *,
    arch: str,
    image: str,
    cmd: list[str],
    env: dict | None = None,
    mounts: dict | None = None,
    timeout: int = 600,
) -> DockerResult:
    """Run ``cmd`` inside ``image`` under the specified ``arch``.

    Raises :class:`subprocess.TimeoutExpired` if ``timeout`` expired,
    and :class:`subprocess.CalledProcessError` if the process exited non-zero.
    This is intentional: calling code almost always wants
    an exception rather than manually inspecting ``returncode`` (tests fail with
    a clear trace).
    """
    full = build_command(arch=arch, image=image, cmd=cmd, env=env, mounts=mounts)
    proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, full, output=proc.stdout, stderr=proc.stderr
        )
    return DockerResult(proc.returncode, proc.stdout, proc.stderr)
