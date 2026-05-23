# Architecture × version test matrix

Single source of truth: which post-check tests run for which AlmaLinux
architecture × major. Driven entirely by
[`config/architectures.yaml`](../config/architectures.yaml) — when this
table needs an update, the YAML is what changes; the logic in
`tests/conftest.py::_skip_unsupported_arch` and the per-arch fields it
reads will pick it up automatically.

## Supported (arch, major) pairs

| Arch | AL9 | AL10 | Notes |
|---|:---:|:---:|---|
| `x86_64`    | ✅ | ✅ | Default tier; full coverage. |
| `aarch64`   | ✅ | ✅ | Full coverage. |
| `s390x`     | ✅ | ✅ | Full coverage; container tests are slow under QEMU. |
| `ppc64le`   | ✅ | ✅ | Full coverage; container tests are slow under QEMU. |
| `i686`      | ✅ | ✅ | Vault-only mirrorlist. **Skips:** container, ISO, upgrade. **Parity NOT skipped** — i686 must stay in lockstep with upstream NEVR; drift = real bug. |
| `x86_64_v2` | ❌ | ✅ | AL10 only. Image: `quay.io/almalinuxorg/almalinux:10` with `--platform=linux/amd64/v2`. |

`x86_64_v2` is rejected with a clear error if you pass it via
`ALMA_ARCHES` for an AL9 run (validated in
`RuntimeConfig.from_env`).

## Per-test coverage

The autouse fixture `_skip_unsupported_arch` in
[`tests/conftest.py`](../tests/conftest.py) skips test variants that
don't apply, based on the arch's `skip_categories` list. The mapping
from category → test file lives in the same conftest
(`_ARCH_SKIP_FILE_CATEGORIES` + the `requires_docker` keyword for
`container`).

All test files below live under `tests/release/`.

| Test file | x86_64 | aarch64 | s390x | ppc64le | i686 | x86_64_v2 (AL10) |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `test_repomd_signature.py`        | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `test_iso_checksums.py`           | ✅ | ✅ | ✅ | ✅ | ⏭️ `iso` | ✅ |
| `test_mirrorlist.py`              | ✅ | ✅ | ✅ | ✅ | ✅ (vault-only) | ✅ |
| `test_almalinux_repos_pkg.py`     | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `test_release_parity.py`          | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (AL10) |
| `test_noarch_parity.py`           | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (AL10) |
| `test_dnf_install_all.py`         | ✅ | ✅ | ✅ (slow) | ✅ (slow) | ⏭️ `container` | ✅ |
| `test_dnf_upgrade.py`             | ✅ | ✅ | ✅ | ✅ | ⏭️ `upgrade`/`container` | ✅ |
| `test_os_release.py`              | ✅ | ✅ | ✅ | ✅ | ⏭️ `container` | ✅ |

Legend:
* ✅ — runs.
* ⏭️ `<cat>` — skipped via `skip_categories: [<cat>]` in
  `config/architectures.yaml`. The skip reason in the report points
  back at the YAML so reviewers can see *why* without reading code.

## Per-arch repo set (primary sections)

`expected_pkg_sections(major, arch)` in
[`post_check/config.py`](../post_check/config.py) decides which
`[section]` IDs in `almalinux-repos`'s `.repo` files must exist for
the `(major, arch)` pair. The mirrorlist test, the
"every-section-shipped" test, and the pungi `.repo` synthesizer all
read from this single source.

| Section | x86_64 / x86_64_v2 | aarch64 / s390x / ppc64le | i686 |
|---|:---:|:---:|:---:|
| `baseos`           | ✅ | ✅ | ✅ |
| `appstream`        | ✅ | ✅ | ✅ |
| `crb`              | ✅ | ✅ | ✅ |
| `extras`           | ✅ | ✅ | ✅ |
| `highavailability` | ✅ | ✅ | ❌ |
| `resilientstorage` | AL9 ✅ / AL10 ❌ | AL9 ✅ / AL10 ❌ | ❌ |
| `rt`               | ✅ | ❌ | ❌ |
| `nfv`              | ✅ | ❌ | ❌ |
| `sap`              | ✅ | ✅ | ❌ |
| `saphana`          | ✅ | ✅ | ❌ |

`x86_64_v2` shares the same expected-section set as `x86_64` (full set
including RT/NFV) — defined as the `_ARCH_X86_FAMILY` tuple in
`config.py`.

## Container image per arch

Used by `dnf install` / `dnf upgrade` / `os-release` tests
(`post_check.helpers.docker.image_for(version, arch=…)`).

| Arch | Image template | Resolves to (AL10) |
|---|---|---|
| `x86_64`    | `almalinux:{major}` (default)                    | `almalinux:10` |
| `aarch64`   | `almalinux:{major}` (default)                    | `almalinux:10` |
| `s390x`     | `almalinux:{major}` (default)                    | `almalinux:10` |
| `ppc64le`   | `almalinux:{major}` (default)                    | `almalinux:10` |
| `i686`      | n/a — container tests are skipped                | n/a |
| `x86_64_v2` | `quay.io/almalinuxorg/almalinux:{major}`         | `quay.io/almalinuxorg/almalinux:10` |

Override via the `docker_image` field in
`config/architectures.yaml`. The template supports a single
placeholder, `{major}`, expanded from `runtime_config.version`.

The corresponding `--platform` is taken from `docker_platform` in the
same YAML entry — for `x86_64_v2` that is `linux/amd64/v2`, matching
the user-facing invocation:

```sh
docker run --platform=linux/amd64/v2 quay.io/almalinuxorg/almalinux:10
```

## How to add a new architecture

1. Add an entry under `config/architectures.yaml`. Required:
   `docker_platform`. Optional, in order of usefulness:
   `supported_majors`, `pungi_host`, `pungi_host_by_major`,
   `docker_image`, `skip_categories`, `vault_only_mirrorlist`.
   Use `pungi_host_by_major` instead of `pungi_host` when the pungi
   hostname varies across AlmaLinux majors — i686 does this because
   AL9 has no standalone i686 compose and rides under
   `x86-64-pungi-9.almalinux.dev`, while AL10 has its own
   `i686-pungi-10.almalinux.dev`.
2. If the arch has a non-default expected-section set, update the
   per-major maps in `post_check/config.py::_EXPECTED_PKG_SECTIONS`
   (or the `_ARCH_*` arch-tuples consumed by them).
3. Update this matrix.
4. No test code changes should be needed — every per-arch behaviour
   is now driven from the YAML.
