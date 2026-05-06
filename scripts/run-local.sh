#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage:
  ALMA_SOURCE=<stable|beta|pungi> ALMA_VERSION=<x.y|x> [ALMA_ARCHES=<csv>] $0 [pytest args]

Examples:
  ALMA_SOURCE=stable ALMA_VERSION=10.1 ALMA_ARCHES=x86_64 $0
  ALMA_SOURCE=beta ALMA_VERSION=10.2 ALMA_ARCHES=x86_64,aarch64 $0 -m "not requires_docker"
  ALMA_SOURCE=pungi ALMA_VERSION=10 ALMA_ARCHES=s390x $0 tests/release/test_repomd_signature.py
EOF
  exit 1
}

[[ -z "${ALMA_SOURCE:-}" ]] && usage
[[ -z "${ALMA_VERSION:-}" ]] && usage

# Normalize ARCHES (strip spaces)
export ALMA_ARCHES="$(echo "${ALMA_ARCHES:-x86_64}" | tr -d ' ')"

# If non-x86_64 arches are requested and container tests are needed, bring up QEMU.
# x86_64_v2 is native on any modern amd64 host (just a CPU feature baseline) — no QEMU.
# i686 doesn't run container tests at all (skip_categories: container in
# config/architectures.yaml), so its presence alone is not a reason to set up QEMU —
# but i686 is x86 anyway, no emulation needed.
NEEDS_QEMU=0
case ",$ALMA_ARCHES," in
  *,aarch64,*|*,s390x,*|*,ppc64le,*) NEEDS_QEMU=1 ;;
esac

# Bring up QEMU only if pytest args don't include "-m not requires_docker"
WANTS_DOCKER=1
for a in "$@"; do
  case "$a" in
    *"not requires_docker"*) WANTS_DOCKER=0 ;;
  esac
done

if [[ $NEEDS_QEMU -eq 1 && $WANTS_DOCKER -eq 1 ]]; then
  if ! command -v docker >/dev/null; then
    echo "docker not found; container tests on $ALMA_ARCHES require Docker." >&2
    exit 2
  fi
  echo ">>> Registering QEMU binfmt..."
  docker run --privileged --rm tonistiigi/binfmt --install all
fi

# Honor $PYTEST env so the Makefile can point us at .venv/bin/pytest
# without forcing the user to ``source .venv/bin/activate`` first.
exec "${PYTEST:-pytest}" "$@"
