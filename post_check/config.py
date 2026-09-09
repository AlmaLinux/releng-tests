"""Runtime configuration loading and env-var driven RuntimeConfig.

ROOT points at the repo root (parent of the `post_check/` package directory).
YAML configs live under `<ROOT>/config/`.

The source registry (``stable``/``beta``/``pungi``/``pulp``) is defined
as a hardcoded constant below — it never varies per-deployment, so we
do not keep it in YAML. Things that DO vary at run time (architecture
matrix, GPG fingerprints) still live under ``config/``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

_SUPPORTED_MAJORS = {"9", "10"}

# ----------------------------------------------------------------- registry
# These are facts about the AlmaLinux release process, not configuration.
# They never change between runs of post-check, so keeping them in YAML
# would be ceremony with zero benefit.

_DEFAULT_REPOS: tuple[str, ...] = (
    "BaseOS",
    "AppStream",
    "CRB",
    "extras",
    "HighAvailability",
    "ResilientStorage",
    "NFV",
    "RT",
    "SAP",
    "SAPHANA",
)


# ----------------------------------------------------------------- expected sections
# Per-major map of [section] IDs that must be present in the
# almalinux-repos package's .repo files. The IDs are lowercase, matching
# how almalinux-repos labels its sections (e.g. "baseos", not "BaseOS").
#
# AL10 dropped ResilientStorage. RT and NFV ship only on x86_64 (this
# applies to every supported major).
#
# Variants like ``-debug``/``-source`` and disabled fallback mirrors are
# intentionally omitted: this list is the "primary repos every user
# expects to find on a freshly-installed system", which is what the
# release gate validates.
_ARCH_ANY: tuple[str, ...] = ()  # empty == every arch

# i686 is the "minimum" arch — community-rebuilt, no SAP/HA/ResilientStorage,
# no RT/NFV. We restrict its expected primary sections to the same set
# that real AlmaLinux i686 mirrors actually carry (baseos / appstream /
# crb / extras). Any change here should be made together with the
# almalinux-repos package's i686 .repo file shipping (the test consumes
# both — the contract here is the lower bound, the package is the
# upper bound).
_ARCH_X86_FAMILY: tuple[str, ...] = ("x86_64", "x86_64_v2")
_ARCH_NOT_I686: tuple[str, ...] = ("x86_64", "x86_64_v2", "aarch64", "s390x", "ppc64le")

_EXPECTED_PKG_SECTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "9": {
        "baseos": _ARCH_ANY,
        "appstream": _ARCH_ANY,
        "crb": _ARCH_ANY,
        "rt": _ARCH_X86_FAMILY,
        "nfv": _ARCH_X86_FAMILY,
        "resilientstorage": _ARCH_NOT_I686,
        "highavailability": _ARCH_NOT_I686,
        "extras": _ARCH_ANY,
        "sap": _ARCH_NOT_I686,
        "saphana": _ARCH_NOT_I686,
    },
    "10": {
        "baseos": _ARCH_ANY,
        "appstream": _ARCH_ANY,
        "crb": _ARCH_ANY,
        "rt": _ARCH_X86_FAMILY,
        "nfv": _ARCH_X86_FAMILY,
        "highavailability": _ARCH_NOT_I686,
        "extras": _ARCH_ANY,
        "sap": _ARCH_NOT_I686,
        "saphana": _ARCH_NOT_I686,
    },
}


def expected_pkg_sections(major: str, arch: str) -> tuple[str, ...]:
    """Section IDs that must be present in the ``almalinux-repos``
    package's ``.repo`` files for the given ``(major, arch)`` combo.

    The IDs are lowercase, matching how the package labels its sections
    in ``.repo`` files (``[baseos]``, not ``[BaseOS]``).

    Use cases:
      * test that every expected section ships in the package;
      * iterate the package's sections in mirrorlist/baseurl tests
        without falling back to whatever happens to be ``enabled=1``.
    """
    if major not in _EXPECTED_PKG_SECTIONS:
        raise ValueError(
            f"unsupported major: {major} (known: {sorted(_EXPECTED_PKG_SECTIONS)})"
        )
    return tuple(
        sec_id
        for sec_id, allowed_arches in _EXPECTED_PKG_SECTIONS[major].items()
        if not allowed_arches or arch in allowed_arches
    )

# pungi result directory names — fixed by AlmaLinux infra, not by us.
_PUNGI_RESULT_DIR_DEFAULT = "latest_result_almalinux"
_PUNGI_RESULT_DIR_KITTEN = "latest_result_almalinux-kitten"

# Pulp internal-beta repo URL templates: loaded from
# ``config/pulp_internal_beta.yaml`` (see that file for schema docs).
#
# ``pulp`` is a hybrid source: the user-facing repos are the regular
# ``stable`` ones (so dnf still pulls signed packages from
# ``repo.almalinux.org``), but on top of that we layer a single extra
# ``.repo`` file pointing at the internal beta hosted on
# ``build.almalinux.org/pulp/content/...``. The internal beta is NOT
# signed — that's expected; ``gpgcheck=0`` for those sections is part
# of the contract.
#
# These URLs live in YAML, not as a Python constant: AL10 currently
# points at a personal copr that is expected to change as the release
# process matures, and AL9 uses a different hosting layout entirely.
# Keeping them as data lets release engineers adjust the URLs without
# editing Python code.


def load_pulp_internal_beta_urls() -> dict[str, dict[str, str]]:
    """Read ``config/pulp_internal_beta.yaml`` into a per-major map.

    Returned shape::

        {
          "9":  {"main": "...", "debug": "...", "source": "..."},
          "10": {"main": "...", "debug": "...", "source": "..."},
        }

    Re-read on every call (consistent with ``load_architectures``) —
    cheap, and avoids a stale cache when an operator edits the YAML
    between pytest runs in a single shell.
    """
    return yaml.safe_load((ROOT / "config" / "pulp_internal_beta.yaml").read_text())


@dataclass(frozen=True)
class RuntimeConfig:
    source: str
    version: str
    arches: tuple[str, ...]
    repos: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        src = os.environ.get("ALMA_SOURCE")
        ver = os.environ.get("ALMA_VERSION")
        if not src or not ver:
            raise SystemExit(
                "ALMA_SOURCE and ALMA_VERSION are required (env vars). "
                "Example: ALMA_SOURCE=stable ALMA_VERSION=10.1"
            )
        major = ver.split(".")[0]
        if major not in _SUPPORTED_MAJORS:
            raise SystemExit(
                f"AlmaLinux major={major} is not supported "
                f"(only {sorted(_SUPPORTED_MAJORS)} allowed). AlmaLinux 8 out of scope."
            )
        arches_cfg = load_architectures()
        arches = tuple(
            a.strip()
            for a in os.environ.get("ALMA_ARCHES", "x86_64").split(",")
            if a.strip()
        )
        for a in arches:
            if a not in arches_cfg:
                raise SystemExit(
                    f"unknown arch: {a!r}. Available: {sorted(arches_cfg.keys())}"
                )
            sup = arches_cfg[a].get("supported_majors")
            if sup and major not in sup:
                raise SystemExit(
                    f"arch={a!r} is not supported on AlmaLinux {major} "
                    f"(supported_majors={sup}). Drop it from ALMA_ARCHES "
                    f"or pick a supported version."
                )
        repos_env = os.environ.get("ALMA_REPOS", "")
        if repos_env:
            repos = tuple(r.strip() for r in repos_env.split(",") if r.strip())
        else:
            sources = load_sources()
            if src not in sources:
                raise SystemExit(
                    f"unknown source: {src}. Available: {sorted(sources.keys())}"
                )
            repos = tuple(sources[src]["repos"])
        return cls(src, ver, arches, repos)


def load_sources() -> dict[str, dict[str, Any]]:
    """Return the source registry as ``{source_name: cfg}``.

    Each ``cfg`` always has:
      * ``repos`` — full list of repos for that source.

    pungi additionally has ``result_dir_default`` and
    ``result_dir_kitten`` because its baseurl includes a per-compose
    directory name.

    pulp additionally has ``internal_beta_urls`` — a per-major map
    ``{major: {"main"/"debug"/"source": URL_TEMPLATE}}`` describing the
    extra unsigned ``.repo`` sections to layer on top of the stable
    repos. ``{arch}`` is the only placeholder substituted at runtime.

    Mirrorlist URLs are intentionally **not** in this registry —
    they're authoritative only in the ``almalinux-repos`` package's
    ``.repo`` files, and ``test_mirrorlist`` reads them from there.
    Mirroring them in a Python constant would just let the two drift.
    """
    repos = list(_DEFAULT_REPOS)
    return {
        "stable": {"repos": list(repos)},
        "beta": {"repos": list(repos)},
        "pungi": {
            "repos": list(repos),
            "result_dir_default": _PUNGI_RESULT_DIR_DEFAULT,
            "result_dir_kitten": _PUNGI_RESULT_DIR_KITTEN,
        },
        # ``pulp`` reuses the stable repo set, but at the **major-only**
        # URL alias (``…/almalinux/10/…``) — not the per-minor one. The
        # operator-supplied ``ALMA_VERSION`` (e.g. ``10.2``) is the
        # **target** that should end up in ``/etc/os-release`` after
        # ``dnf upgrade``; the base is whatever the major alias points
        # at today (current GA in that major). On top of that base we
        # layer one extra ``.repo`` for the internal beta hosted on
        # ``build.almalinux.org/pulp/content/...`` — that is where the
        # target-version packages actually come from.
        #
        # The injection itself happens in ``helpers/repo_inject.py``;
        # the URL templates per major are loaded from
        # ``config/pulp_internal_beta.yaml`` so release engineers can
        # rotate them without a code change.
        "pulp": {
            "repos": list(repos),
            "internal_beta_urls": load_pulp_internal_beta_urls(),
        },
    }


def load_architectures() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "config" / "architectures.yaml").read_text())


def load_noarch_parity_known_drift(
    major: str,
) -> dict[tuple[str, str], frozenset[str]]:
    """Read ``config/noarch_parity_known_drift.yaml`` into a map of
    ``(repo, name) -> {allowed arches}`` that ``test_noarch_parity``
    invariant (B) consults when deciding whether a flagged drift is an
    accepted one.

    Only the ``major`` subtree is read.

    Shape rationale
    ---------------
    The file is keyed by release major first
    (``known_drift: {"9": {<repo>: {<name>: [arch, ...]}}}``) because
    accepted drift belongs to one release stream, not to the distro.
    The AL9 i686 backlog (the Java stack, cockpit, xmvn, LibreOffice,
    …) is accepted because those i686 builds will never be refreshed;
    the same package name drifting on AL10 is a fresh release bug and
    must still fire. Reading only the requested major is what keeps
    those two cases apart — there is deliberately no "all majors"
    wildcard.

    The arches list is a **required** part of each entry, not an
    optional default. The release process has accepted drift only on
    specific arches (i686 today); listing the arch explicitly is what
    keeps the parity test catching drift on *other* arches. The
    consumer interprets the set as "subtract these arches from the
    cell, re-check parity on what remains" (see
    ``compute_noarch_parity_failures``), so listing ``[i686]`` does
    not silence a later ppc64le rebuild that misses x86_64.

    Returns an empty dict when the file is absent, or when it carries
    no entries for ``major`` — keeping the parity test functional in
    environments where no drift has been accepted (a fresh AL10
    release, say). Schema errors (a non-list arches value, a
    non-string entry, an empty arches list, or the pre-major
    ``repo``-at-top-level shape) raise ``ValueError`` at load time
    rather than silently degrading to "all drift accepted" — the
    latter would turn the next release run green by accident.
    """
    path = ROOT / "config" / "noarch_parity_known_drift.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    by_major = data.get("known_drift") or {}
    if not isinstance(by_major, dict):
        raise ValueError(
            f"{path.name}: top-level 'known_drift' must be a mapping "
            f"of major -> {{repo: {{name: [arch, ...]}}}}, got "
            f"{type(by_major).__name__}"
        )
    # Catch the pre-major shape (``known_drift: {AppStream: {...}}``)
    # explicitly. Silently treating a repo key as a major would make
    # every previously-accepted drift invisible for every major, i.e.
    # a wall of "new" failures on the next release run with no clue
    # that the file simply moved a level.
    for key in by_major:
        if not (isinstance(key, str) and key.isdigit()):
            raise ValueError(
                f"{path.name}: known_drift keys must be release majors "
                f"as quoted strings (\"9\", \"10\", …), got {key!r}. "
                f"(The previous repo-at-top-level shape is no longer "
                f"supported — nest each repo under the major whose "
                f"release accepted the drift.)"
            )
    raw = by_major.get(major) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path.name}: known_drift[{major!r}] must be a mapping "
            f"of repo -> {{name: [arch, ...]}}, got {type(raw).__name__}"
        )
    out: dict[tuple[str, str], frozenset[str]] = {}
    for repo, names in raw.items():
        if names is None:
            # ``RepoName:`` with no body is allowed — operators sometimes
            # keep an empty section as a placeholder for an upcoming
            # block. Treated as "no entries for this repo".
            continue
        if not isinstance(names, dict):
            raise ValueError(
                f"{path.name}: known_drift[{major!r}][{repo!r}] must be a "
                f"mapping of name -> [arch, ...], got "
                f"{type(names).__name__}. (The previous list-of-strings "
                f"shape is no longer supported — each entry must specify "
                f"the arches whose drift is accepted.)"
            )
        for name, arches in names.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"{path.name}: known_drift[{major!r}][{repo!r}] "
                    f"contains a non-string or empty key: {name!r}"
                )
            if not isinstance(arches, list) or not arches:
                raise ValueError(
                    f"{path.name}: known_drift[{major!r}][{repo!r}]"
                    f"[{name!r}] must be a non-empty list of arches, got "
                    f"{arches!r}. There is no implicit 'any arch' — list "
                    f"the arches whose drift is accepted explicitly."
                )
            for a in arches:
                if not isinstance(a, str) or not a:
                    raise ValueError(
                        f"{path.name}: known_drift[{major!r}][{repo!r}]"
                        f"[{name!r}] contains a non-string arch entry: "
                        f"{a!r}"
                    )
            out[(repo, name)] = frozenset(arches)
    return out


def arch_skip_categories(arch: str) -> frozenset[str]:
    """Categories of tests this arch opts out of (see architectures.yaml).

    Currently recognised values: ``"container"`` (any test marked
    ``requires_docker``), ``"iso"`` (ISO checksum/reachability tests),
    ``"upgrade"`` (dnf-upgrade test). The mapping from category to
    test files lives in ``tests/conftest.py`` so the YAML stays
    test-file-agnostic.

    Returns an empty set when the arch is unknown OR has no
    ``skip_categories`` field — both are not-an-error: an unknown arch
    will be rejected upstream by ``RuntimeConfig.from_env``, and most
    arches have no skips at all.
    """
    cfg = load_architectures().get(arch, {})
    return frozenset(cfg.get("skip_categories") or ())


def arch_supports_major(arch: str, major: str) -> bool:
    """True iff ``arch`` is published for AlmaLinux ``major``.

    An arch with no ``supported_majors`` field is treated as supporting
    all majors (the historical default — matches the legacy four arches
    which existed in AL9 and AL10).
    """
    cfg = load_architectures().get(arch, {})
    sup = cfg.get("supported_majors")
    if not sup:
        return True
    return major in sup


def arch_uses_vault_only_mirrorlist(arch: str) -> bool:
    """True iff this arch's public mirrorlist returns only vault URLs.

    Used by ``test_mirrorlist`` to relax the "URLs must not point at
    vault for stable" rule and the "≥2 of 3 mirrors must be live" rule
    when the mirrorlist legitimately returns a single
    ``vault.almalinux.org`` URL (which is the case for i686, the same
    way it is for beta).
    """
    cfg = load_architectures().get(arch, {})
    return bool(cfg.get("vault_only_mirrorlist"))


def compute_prev_version(version: str) -> str | None:
    """Compute the previous AlmaLinux minor version for upgrade testing.

    For ``"x.y"`` with ``y >= 1``, returns ``"x.{y-1}"`` —
    e.g. ``"10.1" → "10.0"``, ``"10.2" → "10.1"``.

    Returns ``None`` when there is no in-major predecessor:

    * ``"x.0"`` — first minor of a major; the previous release lives in
      a different major and would require an explicit cross-major upgrade
      test (out of scope here);
    * major-only versions used by pungi (e.g. ``"10"``) — pungi composes
      do not carry a "previous minor" by design.

    Caller decides what to do with ``None``: the upgrade test skips,
    other consumers may want to fall back to a different code path.
    """
    parts = version.split(".")
    if len(parts) != 2:
        return None
    major, minor = parts
    try:
        minor_int = int(minor)
    except ValueError:
        return None
    if minor_int < 1:
        return None
    return f"{major}.{minor_int - 1}"
