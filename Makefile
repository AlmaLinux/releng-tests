PY ?= python3

# --- Variables -------------------------------------------------------------
# ALMA_SOURCE and ALMA_VERSION are REQUIRED for any target that runs the
# release-content tests (test-release / test-release-host /
# test-release-container / test-release-arch-*). There is no "default
# release" we'd want to silently pick. The validation is enforced by the
# ``_require-vars`` pre-target below; running e.g. ``make test-release``
# without them prints a clear error and exits non-zero.
#
# Internal-tooling tests (``test-internal``) do NOT depend on a real
# AlmaLinux release — they validate post-check itself and run offline,
# so they don't require these env vars.
#
# ALMA_ARCHES defaults to all four supported architectures. Override on
# the command line for a quick single-arch run.
ALMA_ARCHES ?= x86_64,aarch64,s390x,ppc64le

# Use the venv pytest if it exists — otherwise fall back to PATH.
# This means ``make test-release`` works without ``source .venv/bin/activate`` first.
PYTEST ?= $(shell test -x .venv/bin/pytest && echo .venv/bin/pytest || echo pytest)

export ALMA_SOURCE ALMA_VERSION ALMA_ARCHES ALMA_REPOS PYTEST

# Bare ``make`` (no target) shows help — friendlier than running the
# default test target by accident on a fresh checkout.
.DEFAULT_GOAL := help

.PHONY: help test-release test-release-host test-release-container test-internal lint setup-qemu _require-vars

# ----------------------------------------------------------------- help

define HELP_TEXT
post-check — AlmaLinux release validation suite

REQUIRED variables (only for the test-release-* targets):
  ALMA_SOURCE   stable | beta | pungi
  ALMA_VERSION  major.minor for stable/beta (e.g. 10.1); major only for pungi (e.g. 10)

OPTIONAL variables:
  ALMA_ARCHES   CSV of architectures (default: x86_64,aarch64,s390x,ppc64le)
                Optional extras: i686 (AL9, AL10), x86_64_v2 (AL10 only).
                See docs/ARCH_MATRIX.md for what's tested per arch+version.
  ALMA_REPOS    CSV of repos (default: full set per source)

Test categories:
  tests/release/   — validates the AlmaLinux release (the product).
                     Drives the GA artifact under reports/release-report-*.md.
  tests/internal/  — validates post-check itself (helpers, parsers, the
                     docker wrapper, suite-level contracts). Tests the
                     *tool*, not the product.

Targets:
  test-release                 Run the full release-content suite. Compact
                               stdout + detailed reports/release-report-
                               <UTC-timestamp>.md (the GA artifact).
                               REQUIRES ALMA_SOURCE/ALMA_VERSION.
  test-release-host            Release tests minus the container-bound ones
                               (no Docker). Good for fast iteration.
  test-release-container       Release tests that need Docker only.
  test-release-arch-<arch>     Release tests pinned to one arch
                               (x86_64|aarch64|s390x|ppc64le|i686|x86_64_v2).

  test-internal                Run every test under tests/internal/. Offline,
                               no env vars required.

  test-file-<name>             Run a single test file by basename (without
                               .py). Searches tests/release/, then
                               tests/internal/. Env vars required only if
                               the file ends up in tests/release/.
                               Example: 'make test-file-test_almalinux_repos_pkg'.
  test-k-<expr>                Run tests matching a pytest -k expression
                               (substring match across test names — can
                               match many tests across files).
                               Example: 'make test-k-dnf_install'.

  lint                         ruff + mypy.
  setup-qemu                   Register binfmt_misc for non-x86_64 emulation.
  help                         This message.

Examples:
  make test-release ALMA_SOURCE=stable ALMA_VERSION=10.1
  make test-release ALMA_SOURCE=beta   ALMA_VERSION=10.2
  make test-release ALMA_SOURCE=pungi  ALMA_VERSION=10
  make test-release ALMA_SOURCE=stable ALMA_VERSION=10.1 ALMA_ARCHES=x86_64
  make test-release-host ALMA_SOURCE=stable ALMA_VERSION=10.1
  make test-release-arch-aarch64 ALMA_SOURCE=stable ALMA_VERSION=10.1
  make test-internal
  make test-file-test_almalinux_repos_pkg ALMA_SOURCE=stable ALMA_VERSION=10.1
  make test-file-test_url_builder
  make test-k-dnf_install ALMA_SOURCE=stable ALMA_VERSION=10.1
endef
export HELP_TEXT

help:
	@echo "$$HELP_TEXT"

# ----------------------------------------------------------------- guards

# Pre-target that ensures ALMA_SOURCE / ALMA_VERSION are set. Anything
# that hits AlmaLinux mirrors must depend on this — otherwise the
# operator gets a confusing pytest collection error far from the real
# cause.
_require-vars:
	@if [ -z "$$ALMA_SOURCE" ] || [ -z "$$ALMA_VERSION" ]; then \
	  echo "" >&2; \
	  echo "ERROR: ALMA_SOURCE and ALMA_VERSION are required." >&2; \
	  echo "" >&2; \
	  echo "  Examples:" >&2; \
	  echo "    make test-release ALMA_SOURCE=stable ALMA_VERSION=10.1" >&2; \
	  echo "    make test-release ALMA_SOURCE=beta   ALMA_VERSION=10.2" >&2; \
	  echo "    make test-release ALMA_SOURCE=pungi  ALMA_VERSION=10" >&2; \
	  echo "" >&2; \
	  echo "  See 'make help' for the full list of targets and variables." >&2; \
	  echo "" >&2; \
	  exit 1; \
	fi

# ----------------------------------------------------------------- targets

# `make test-release` runs only the content tests that validate AlmaLinux
# itself (everything under tests/release/, additionally filtered by the
# ``release`` marker — one test in tests/release/test_os_release.py is a
# QEMU/binfmt sanity smoke that intentionally omits the marker so it
# does not pollute the GA artifact). Output is intentionally minimal —
# pytest's verbose listing is suppressed, only our per-file OK/FAIL
# block is printed. The detailed markdown report is written to
# reports/release-report-<UTC-timestamp>.md (timestamped so repeated
# runs don't overwrite each other; CI uses the freshest one as the
# artifact).
#
# We override pytest.ini's addopts to drop -v / -ra; conftest's
# pytest_unconfigure produces all the human-readable output.
test-release: _require-vars
	@mkdir -p reports
	$(PYTEST) tests/release -m release \
	  --no-header -v --tb=no \
	  -o "addopts=--strict-markers" \
	  --color=yes

test-release-host: _require-vars
	./scripts/run-local.sh tests/release -m "release and not requires_docker"

test-release-container: _require-vars
	./scripts/run-local.sh tests/release -m "release and requires_docker"

test-release-arch-%: _require-vars
	@case "$*" in \
	  x86_64|aarch64|s390x|ppc64le|i686|x86_64_v2) ALMA_ARCHES=$* ./scripts/run-local.sh tests/release -m release ;; \
	  *) echo "Unknown arch: $*. Use one of: x86_64 aarch64 s390x ppc64le i686 x86_64_v2" >&2; exit 1 ;; \
	esac

# `make test-internal` validates post-check itself (the tool, not the
# product). Everything under tests/internal/ — including the suite-level
# contract tests that check our own tests don't lie. Offline, no
# ALMA_* env vars needed.
test-internal:
	$(PYTEST) tests/internal

# Run a single test file by name (without the .py extension).
# Searches tests/release/, then tests/internal/.
#
# If the matched file is under tests/internal/ we skip the env-var
# guard and skip run-local.sh (which would refuse to run without
# ALMA_SOURCE/VERSION). Internal tests don't talk to AlmaLinux, so
# requiring those vars would be ceremony for ceremony's sake.
#
# Examples:
#   make test-file-test_almalinux_repos_pkg ALMA_SOURCE=stable ALMA_VERSION=10.1
#   make test-file-test_url_builder
test-file-%:
	@if [ -f "tests/release/$*.py" ]; then \
	  if [ -z "$$ALMA_SOURCE" ] || [ -z "$$ALMA_VERSION" ]; then \
	    echo "" >&2; \
	    echo "ERROR: ALMA_SOURCE and ALMA_VERSION are required for release tests." >&2; \
	    echo "  Example: make test-file-$* ALMA_SOURCE=stable ALMA_VERSION=10.1" >&2; \
	    echo "" >&2; \
	    exit 1; \
	  fi; \
	  exec ./scripts/run-local.sh "tests/release/$*.py"; \
	elif [ -f "tests/internal/$*.py" ]; then \
	  exec $(PYTEST) "tests/internal/$*.py"; \
	else \
	  echo "ERROR: $*.py not found under tests/release/ or tests/internal/." >&2; \
	  echo "Available test files:" >&2; \
	  find tests -maxdepth 2 -name 'test_*.py' 2>/dev/null | sort | sed 's|^|  |' >&2; \
	  exit 1; \
	fi

# Run tests matching a pytest -k keyword expression. The expression can
# match test names across BOTH categories, so we always go through
# run-local.sh (which requires env vars in case the match hits release
# tests). If you only want to match internal tests, just call pytest
# directly: ``pytest tests/internal -k <expr>``.
#
# Example: make test-k-dnf_install ALMA_SOURCE=stable ALMA_VERSION=10.1
test-k-%: _require-vars
	./scripts/run-local.sh -k "$*"

lint:
	ruff check .
	mypy post_check tests

setup-qemu:
	./scripts/setup-qemu.sh
