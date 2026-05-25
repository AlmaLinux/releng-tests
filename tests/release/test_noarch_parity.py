"""Parity of noarch packages across arches across ALL repos.

Two complementary invariants — both checked by the same test, since
both are properties of "the release is consistent across arches":

(A) **Same NEVRA, same bytes.** A given NEVRA must have the same
    sha256 wherever it appears -- across arches of one repo, AND
    across different repos within the same release. Catches a
    noarch RPM that was rebuilt or replaced under the same NEVRA
    in one arch but not in others.

(B) **Same name, same latest build.** For every ``(repo, name)``
    where the noarch is present in >=2 arches, the *latest* EVR
    available in each arch must match (and -- equivalently --
    have the same sha256). Catches a publication bug where a
    new build of ``foo`` made it to one arch's repodata but not
    to another's, leaving ``dnf install foo`` to pull different
    versions on x86_64 vs aarch64. Invariant (A) doesn't catch
    this on its own: the two NEVRAs are different, so each lives
    in only one cell, and (A)'s "appears in >=2 cells" precondition
    skips them both.

Both invariants span the **full repo set** of the source (BaseOS,
AppStream, CRB, extras, HighAvailability, ResilientStorage, NFV,
RT, SAP, SAPHANA) and the **full architecture matrix** from
``config/architectures.yaml`` -- parity is a property of the release
itself, not of the operator's ``ALMA_ARCHES`` / ``ALMA_REPOS`` subset.

Per-source URL where the noarch parity check reads from:

* ``stable`` / ``beta`` / ``pungi`` — the named repos of the
  release-under-test (BaseOS / AppStream / …), built by
  :class:`RepoURL`.
* ``pulp`` — the layered **internal-beta** repo on
  ``build.almalinux.org/pulp/content/...``. The major-aliased stable
  layer pulp also pulls in carries the current GA and is already
  validated by ``stable`` runs — re-checking it here would just
  produce duplicate "passed" rows without testing the upgrade target.
  The internal-beta is **flat** (no per-component split), so the
  ``(repo, arch)`` grid collapses to a single synthetic repo cell
  per arch.

Legitimate exclusions (not flagged):

* **Modular packages** (``.module`` in the release tag, e.g.
  ``apache-commons-cli-1.9.0-4.module_el9.6.0+148+fb6dc857``).
  Module streams have their own per-arch publication lifecycle —
  modules aren't shipped to i686 at all on AL9, and even between
  the main four arches a stream can be unpublished/replaced
  independently of the base release. Treating modular drift as a
  parity failure would mean every module on a multi-arch release
  would flag i686 as "drifting" — which is a property of the
  modularity contract, not a release bug. Filtered out at
  collection time in :func:`_is_modular`.
* **Arch-specific noarch.** ``syslinux-nonlinux``,
  ``efi-filesystem`` etc. only make sense on x86_64 and ship there
  only. The name appears in exactly one arch, so (B) skips it; the
  NEVRA appears in exactly one cell, so (A) skips it.
* **Stale historical builds on one arch.** AppStream/CRB sometimes
  keep an outdated release on a single arch (e.g. x86_64 still has
  ``virt-v2v-bash-completion-2.8.1-8.el10`` from 10.0). (B) compares
  only the *latest* EVR per arch, so the leftover doesn't fail the
  test as long as the current build is in sync. (A) ignores it
  because that NEVRA is only in one cell.
"""

from collections.abc import Iterable

import pytest

from post_check.config import (
    arch_supports_major,
    load_architectures,
    load_noarch_parity_known_drift,
    load_sources,
)
from post_check.helpers import http, repodata
from post_check.helpers.rpm_evr import evr_cmp, evr_str
from post_check.helpers.url_builder import RepoURL, pulp_internal_beta_repo_base

pytestmark = [pytest.mark.online, pytest.mark.release]


# Synthetic single-component "repo" used as the cell key for the pulp
# internal-beta on the (repo, arch) grid. The internal-beta is a flat
# per-arch repo with no BaseOS / AppStream split, so the per-component
# dimension collapses to one — but ``compute_noarch_parity_failures``
# still needs *some* repo key to slot each (arch, package) into. Using
# a fixed string keeps the renderer identifying the cell consistently
# across arches and matches the ``.repo`` section name we ship in
# :func:`post_check.helpers.repo_inject.render_pulp_internal_beta_repo_file`.
_PULP_REPO_CELL = "pulp-internal-beta"


def _strip_src_rpm_suffix(s: str) -> str:
    """``foo-1.0-1.el10.src.rpm`` → ``foo-1.0-1.el10``. Empty stays empty.

    Used as a stable group key for "packages built from the same source"
    in the (B) failure renderer. We strip the trailing ``.src.rpm`` so
    the displayed identifier reads like an N-V-R the operator already
    recognises (``gnome-shell-extensions-49.0-2.el10``) instead of a
    full filename.
    """
    if s.endswith(".src.rpm"):
        return s[: -len(".src.rpm")]
    return s


def _classify_b_cause(arch_map: dict[str, tuple[tuple[str, str, str], str, str]]) -> str:
    """Single-line cause label for a (B) failure. Used as the group header.

    Two real-world causes for a (repo, name) pair to fail (B):

    * **version skew** — at least two arches have different *latest*
      EVRs. Typical real-world trigger: a rebuild landed in some arches'
      composes but not in others.
    * **rebuild without version bump** — every arch has the *same*
      latest EVR but the bytes differ. Typical trigger: the binary was
      re-published into one arch's repo without bumping the release
      tag, so ``dnf`` clients on different mirrors get different files
      under the same NEVRA.

    A clean release is silent; one of these two labels is what the
    operator sees on a real failure.
    """
    evrs = {evr for evr, _sha, _srpm in arch_map.values()}
    if len(evrs) > 1:
        return "version skew (latest EVR differs between arches)"
    return "rebuild without version bump (same EVR, different sha256)"


def compute_noarch_parity_failures(
    cells: dict[tuple[str, str], Iterable[repodata.Package]],
    known_drift_b: dict[tuple[str, str], frozenset[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """Pure check function — fed `(repo, arch) -> [Package]`, returns
    `(failures_A, failures_B)` as lists of human-readable lines.

    Split out from the test body so meta-tests can mutate inputs and
    prove each invariant fires on the right shape of breakage and stays
    silent on the legal-but-asymmetric shapes (arch-specific noarch,
    stale historical builds). The online test wraps this with the I/O
    that fetches the cells.

    Output structure for invariant (B):

    * one summary line at the top with overall counts;
    * one block per *group* — packages from the same source RPM with
      the same arch→EVR pattern are collapsed into a single entry,
      because in practice they all break together (a single rebuild
      misses an arch and 20+ subpackages flip simultaneously);
    * inside each block: the cause label, the SRPM(s) involved, the
      per-arch EVR breakdown, and the list of affected binary names.

    The legacy ``arch=evr_str`` substring (``s390x=1.1-1.el10``) is
    preserved on the per-arch lines so meta-tests grepping for it keep
    passing.

    ``known_drift_b``
    -----------------
    Optional ``dict[(repo, name), frozenset[arches]]`` describing (B)
    violations the operator has accepted as "won't be rebuilt", scoped
    by arch. The suppression rule is:

        subtract ``allowed_arches`` from the cell — if the remaining
        arches all agree on (EVR, sha), the drift matches what was
        accepted and we silence it; otherwise the drift is wider
        than what was accepted and we fire.

    So listing ``("AppStream", "ant"): {"i686"}`` silences the i686 lag
    on AppStream/ant but DOES NOT silence a hypothetical future
    ppc64le rebuild of ``ant`` that misses x86_64 — the remaining
    {x86_64, aarch64, ppc64le, s390x} would still disagree after
    removing i686, so the test fires.

    The suppression applies ONLY to (B). Invariant (A) (same NEVRA,
    different sha256) is a stronger signal (mirror divergence /
    re-publish without bump) and is intentionally left at full
    strength regardless of this map. Loaded from
    ``config/noarch_parity_known_drift.yaml`` by the online test;
    meta-tests pass it directly.
    """
    known_drift_b = known_drift_b or {}
    # (A) NEVRA -> {(repo, arch): (sha256, sourcerpm)}
    nevra_to_cells: dict[str, dict[tuple[str, str], tuple[str, str]]] = {}
    # (B) (repo, name) -> arch -> (evr_tuple, sha256, sourcerpm)
    #     Only the highest EVR per arch is kept (mimics what dnf would install).
    per_name: dict[
        tuple[str, str],
        dict[str, tuple[tuple[str, str, str], str, str]],
    ] = {}

    for (repo, arch), pkgs in cells.items():
        for pkg in pkgs:
            # (A) NEVRA-keyed
            nevra_to_cells.setdefault(pkg.nevra, {})[(repo, arch)] = (
                pkg.checksum,
                pkg.sourcerpm,
            )
            # (B) (repo, name)-keyed, keep the highest EVR per arch
            key = (repo, pkg.name)
            evr = (pkg.epoch or "0", pkg.version, pkg.release)
            arch_map = per_name.setdefault(key, {})
            prev = arch_map.get(arch)
            if prev is None or evr_cmp(evr, prev[0]) > 0:
                arch_map[arch] = (evr, pkg.checksum, pkg.sourcerpm)

    # ---------- (A) NEVRA -> sha256 parity ---------------------------------
    # Same NEVRA, different bytes — typically a rebuild that was published
    # under the same version. Display keeps the legacy NEVRA-first format
    # (meta-tests grep for the NEVRA string) but adds the per-arch sha256
    # prefix so the operator can eyeball which arches diverged.
    failures_a: list[str] = []
    nevra_shared = 0
    for nevra, cell_data in nevra_to_cells.items():
        if len(cell_data) < 2:
            # Only one cell — not a parity case (legal arch-specific
            # noarch, or stale historical build). Skipped.
            continue
        nevra_shared += 1
        shas = {sha for sha, _srpm in cell_data.values()}
        if len(shas) > 1:
            # Group cells by sha so identical-bytes arches collapse:
            #   ``aaaa…: x86_64, s390x   bbbb…: aarch64``
            sha_to_cells: dict[str, list[str]] = {}
            for (repo, arch), (sha, _srpm) in cell_data.items():
                sha_to_cells.setdefault(sha, []).append(f"{repo}/{arch}")
            rendered = "; ".join(
                f"{sha[:12]}… on {sorted(cells_)}"
                for sha, cells_ in sorted(sha_to_cells.items(), key=lambda kv: -len(kv[1]))
            )
            failures_a.append(f"  {nevra}: {rendered}")
    if failures_a:
        failures_a = [
            f"[A] same NEVRA must have same sha256 — "
            f"{len(failures_a)} of {nevra_shared} shared NEVRA(s) failed:",
            *failures_a[:20],
        ]

    # ---------- (B) (repo, name) -> latest EVR parity ----------------------
    # Collect raw failures first, then group by (repo, srpm-pattern) so
    # 20+ subpackages of one SRPM that broke together render as a single
    # block instead of 20 near-identical lines.
    failures_b_raw: list[tuple[str, str, dict[str, tuple[tuple[str, str, str], str, str]]]] = []
    name_shared = 0
    suppressed_b = 0
    for (repo, name), arch_map in per_name.items():
        if len(arch_map) < 2:
            # Name in exactly one arch — legal arch-specific noarch. Skip.
            continue
        name_shared += 1
        # All arches must agree on (latest EVR, sha256). Comparing the
        # full tuple subsumes both "same version" and "same bytes".
        distinct = {(evr, sha) for (evr, sha, _srpm) in arch_map.values()}
        if len(distinct) > 1:
            # ``known_drift_b`` is an explicit (repo, name) -> allowed
            # arches map (see config/noarch_parity_known_drift.yaml).
            # We only suppress AFTER the parity comparison flagged the
            # pair — so listing a non-drifting name in the YAML is a
            # no-op, never a source of false-pass. (A) is untouched:
            # a same-NEVRA / different-sha violation on an allowlisted
            # name still fires.
            allowed = known_drift_b.get((repo, name))
            if allowed is not None:
                # Subtract the allowed arches and re-check parity on
                # what's left. If the remaining arches agree (or there
                # are none — operator explicitly accepted every arch),
                # the drift is exactly what was accepted → suppress.
                # If the remaining arches still disagree, the drift
                # extends beyond the accepted set → fire as a fresh
                # release bug (e.g. ant suddenly drifts on ppc64le
                # too, with only i686 in the allowlist).
                remaining = {
                    (e, s)
                    for arch, (e, s, _srpm) in arch_map.items()
                    if arch not in allowed
                }
                if len(remaining) <= 1:
                    suppressed_b += 1
                    continue
            failures_b_raw.append((repo, name, arch_map))

    if not failures_b_raw:
        return failures_a, []

    # Group key:
    #   (repo, frozenset((arch, srpm-N-V-R))) — packages from the same
    #   SRPM with the same arch→SRPM mapping land in one group. When
    #   sourcerpm is missing (rare, stripped metadata), we degrade to
    #   keying by ``(arch, evr_str(evr))`` so the grouping still works.
    groups: dict[
        tuple[str, frozenset[tuple[str, str]]],
        list[tuple[str, dict[str, tuple[tuple[str, str, str], str, str]]]],
    ] = {}
    for repo, name, arch_map in failures_b_raw:
        fp_items = []
        for arch, (evr, _sha, srpm) in arch_map.items():
            marker = (
                _strip_src_rpm_suffix(srpm)
                if srpm
                else f"_no_srpm_:{evr_str(evr)}"
            )
            fp_items.append((arch, marker))
        groups.setdefault((repo, frozenset(fp_items)), []).append((name, arch_map))

    # Compact summary: one headline line, then one block per group.
    # The block puts the noarch package names FIRST — they are what
    # the operator actually needs to know broke. SRPM is just the
    # grouping mechanism: 20 subpackages of one source RPM that
    # rebuilt out-of-sync land in one block, surfaced as a parenthetical
    # ("all from SRPM <name>") in the header. The per-arch EVR
    # breakdown follows the package list.
    #
    # When ``known_drift_b`` silenced any pairs, surface the count in
    # the headline so the operator can tell "5 of 7 real" from "5 of 5
    # real with 60 accepted drifts" — important when triaging whether a
    # new failure is a regression or a fresh entry needing the YAML.
    suppressed_note = (
        f" ({suppressed_b} suppressed by known-drift allowlist)"
        if suppressed_b
        else ""
    )
    failures_b: list[str] = [
        f"[B] noarch parity: "
        f"{len(failures_b_raw)} of {name_shared} noarch package(s) differ "
        f"across arches in {len(groups)} group(s).{suppressed_note}"
    ]
    # Sort groups: by repo first, then by descending size (biggest
    # blast-radius surfaces at the top of the report).
    for (repo, _fp), members in sorted(
        groups.items(), key=lambda kv: (kv[0][0], -len(kv[1]))
    ):
        # Within a group every member shares the same arch→EVR shape
        # (that's literally the group key), so any one member is
        # representative. Per-package sha256 still varies — we don't
        # surface it here, invariant (A) covers "same NEVRA different
        # bytes" separately.
        _name, sample_map = members[0]
        cause_short = (
            "version skew"
            if len({evr for evr, _s, _r in sample_map.values()}) > 1
            else "rebuild without version bump"
        )
        member_names = sorted(name for name, _ in members)

        # Group arches by their EVR: ``[arch1, arch2]: EVR`` lines.
        # On a real version-skew this collapses 4-6 arches into 2 lines
        # ("here's the old version, here's the new"); on a per-arch
        # one-off it expands to one line per arch.
        evr_to_arches: dict[str, list[str]] = {}
        for arch, (evr, _sha, _srpm) in sample_map.items():
            evr_to_arches.setdefault(evr_str(evr), []).append(arch)
        # Sort by group size desc so the largest cohort goes first.
        evr_lines = sorted(
            evr_to_arches.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )

        failures_b.append("")
        if len(members) == 1:
            # Single noarch package — header carries ``repo/name``,
            # body lists the full NVRA observed on each arch-group
            # (one NVRA per distinct EVR seen). The arch is always
            # ``.noarch`` for the noarch parity test, so spelling
            # it out in the NVRA is the convention rpm tooling uses.
            name = members[0][0]
            failures_b.append(f"  {repo}/{name} — {cause_short}")
            for evr_s, arches in evr_lines:
                nvra = f"{name}-{evr_s}.noarch"
                arches_list = ", ".join(sorted(arches))
                failures_b.append(f"      {nvra}  on [{arches_list}]")
        else:
            # Multi-package group: NOARCH PACKAGES are the headline.
            # Within a group every member shares the same SRPM name
            # (different SRPM versions per arch are still the same
            # source package), so we surface the SRPM as a header
            # parenthetical: it explains "these 20 broke because the
            # gnome-shell-extensions rebuild missed one arch" without
            # making the SRPM itself look like the failing thing.
            srpm_names = set()
            for _evr, _sha, srpm in sample_map.values():
                if srpm:
                    srpm_names.add(
                        _strip_src_rpm_suffix(srpm).rsplit("-", 2)[0]
                    )
            srpm_note = (
                f" from SRPM `{next(iter(srpm_names))}`"
                if len(srpm_names) == 1
                else ""
            )
            failures_b.append(
                f"  {repo} — {cause_short} "
                f"({len(members)} noarch packages{srpm_note}):"
            )
            # NVRAs grouped by arch-cohort. Within a multi-package
            # group every member shares the same arch→EVR pattern,
            # so each cohort has exactly one NVRA per binary name —
            # which makes the "what version where" answer a single
            # block of NVRA lines under each arch-cohort header.
            for evr_s, arches in evr_lines:
                arches_list = ", ".join(sorted(arches))
                failures_b.append(f"      on [{arches_list}]:")
                for name in member_names[:50]:
                    nvra = f"{name}-{evr_s}.noarch"
                    failures_b.append(f"        - {nvra}")
                if len(member_names) > 50:
                    failures_b.append(
                        f"        … and {len(member_names) - 50} more"
                    )

    return failures_a, failures_b


def _all_arches(major: str | None = None) -> tuple[str, ...]:
    """Every architecture published for AlmaLinux ``major``.

    Parity is a property of the release, not of the operator's
    ``ALMA_ARCHES`` subset, so we always span the full matrix.
    Arches whose ``supported_majors`` doesn't include ``major`` are
    filtered out (e.g. ``x86_64_v2`` is AL10-only).

    Note: ``skip_categories`` is intentionally NOT consulted here.
    Every published arch — including i686 — must satisfy noarch
    parity; an arch-specific rebuild that drifts from the rest of
    the matrix is precisely the release-time bug this test exists
    to catch.
    """
    arches = sorted(load_architectures().keys())
    if major is not None:
        arches = [a for a in arches if arch_supports_major(a, major)]
    return tuple(arches)


def _is_modular(pkg: repodata.Package) -> bool:
    """True if ``pkg`` was built as part of a DNF *module* (modular content).

    Modular RPMs carry a ``.module`` segment in their release tag,
    e.g. ``1.9.0-4.module_el9.6.0+148+fb6dc857``. Module streams have
    their own per-arch lifecycle on top of the base release: AL9.7's
    ``apache-commons-cli`` ships modular ``1.9.0-4.module_el9.6.0+…``
    on x86_64/aarch64/s390x/ppc64le but only the legacy non-modular
    ``1.4-18.el9_5`` on i686 (modules aren't published for i686 at
    all). That's a property of *the modularity contract*, not a
    release bug, so we exclude modular packages from noarch parity
    entirely — otherwise every module on a multi-arch release would
    flag i686 as "drifting".

    Real, non-modular drift on the same release stream is unaffected:
    those packages don't carry ``.module`` and stay in the parity set.
    """
    return ".module" in pkg.release


def _collect_noarch(session, source, version, arch, repo) -> list[repodata.Package]:
    """Returns all non-modular noarch ``Package`` records in the repo
    (or [] on 404).

    Returning ``Package`` rather than ``{NEVRA: sha}`` lets the caller
    get at ``epoch/version/release`` separately for invariant (B),
    which has to compute the highest EVR per arch.

    Per-source URL resolution:

    * ``stable`` / ``beta`` / ``pungi`` — built by :class:`RepoURL` for
      ``(source, version, arch, repo)``.
    * ``pulp`` — the ``repo`` argument is ignored; the URL comes from
      :func:`pulp_internal_beta_repo_base`. The internal-beta is flat
      (no per-component subtree), so all "repos" of the source share
      one URL — but the caller still threads a single
      ``_PULP_REPO_CELL`` key for each ``(repo, arch)`` so the
      ``compute_noarch_parity_failures`` rendering stays uniform.

    Modular packages (``.module`` in release) are filtered out — see
    :func:`_is_modular` for the rationale.
    """
    if source == "pulp":
        repo_base = pulp_internal_beta_repo_base(version=version, arch=arch)
    else:
        repo_base = RepoURL.from_config(
            source=source, version=version, arch=arch, repo=repo
        ).repo_base()
    try:
        primary_url = repodata.find_primary_xml_url(session, repo_base)
    except Exception:
        return []
    return [
        p
        for p in repodata.iter_packages(session, primary_url, arch_filter="noarch")
        if not _is_modular(p)
    ]


def test_noarch_packages_identical_across_arches_across_all_repos(runtime_config):
    """Both noarch parity invariants — see module docstring.

    The test runs over the full repo set of the source AND the full
    architecture matrix from ``config/architectures.yaml``. Each
    invariant is reported as a separate failure block so that, on
    failure, you can tell whether the breakage is "same NEVRA, two
    different files" (rebuild without a bump) or "same name, two
    different latest builds" (publication skew).
    """
    major = runtime_config.version.split(".")[0]
    all_arches = _all_arches(major=major)
    # On pulp the internal-beta is a flat per-arch repo (no BaseOS /
    # AppStream / … split) — the (repo, arch) grid collapses to one
    # synthetic "pulp-internal-beta" repo per arch. For every other
    # source we iterate the full per-source repo set.
    if runtime_config.source == "pulp":
        all_repos = [_PULP_REPO_CELL]
    else:
        all_repos = list(load_sources()[runtime_config.source]["repos"])

    s = http.session()
    cells: dict[tuple[str, str], list[repodata.Package]] = {}
    for repo in all_repos:
        for arch in all_arches:
            pkgs = _collect_noarch(
                s, runtime_config.source, runtime_config.version, arch, repo
            )
            if not pkgs:
                # Either the repo doesn't exist for this arch (RT/NFV
                # on non-x86, ResilientStorage on AL10, internal-beta
                # not pushed for an arch yet) or no noarch in this
                # cell. Not a parity violation either way.
                continue
            cells[(repo, arch)] = pkgs

    # ``known_drift_b`` is the (repo, name) allowlist of accepted (B)
    # drifts — loaded from config so release engineers can add/remove
    # entries without touching test code. See the YAML file's docstring
    # for the scope guarantees (B-only, exact pair match, no SRPM
    # widening).
    known_drift_b = load_noarch_parity_known_drift()
    failures_a, failures_b = compute_noarch_parity_failures(cells, known_drift_b=known_drift_b)
    blocks = []
    if failures_a:
        blocks.append("\n".join(failures_a))
    if failures_b:
        blocks.append("\n".join(failures_b))
    assert not blocks, "\n\n".join(blocks)
