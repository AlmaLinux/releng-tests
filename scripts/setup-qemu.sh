#!/usr/bin/env bash
# Register QEMU binfmt_misc for all 4 AlmaLinux architectures in local
# Docker. Done ONCE per host (binfmt_misc state lives in the kernel but
# is lost after a reboot -> run after every reboot, or use
# systemd-binfmt). In CI (GitHub Actions ubuntu-latest) registration is
# done as a separate workflow step before running
# pytest -m requires_docker.
#
# Requires --privileged: tonistiigi/binfmt writes to /proc/sys/fs/binfmt_misc.
set -euo pipefail

if ! command -v docker >/dev/null; then
  echo "docker not found in PATH" >&2
  exit 1
fi

# Register QEMU binfmt_misc for all architectures
docker run --privileged --rm tonistiigi/binfmt --install all
echo "QEMU binfmt registered. Verifying..."
docker run --rm --platform=linux/arm64   almalinux:10 uname -m
docker run --rm --platform=linux/s390x   almalinux:10 uname -m
docker run --rm --platform=linux/ppc64le almalinux:10 uname -m
echo "OK"
