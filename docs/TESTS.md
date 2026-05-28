# post-check test catalog

> See also: [ARCH_MATRIX.md](ARCH_MATRIX.md) for which tests run on
> which architectures and AlmaLinux versions (i686, x86_64_v2, etc.).

What this suite actually verifies in a **finished AlmaLinux release**
(stable / beta / pungi). Only the substantive tests are catalogued
here — the ones that hit `repo.almalinux.org` /
`vault.almalinux.org` / `*-pungi-*.almalinux.dev`, or that boot
AlmaLinux inside Docker and validate behaviour from there. Unit tests
for internal helpers and config/CI contracts are intentionally
omitted: those describe *how the tooling works*, not *what is being
checked in the release*.

## Conventions

- **`requires_docker`** — needs a running docker daemon (and binfmt
  for non-x86 arches).
- **`slow`** — may take > 5 minutes (especially the `dnf install all`
  resolve under QEMU emulation on s390x/ppc64le).
- **`arch` fixture** — the test is run once per architecture from
  `ALMA_ARCHES`.
- **`repo` fixture** — the test is run once per repo from the
  source's default set (BaseOS, AppStream, CRB, extras,
  HighAvailability, ResilientStorage, NFV, RT, SAP, SAPHANA).

---

## tests/release/test_repomd_signature.py

GPG signature check on `repomd.xml` against the AlmaLinux release
key. Downloads `repomd.xml` and `repomd.xml.asc` for every
`(arch, repo)` pair and verifies them with the release key. A 404
(repo doesn't exist for this `source/version/arch` combination) is
treated as skip.

| Test | What it checks |
|---|---|
| `test_repomd_signature` (`arch`, `repo` fixtures) | The `repomd.xml` signature is valid under the release key for the current major. |

---

## tests/release/test_iso_checksums.py

Checks `isos/<arch>/CHECKSUM` on the mirror: the clearsigned
signature, the presence of every expected ISO flavour, and that the
ISO files themselves are actually reachable.

| Test | Markers | What it checks |
|---|---|---|
| `test_iso_checksum_file_signature_valid` (`arch` fixture) | — | `CHECKSUM` exists for every arch and contains a valid clearsigned signature from the release key. Skip on 404. |
| `test_iso_checksum_file_lists_all_expected_iso_names` (`arch` fixture) | — | `CHECKSUM` lists all three expected ISO flavours: `dvd`, `boot`, `minimal`. |
| `test_iso_files_exist_via_head` (`arch` fixture) | — | Every ISO listed in `CHECKSUM` is physically reachable (HTTP HEAD = 200, with `Content-Length`). |

---

## tests/release/test_mirrorlist.py

Tests for the public mirrorlist service (`mirrors.almalinux.org`)
**plus** `baseurl` reachability, both driven by the `.repo` files
shipped in the `almalinux-repos` package itself — the test downloads
the latest package from BaseOS, walks the **expected primary
sections** for the current `(major, arch)` (see
[`expected_pkg_sections`](../post_check/config.py)), and validates
exactly the URLs a real client would consult after installing the
package. This catches drift between the mirrorlist service, the
package, and the public mirror layout in a single pass; sections like
`-debug` / `-source` are intentionally out of scope.

Both stable and beta ship a mirrorlist (beta only ever returns
`vault.almalinux.org` — that's fine). The whole module is skipped
for pungi: it has no mirrorlist service, and the `.repo` files
shipped in pungi composes still point at the public mirror layout,
not at the per-arch pungi compose hostname, so probing those URLs
from a pungi run would be meaningless. Mirrorlist URLs returned by
the server can legitimately contain the `$basearch` placeholder
(dnf substitutes on the client) — the tests do the same substitution
on their side before checking arch presence.

| Test | What it checks |
|---|---|
| `test_mirrorlist_returns_200` (`arch` fixture) | For every expected section in the package's `.repo` files that ships `mirrorlist=`, the substituted URL returns HTTP 200 with a non-empty body. |
| `test_mirrorlist_returned_urls_contain_requested_arch` (`arch` fixture) | Every URL in every section's mirrorlist response (post `$basearch` substitution) contains the requested arch — guards against the bug where a mirrorlist for x86_64 hands back aarch64 mirrors. |
| `test_mirrorlist_returned_urls_contain_requested_version` (`arch` fixture) | Every URL in every section's response contains the requested version: for beta — `vault.almalinux.org/<version>-beta/…` (e.g. `10.2-beta`); for stable — `<version>` on real mirrors (never `vault`, never `-beta`). |
| `test_baseurl_serves_repomd_for_every_section` (`arch` fixture) | For every expected section that ships `baseurl=`, the substituted URL must directly serve `repodata/repomd.xml` (HEAD = 200). Skipped if none of the expected primary sections has a baseurl (typical for stable/beta — primary repos ship as mirrorlist-only). |
| `test_mirrorlist_each_returned_url_has_repomd_xml_reachable` | Real reachability of mirrors for the first section that ships a mirrorlist (under the first arch from `ALMA_ARCHES`): for stable ≥2 of the first 3 URLs must serve `repodata/repomd.xml`; for beta ≥1 is enough (the mirrorlist returns a single vault URL). |

---

## tests/release/test_almalinux_repos_pkg.py

Inspects the contents of the `almalinux-repos` package (plus
`almalinux-release` and `almalinux-gpg-keys` for cross-validation):
downloads the RPM from BaseOS, unpacks it in memory, and validates
every shipped `.repo` file. The package family is also published for
pungi composes, so these tests run for every source.

The expected primary repo set per major (`baseos`, `appstream`,
`crb`, `rt`, `nfv`, `resilientstorage`, `highavailability`, `extras`,
`sap`, `saphana`) is encoded in
[`post_check.config.expected_pkg_sections`](../post_check/config.py).
AL10 dropped `resilientstorage`. `rt` and `nfv` ship only on x86_64
across both majors.

| Test | What it checks |
|---|---|
| `test_almalinux_repos_pkg_writes_to_etc_yum_repos_d` | The package contains at least one `.repo` file under `/etc/yum.repos.d/`. |
| `test_almalinux_repos_pkg_ships_all_expected_sections` (`arch` fixture) | For the current `(major, arch)` combination, every primary section listed in `expected_pkg_sections` has a corresponding `[section]` block in some `.repo` file. AL9 expects 10 sections (incl. `resilientstorage`) on x86_64, 8 on non-x86 (no `rt`/`nfv`); AL10 expects 9 / 7. |
| `test_almalinux_repos_pkg_gpgcheck_is_1_for_every_repo` | Every section of every `.repo` has `gpgcheck=1`. |
| `test_almalinux_repos_pkg_no_repo_with_sslverify_disabled` | Nowhere is `sslverify=0` set. |
| `test_almalinux_repos_pkg_each_baseurl_returns_repomd` (`arch` fixture) | For every enabled section with a `baseurl`, HEAD on `repodata/repomd.xml` returns 200 — i.e. the package contains no references to non-existent repos. Skipped for pungi (package's `.repo` files point at the public layout, not the per-arch pungi compose hosting) and for pulp (baseurls are the stable-major public URLs, not the internal-beta layer that carries the target-minor packages). |
| `test_almalinux_repos_pkg_each_gpgkey_path_exists_in_release_pkg` | Every `gpgkey=file:///etc/pki/rpm-gpg/X` is physically shipped by either `almalinux-release` or `almalinux-gpg-keys` (in AL10 the keys were split out into a separate package). |

---

## tests/release/test_release_parity.py

Cross-arch parity for the `almalinux-release` and `almalinux-repos`
packages within a single release. The contract: the
`(name, epoch, version, release)` tuple must match across **every**
published arch. Arch and binary checksum are deliberately **not**
part of the invariant — AL10 ships these packages per-arch.

The test always iterates over the full architecture matrix from
`config/architectures.yaml`, regardless of `ALMA_ARCHES` — parity is
a property of the release, not of the operator's local subset.

Per-source URL the parity check reads from:

* `stable` / `beta` / `pungi` — `BaseOS` of the release-under-test.
* `pulp` — the layered **internal-beta** repo on
  `build.almalinux.org/pulp/content/...` (flat, per-arch). That's the
  layer where the upgrade-target build of `almalinux-release` actually
  lives (`version == ALMA_VERSION`); the major-aliased stable layer
  pulp also pulls in carries the current GA and is already covered by
  `stable` runs.

| Test | What it checks |
|---|---|
| `test_release_pkg_same_nevr_across_arches` (parametrized over `pkg_name=[almalinux-release, almalinux-repos]`) | Collects `(N, E, V, R)` for each package on every arch; all tuples must match. The package must be present on every arch — no silent skips. |
| `test_almalinux_release_version_matches_input_version` | `almalinux-release.version` matches `ALMA_VERSION` (for major-only pungi the comparison is by major). |

---

## tests/release/test_noarch_parity.py

Cross-arch parity for noarch packages **across all repos** in a
release. Two complementary invariants are checked together — both
are properties of "the release is consistent across arches":

* **(A) Same NEVRA → same sha256.** A NEVRA that appears in ≥2
  `(repo, arch)` cells must hash to the same bytes everywhere.
  Catches a noarch RPM that was rebuilt or replaced under the same
  NEVRA on one arch but not on others.
* **(B) Same `(repo, name)` → same latest EVR (and sha256) across
  arches.** For every `(repo, name)` where a noarch is present in
  ≥2 arches, the *latest* EVR in each arch must match. Catches a
  publication bug where a new build of `foo` made it to one arch's
  repodata but not another's, leaving `dnf install foo` to pull
  different versions on x86_64 vs aarch64. Invariant (A) doesn't
  catch this on its own — the two NEVRAs are different, so each
  lives in only one cell.

The test runs over the full repo set of the source AND the full
architecture matrix from `config/architectures.yaml` — neither
`ALMA_REPOS` nor `ALMA_ARCHES` narrow it down.

Per-source URL the noarch parity check reads from:

* `stable` / `beta` / `pungi` — the release-under-test's named repos
  (BaseOS / AppStream / …).
* `pulp` — the layered **internal-beta** repo on
  `build.almalinux.org/pulp/content/...`. The major-aliased stable
  layer pulp also pulls in carries the current GA (already covered by
  `stable` runs); the internal-beta is flat, so the `(repo, arch)`
  grid collapses to one synthetic repo cell per arch.

Legitimate exclusions (not flagged by either invariant):
arch-specific noarch like `syslinux-*` on x86_64 (name in exactly
one arch); stale historical builds left on one arch (B compares only
the *latest* EVR per arch,
so a leftover doesn't fail the test as long as the current build is
in sync).

| Test | What it checks |
|---|---|
| `test_noarch_packages_identical_across_arches_across_all_repos` | Downloads `primary.xml` for every `(repo, arch)` pair across the full repo set. Builds `{NEVRA → {(repo,arch): sha256}}` for invariant (A) and `{(repo, name) → {arch: (latest_evr, sha256)}}` for invariant (B), the latter using `post_check.helpers.rpm_evr.evr_cmp` to pick the latest EVR per arch. Reports each invariant's failures in a separate block so the diagnosis is unambiguous (rebuilt-without-bump vs publication-skew). |

---

## tests/release/test_srpm_version_consistency.py

Per-arch SRPM version consistency across all repos of a release. For
every published architecture, every binary RPM must reference exactly
one version of any given source RPM name — a release that ships two
binaries built from different versions of the same SRPM is
"split-brain" and dnf would resolve subpackages inconsistently.

Consolidates the per-arch
`check-<arch>-compose-srpm-versions-<major>.py` scripts from
`releng-almalinux/tools/` into a single test that loops the full
architecture matrix. Modular packages (`.module` in release) are
excluded — module streams have their own per-stream lifecycle and
routinely carry coexisting versions in repodata (same rationale as
`test_noarch_parity._is_modular`). Debug repositories are out of scope
for this first cut: debug binaries share their SRPM with the
corresponding main binary, so an inconsistency in debug almost always
also surfaces in main.

Always iterates the full architecture matrix from
`config/architectures.yaml` AND the full repo set of the source —
consistency is a property of the release, not of `ALMA_ARCHES` /
`ALMA_REPOS`. On `pulp`, the named repo set collapses to the flat
per-arch internal-beta URL.

| Test | What it checks |
|---|---|
| `test_srpm_versions_consistent_within_each_arch` | For every published arch, no source RPM name is referenced by more than one EVR. On failure, the report lists the newest SRPM, every older SRPM, and the binary RPM filenames built from those older SRPMs (those are the files an operator must delete from the repository to clear the drift). |

---

## tests/release/test_os_release.py

Integration checks for `/etc/os-release` and
`/etc/almalinux-release` inside containers across all four
architectures. `requires_docker` is applied via `pytestmark`.

| Test | What it checks |
|---|---|
| `test_run_in_arch_executes_uname_m` (`arch` fixture) | `uname -m` inside the container reports exactly the requested arch (sanity check for emulation). |
| `test_release_files_match_target_after_upgrade` (`arch` fixture) | Boots `almalinux:<major>`, mounts the target release's own `.repo` files (extracted from the `almalinux-repos` package — no URL is generated locally), runs `dnf upgrade -y`, then asserts `/etc/os-release` reports `VERSION_ID="<major.minor>"` (for stable/beta) or `startswith(<major>)` (for pungi), and `/etc/almalinux-release` mentions the same version. The minor-level invariant lives here (the bare `almalinux:<major>` Docker tag points at the *latest released* minor, which won't match a pre-GA version under test). |
| `test_qemu_binfmt_check_documents_required_setup` (`arch` fixture) | Smoke `docker run` on a non-x86_64 arch; on failure it prints a hint about `tonistiigi/binfmt` / `setup-qemu.sh`. Skipped for x86_64. **Infrastructure check, not a product test — does not carry the `release` marker and is excluded from the release report.** |

---

## tests/release/test_dnf_install_all.py

M2 test: `dnf install '*' --skip-broken --assumeno` inside a
container for every architecture — this is a **resolve-only** run,
dnf does the full depsolve and prints the `Skipped packages were:`
block but downloads and installs nothing. The block is parsed and
diffed against a per-major allowlist
(`tests/data/allowed_install_failures-<major>.yaml`). Any package
that didn't resolve and is **not** on the allowlist — fail.
`requires_docker`. The `slow` marker is added automatically on
`s390x` / `ppc64le`. Timeout: 10 minutes on every arch (depsolve
without download or install fits within that even under QEMU).

| Test | Markers | What it checks |
|---|---|---|
| `test_dnf_install_all_for_arch` | `requires_docker`, `arch` fixture | Mounts the generated `.repo` and GPG key, runs `dnf install --skip-broken --assumeno --setopt=install_weak_deps=False '*'` inside `almalinux:<major>` (major-only tag: per-minor tags don't exist for beta at all, and for stable the post-check runs *before* the release ships, while the per-minor tag isn't published yet). Parses the log via `dnf_log.parse_skipped`; any package outside its major's allowlist — fail. |

---

## tests/release/test_dnf_upgrade.py

Real `dnf upgrade` from the previous AlmaLinux minor to the current
one. `requires_docker`. The previous version is computed
automatically from `ALMA_VERSION` (`10.2 → 10.1`); for `x.0` or
major-only versions the test is skipped.

| Test | Markers | What it checks |
|---|---|---|
| `test_dnf_upgrade_changes_os_release_to_target` | `requires_docker`, `arch` fixture | Boots a container with the previous minor, mounts the target repo and GPG key, runs `dnf upgrade -y`. After the upgrade: `/etc/os-release` reports `VERSION_ID="<target>"` and the `almalinux-release-<target>` package is installed. |

---

## Mapping to the contract items (from README)

| Item | Test file |
|---|---|
| 1. All packages installable (`dnf install all`) | `tests/release/test_dnf_install_all.py` |
| 2. `almalinux-repos` repository check | `tests/release/test_almalinux_repos_pkg.py` |
| 3. `/etc/os-release` version | `tests/release/test_os_release.py` |
| 4. `dnf upgrade` from the previous minor | `tests/release/test_dnf_upgrade.py` |
| 5. Mirrorlist 200 + URL for every arch | `tests/release/test_mirrorlist.py` |
| 6. ISO checksums valid and signed | `tests/release/test_iso_checksums.py` |
| 7. `repomd.xml` signatures | `tests/release/test_repomd_signature.py` |
| 8. noarch package versions identical across arches | `tests/release/test_noarch_parity.py` |
| 9. `almalinux-release/repos` with the same N-E-V-R on every arch | `tests/release/test_release_parity.py` |
| 10. SRPM version consistency within each arch (no split-brain on subpackages) | `tests/release/test_srpm_version_consistency.py` |

## How many tests run in each configuration

Numbers from the most recent run (`-m "not requires_docker"`, 4 arches):

| Release | passed | skipped | failed |
|---|---:|---:|---:|
| `stable 10.1` | 257 | 19 | 0 |
| `pungi 10` | 157 | 106 | 0 |
| `beta 10.2` | 164 | 99 | 0 |
| `beta 9.8` | 169 | 94 | 0 |

Skips are legitimate: missing repos (NFV/RT/ResilientStorage on
non-x86), no mirrorlist for pungi, etc.
