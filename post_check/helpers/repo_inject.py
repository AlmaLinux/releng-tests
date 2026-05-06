"""Provide ``.repo`` files for AlmaLinux containers.

Three paths, picked by source:

* **stable / beta** — extract the ``.repo`` files **shipped in the
  ``almalinux-repos`` package** and mount them as-is. The package
  already publishes the right mirrorlist + baseurl for the release;
  re-deriving them locally would just let the two drift.

* **pungi** — synthesise minimal ``.repo`` files locally. A pungi
  compose's ``almalinux-repos`` package still references the public
  mirror layout (``repo.almalinux.org`` / ``vault.almalinux.org``),
  but the compose itself lives at a per-arch hostname
  (``x86-64-pungi-10.almalinux.dev`` etc.) — so extraction would
  point dnf at URLs where the compose hasn't been published yet,
  and ``dnf install`` would 404 on the first metadata fetch.

* **pulp** — extract the ``.repo`` files from the **current GA**
  ``almalinux-repos`` package (downloaded via the major-aliased
  stable URL — ``ALMA_VERSION``'s minor is dropped here) AND layer
  one extra synthesised ``.repo`` on top, pointing at the internal
  beta for the *target* version on
  ``build.almalinux.org/pulp/content``. The extra repo is unsigned
  (``gpgcheck=0``) by design — the whole point of this source is to
  exercise an upgrade from current-GA stable to a beta target via an
  additional unsigned repo.

Callers go through :func:`prepare_repo_files_dir` — it dispatches on
``runtime_config.source`` and lays the resulting files into a tmp
dir ready to be bind-mounted at ``/etc/yum.repos.d/`` (read-only).
The dir-mount hides the base image's pre-baked ``almalinux*.repo`` so
the container only sees the URLs we want to validate.

Placeholders (``$releasever`` / ``$basearch``) are left intact in the
package-extraction path — dnf substitutes them inside the target
container. The synthesised paths (pungi, pulp) embed concrete arches
in the URL because the URL templates do not use dnf placeholders.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from post_check.helpers import rpm_extractor

if TYPE_CHECKING:
    from post_check.config import RuntimeConfig


# section_id (lowercase, as it appears in .repo files) → repo path
# component (CamelCase / mixed case, as it appears in published URLs
# ``almalinux/{repo}/{arch}/os``).
#
# Used ONLY by :func:`render_pungi_repo_files`. For stable/beta we
# never need this mapping because we reuse the package's own .repo
# files verbatim.
_SECTION_ID_TO_REPO_NAME: dict[str, str] = {
    "baseos": "BaseOS",
    "appstream": "AppStream",
    "crb": "CRB",
    "rt": "RT",
    "nfv": "NFV",
    "resilientstorage": "ResilientStorage",
    "highavailability": "HighAvailability",
    "extras": "extras",  # lowercase on purpose — that's how the URL is published
    "sap": "SAP",
    "saphana": "SAPHANA",
}


def extract_repo_files(rpm_bytes: bytes) -> dict[str, bytes]:
    """Map basename → raw bytes for every ``/etc/yum.repos.d/*.repo``
    shipped in the ``almalinux-repos`` ``.rpm``.

    Returns the **unmodified** file contents — placeholders like
    ``$releasever`` and ``$basearch`` are left intact, because dnf
    expands them inside the target container.
    """
    out: dict[str, bytes] = {}
    for path in rpm_extractor.list_files(rpm_bytes):
        if not path.startswith("./etc/yum.repos.d/"):
            continue
        if not path.endswith(".repo"):
            continue
        basename = path.rsplit("/", 1)[-1]
        out[basename] = rpm_extractor.read_file(rpm_bytes, path)
    return out


def render_pungi_repo_files(
    *, version: str, arch: str, kitten: bool = False
) -> dict[str, bytes]:
    """Synthesise a minimal ``.repo`` file for a pungi compose.

    Why we generate instead of extract: a pungi compose's
    ``almalinux-repos`` still references the public mirror layout
    (``repo.almalinux.org``/``vault.almalinux.org``), but the compose
    itself is hosted at a per-arch ``*-pungi-<major>.almalinux.dev``
    URL not yet published anywhere downstream. Extracting would point
    dnf at URLs that 404; we point it at the compose URL via
    :class:`RepoURL`.

    The repo set comes from
    :func:`post_check.config.expected_pkg_sections` so per-(major,
    arch) constraints (no ``ResilientStorage`` on AL10, ``RT``/``NFV``
    only on x86_64) are honoured exactly the same way the
    ``almalinux-repos`` package would.
    """
    # Local import: the helpers module must stay importable without
    # the YAML-backed config when used purely for rpm-extraction tests.
    from post_check.config import expected_pkg_sections
    from post_check.helpers.url_builder import RepoURL

    major = version.split(".")[0] if "." in version else version
    sections: list[str] = []
    for sec_id in expected_pkg_sections(major, arch):
        repo_name = _SECTION_ID_TO_REPO_NAME[sec_id]
        baseurl = RepoURL.from_config(
            source="pungi", version=version, arch=arch, repo=repo_name, kitten=kitten,
        ).repo_base()
        sections.append(
            f"[{sec_id}]\n"
            f"name=AlmaLinux {major} - {repo_name}\n"
            f"baseurl={baseurl}/\n"
            f"enabled=1\n"
            f"gpgcheck=1\n"
            f"gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-{major}\n"
        )
    # Single file with all sections — pungi composes only need to be
    # reachable, not preserve the upstream package's file split.
    return {"post-check-pungi.repo": ("\n".join(sections) + "\n").encode()}


def render_pulp_internal_beta_repo_file(
    *, version: str, arch: str
) -> dict[str, bytes]:
    """Synthesise the unsigned internal-beta ``.repo`` for the ``pulp`` source.

    Pulp layers an extra repo on top of the regular stable layout — the
    one hosted on ``build.almalinux.org/pulp/content/...``. AL9 and AL10
    use different URL prefixes (``builds/AlmaLinux-9-…`` vs.
    ``copr/eabdullin1-almalinux10-…``) so the URL templates per-major
    live in :func:`post_check.config.load_sources`'s ``pulp`` cfg under
    ``internal_beta_urls``.

    Three sections are emitted: a main per-arch repo (``enabled=1``),
    a per-arch ``-debug`` companion (``enabled=0``) and an arch-less
    ``-source`` companion (``enabled=0``). The default-disabled
    companions match how the stock ``almalinux-repos`` ships its own
    ``-debug`` / ``-source`` variants — present, but opted-in only by
    operators who actually need them.

    These repos are **not signed** — ``gpgcheck=0`` is the contract
    for this source, not a regression. The signed-repo invariants
    enforced by ``test_almalinux_repos_pkg`` apply only to the .repo
    files inside the package, which we don't touch here.
    """
    from post_check.config import load_sources

    cfg = load_sources()["pulp"]
    major = version.split(".")[0] if "." in version else version
    urls = cfg["internal_beta_urls"].get(major)
    if not urls:
        raise ValueError(
            f"pulp internal-beta URLs not defined for AlmaLinux {major} "
            f"(known: {sorted(cfg['internal_beta_urls'])})"
        )

    sections: list[str] = []
    # ``main`` first so the report ordering matches the operator's
    # mental model: the primary unsigned beta is on top, the optional
    # debug/source companions follow.
    for kind in ("main", "debug", "source"):
        if kind not in urls:
            continue
        # ``{arch}`` is intentionally a no-op for the source URL
        # (sources are arch-less and the template carries no
        # placeholder), so .format() is safe across all three kinds.
        baseurl = urls[kind].format(arch=arch)
        if kind == "main":
            sec_id = "pulp-internal-beta"
            display = f"AlmaLinux {major} - Pulp internal beta"
            enabled = 1
        else:
            sec_id = f"pulp-internal-beta-{kind}"
            display = f"AlmaLinux {major} - Pulp internal beta ({kind})"
            enabled = 0
        sections.append(
            f"[{sec_id}]\n"
            f"name={display}\n"
            f"baseurl={baseurl}\n"
            f"enabled={enabled}\n"
            f"gpgcheck=0\n"
        )
    return {"post-check-pulp-internal-beta.repo": ("\n".join(sections) + "\n").encode()}


def prepare_repo_files_dir(
    *,
    runtime_config: "RuntimeConfig",
    arch: str,
    target_dir: Path,
    rpm_bytes: bytes | None = None,
    kitten: bool = False,
) -> Path:
    """Materialise ``.repo`` files for the runtime into ``target_dir``.

    Dispatches on ``runtime_config.source``:

    * ``stable`` / ``beta`` — extracts from ``rpm_bytes`` (the
      ``almalinux-repos`` package downloaded in fixtures).
      ``rpm_bytes`` is required.
    * ``pungi`` — generates per-arch URLs locally. ``rpm_bytes`` is
      ignored even if provided (the pungi compose's package ships
      wrong URLs — see :func:`render_pungi_repo_files`).
    * ``pulp`` — extracts from ``rpm_bytes`` (same as stable) AND
      layers an extra unsigned ``.repo`` file on top, pointing at the
      internal beta on ``build.almalinux.org/pulp/content``.
      ``rpm_bytes`` is required.

    Returns ``target_dir`` so the caller can bind-mount it directly.
    """
    if runtime_config.source == "pungi":
        files = render_pungi_repo_files(
            version=runtime_config.version, arch=arch, kitten=kitten
        )
    else:
        if rpm_bytes is None:
            raise ValueError(
                f"source={runtime_config.source!r} requires rpm_bytes "
                f"(the bytes of the almalinux-repos package). "
                f"Pass the ``almalinux_repos_pkg_bytes`` fixture."
            )
        files = extract_repo_files(rpm_bytes)
        if runtime_config.source == "pulp":
            # Layer the unsigned internal-beta on top of the stable
            # package's .repo files. Distinct basename so we never
            # collide with anything the package itself ships.
            files.update(
                render_pulp_internal_beta_repo_file(
                    version=runtime_config.version, arch=arch
                )
            )
    target_dir.mkdir(parents=True, exist_ok=True)
    for basename, content in files.items():
        (target_dir / basename).write_bytes(content)
    return target_dir


def dnf_repo_flags(
    *, source: str, version: str, arch: str
) -> str:
    """Return the ``--disablerepo``/``--enablerepo`` fragment to pin dnf
    inside a container to *only* the repos we just laid down via
    :func:`prepare_repo_files_dir`.

    The bind-mount over ``/etc/yum.repos.d/`` already hides the image's
    pre-baked AlmaLinux repos, so for stable/beta/pulp we trust dnf to
    pick up exactly what the package shipped (plus, for pulp, the
    layered internal-beta file) — returns ``""``.

    For **pungi** we go further: dnf is told to disable everything and
    re-enable just the section IDs synthesised by
    :func:`render_pungi_repo_files`. Two reasons:

    * defence in depth — if a future image variant leaks a baseurl in
      via ``/etc/dnf/dnf.conf`` or ``/etc/dnf/repos.d/`` (paths the bind
      mount does **not** cover), explicit ``--disablerepo='*'`` keeps
      the test honest.
    * visibility — a reviewer reading ``dnf install …`` in test output
      sees that only the pungi sections are active, instead of having
      to reason about bind-mount semantics.

    Returned string ends with a trailing space (or is empty), so it can
    be interpolated directly into the ``dnf <verb> {flags}<args>``
    template without extra spacing logic.
    """
    if source != "pungi":
        return ""
    # Local import keeps this module importable without the YAML config
    # for pure rpm-extraction tests (mirrors render_pungi_repo_files).
    from post_check.config import expected_pkg_sections

    major = version.split(".", 1)[0]
    sections = ",".join(expected_pkg_sections(major, arch))
    return f"--disablerepo='*' --enablerepo='{sections}' "


def build_install_command(
    *, allow_broken: bool = True, no_weak: bool = True
) -> list[str]:
    """Return the ``docker exec``-style argv for ``dnf install -y '*'``.

    The command runs inside the container under ``bash -c`` so we can both
    glob ``'*'`` and tee the output to ``/tmp/dnf-install.log`` for later
    inspection. Defaults match what the "install everything" container check
    needs: ``--skip-broken`` (don't abort on a single unsatisfied dep) and
    ``install_weak_deps=False`` (Recommends/Suggests off — we only validate
    that the *required* graph is closed).
    """
    flags = ["dnf", "install", "-y"]
    if allow_broken:
        flags.append("--skip-broken")
    if no_weak:
        flags.append("--setopt=install_weak_deps=False")
    flags.append("'*'")
    return ["bash", "-c", " ".join(flags) + " 2>&1 | tee /tmp/dnf-install.log"]
