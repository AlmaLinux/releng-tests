# post-check

Post-release validation suite for AlmaLinux. Verifies that a finished
release (stable / beta / pungi compose) is internally consistent and
externally reachable: repository signatures, ISO checksums, mirrorlist
service, package parity across arches, and a real `dnf install` /
`dnf upgrade` inside containers for every supported architecture.

For the **per-test breakdown** — what each file asserts, which fixtures
it parametrises over, and when it skips — see
[`docs/TESTS.md`](docs/TESTS.md).

---

## Quickstart

```bash
# 1. Install dependencies (Python 3.12+).
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Pick a release to validate.
make test-release ALMA_SOURCE=stable ALMA_VERSION=10.1     # repo.almalinux.org
make test-release ALMA_SOURCE=beta   ALMA_VERSION=10.2     # vault.almalinux.org
make test-release ALMA_SOURCE=pungi  ALMA_VERSION=10       # per-arch compose host
```

Output: short OK/FAIL summary on stdout, full markdown report at
`reports/release-report-<UTC-timestamp>.md`.

Bare `make` (no target) prints the full help.

---

## Test categories

The suite is split into two folders:

| Folder | What it validates | Needs ALMA_* env |
|---|---|---|
| `tests/release/` | The AlmaLinux release itself — signatures, ISOs, mirrorlist, package parity, real `dnf` runs. **The product under test.** | yes |
| `tests/internal/` | post-check itself — helper parsers, the docker wrapper, URL builders, plus suite-level contract tests that pin our own assertions, markers, and skip logic. **The tool, not the product.** | no |

## Common targets

All release-content targets share the `test-release-` prefix; everything
that validates post-check itself runs under `test-internal`. There is no
unified "run everything" target — pick the category you want.

| Command | What it runs | Needs Docker | Needs network |
|---|---|---|---|
| `make test-release` | Full release-content suite over `tests/release/`. Produces the GA artifact under `reports/`. | yes (one container bucket) | yes |
| `make test-release-host` | Release tests minus the container-bound ones. Good for fast iteration. | no | yes |
| `make test-release-container` | Release tests that need Docker only (`dnf install all`, upgrade, `/etc/os-release`). | yes | yes |
| `make test-release-arch-<arch>` | Release tests pinned to one arch (`x86_64` / `aarch64` / `s390x` / `ppc64le` / `i686` / `x86_64_v2`). | yes | yes |
| `make test-internal` | Every test under `tests/internal/` — helpers, parsers, suite-level contracts. Offline. | no | no |
| `make test-file-<name>` | One test file by basename. Searches `tests/release/`, then `tests/internal/`. Env vars required only if the match is in `tests/release/`. Example: `make test-file-test_noarch_parity`. | depends | depends |
| `make test-k-<expr>` | Pytest `-k` expression — substring match across test names, can hit both categories. Example: `make test-k-dnf_install`. | depends | depends |
| `make lint` | `ruff` + `mypy`. | no | no |
| `make setup-qemu` | Register `binfmt_misc` so non-x86 containers run under QEMU. | yes | no |

`test-internal`, `lint`, `setup-qemu`, and `help` run without any env
vars. Every `test-release-*` target requires `ALMA_SOURCE` /
`ALMA_VERSION` — running without them prints a clear error and exits
non-zero.

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `ALMA_SOURCE` | yes | — | `stable` (repo.almalinux.org), `beta` (vault.almalinux.org), or `pungi` (per-arch compose hostname). |
| `ALMA_VERSION` | yes | — | `major.minor` for stable/beta (e.g. `10.1`); `major` only for pungi (e.g. `10`). |
| `ALMA_ARCHES` | no | `x86_64,aarch64,s390x,ppc64le` | CSV of architectures to test. Optional extras: `i686` (AL9, AL10) and `x86_64_v2` (AL10 only). See [`docs/ARCH_MATRIX.md`](docs/ARCH_MATRIX.md) for what runs per arch+version. |
| `ALMA_REPOS` | no | full repo set per source | CSV of repos: `BaseOS,AppStream,CRB,extras,HighAvailability,ResilientStorage,NFV,RT,SAP,SAPHANA`. Parity tests always span the full set regardless of this — they describe a property of the release. |

The previous minor for `dnf upgrade` is computed automatically from
`ALMA_VERSION` (`10.2 → 10.1`, `10.1 → 10.0`). On `x.0` and major-only
versions the upgrade test is skipped with a clear reason.

---

## What it checks (release contract)

The release-content suite is the operational definition of "AlmaLinux
release is ready to ship." Each item below maps to one test file —
follow the link for the full assertion list.

| # | Item | Test file |
|---|---|---|
| 1 | All packages resolve (`dnf install '*'`, resolve-only) | [`tests/release/test_dnf_install_all.py`](tests/release/test_dnf_install_all.py) |
| 2 | `almalinux-repos` package contents | [`tests/release/test_almalinux_repos_pkg.py`](tests/release/test_almalinux_repos_pkg.py) |
| 3 | `/etc/os-release` reports the target version | [`tests/release/test_os_release.py`](tests/release/test_os_release.py) |
| 4 | `dnf upgrade` from the previous minor | [`tests/release/test_dnf_upgrade.py`](tests/release/test_dnf_upgrade.py) |
| 5 | Mirrorlist returns 200 + URLs for every arch | [`tests/release/test_mirrorlist.py`](tests/release/test_mirrorlist.py) |
| 6 | ISO checksums valid and clearsigned | [`tests/release/test_iso_checksums.py`](tests/release/test_iso_checksums.py) |
| 7 | `repomd.xml` GPG signatures | [`tests/release/test_repomd_signature.py`](tests/release/test_repomd_signature.py) |
| 8 | noarch packages identical across arches (NEVRA → sha256, name → latest EVR) | [`tests/release/test_noarch_parity.py`](tests/release/test_noarch_parity.py) |
| 9 | `almalinux-release` / `almalinux-repos` have the same N-E-V-R on every arch (arch and binary checksum intentionally differ — these packages are built per-arch on AL10) | [`tests/release/test_release_parity.py`](tests/release/test_release_parity.py) |

→ **Detailed catalogue:** [`docs/TESTS.md`](docs/TESTS.md).
→ **Architecture × version matrix:** [`docs/ARCH_MATRIX.md`](docs/ARCH_MATRIX.md).

---

## Reports

`make test-release` writes a timestamped markdown report to
`reports/release-report-<UTC-timestamp>.md`. CI uploads the freshest
one as the workflow artifact.

The terminal output during a release run is intentionally minimal
(per-file OK/FAIL block + a final tally). Pytest's verbose dump goes
into the markdown report so it doesn't drown the human-readable
summary.

---

## Running in CI

GitHub Actions → **Actions → Release Tests → Run workflow**. The
workflow exposes `source` / `version` / `arches` as inputs and
publishes the markdown report and JUnit XML as artifacts. Per-arch
`s390x` / `ppc64le` jobs run under QEMU emulation (`tonistiigi/binfmt`
is registered automatically in the workflow setup step).

---

## Requirements

- **Python 3.12+** (3.14 also works).
- **Docker** — only for container tests (`test-container`,
  `test_dnf_install_all`, `test_dnf_upgrade`, `test_os_release`).
  Host-only checks run fine without it.
- **gpg** — for repomd / ISO signature verification (preinstalled on
  most distros; if missing, signature tests skip with a clear reason).
- **QEMU binfmt** — only for running non-x86 containers on an x86 host
  (`make setup-qemu` does the registration).

---

## Project layout

```
post_check/        Library code (config loaders, helpers: HTTP, GPG,
                   repodata, RPM extraction, RPM EVR comparison, etc.).
tests/             Test suite, split by what each test validates:
  release/         Tests of the AlmaLinux release itself (the product).
                   Each file maps to one contract item — see table above.
  internal/        Tests of post-check itself (the tool) — helper
                   parsers, the docker wrapper, URL builders, plus
                   suite-level contracts (every test has an assertion,
                   markers don't lie, every file imports cleanly).
                   Offline, no env vars.
  conftest.py      Shared fixtures (runtime_config, arch, repo).
  conftest_data.py Synthetic repodata builders shared across tests.
config/            architectures.yaml — published-arch matrix per
                   release. The source registry (stable / beta / pungi)
                   lives in post_check/config.py.
scripts/           run-local.sh, setup-qemu.sh, merge-release-reports.py.
docs/TESTS.md      Per-test catalogue (what each test asserts).
reports/           (gitignored) timestamped release reports.
```
