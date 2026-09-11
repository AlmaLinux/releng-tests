"""URL builder for AlmaLinux repositories (stable / beta / pungi / pulp).

Pure builder: no HTTP requests. All I/O is external via helpers.http.

There are four sources, with two version schemes:
stable and beta require ``major.minor`` (e.g. ``10.1``); pungi and
pulp accept either form and silently use the major only (``10``).
This validation is hardcoded below so the source registry only has
to describe things that actually vary (pungi result_dir, pulp
internal-beta URL templates).

``pulp`` is a hybrid: for the named repos (BaseOS, AppStream, …) it
serves the **major-aliased** stable URLs (``…/almalinux/10/…``), not
the per-minor ones. The reason: pulp's whole point is to test an
upgrade path — the operator passes ``ALMA_VERSION=10.2`` as the
**target** that should end up in ``/etc/os-release`` after
``dnf upgrade``, but the *base* the layered internal-beta is added
on top of is the current GA in that major (whatever
``repo.almalinux.org/almalinux/10`` happens to alias to today). The
"extra" pulp internal-beta ``.repo`` is layered on top by
:mod:`post_check.helpers.repo_inject`, not by this builder.
"""

from __future__ import annotations

from dataclasses import dataclass

from post_check.config import load_architectures, load_sources

# stable/beta use major.minor (10.1); pungi/pulp use major-only (10),
# silently dropping any minor the caller passes in.
_MAJOR_MINOR_SOURCES = frozenset({"stable", "beta"})
_MAJOR_ONLY_SOURCES = frozenset({"pungi", "pulp"})


@dataclass(frozen=True)
class RepoURL:
    source: str
    version: str  # "10.1" for stable/beta, "10" for pungi
    arch: str
    repo: str
    kitten: bool = False  # only meaningful for pungi

    # ------------------------------------------------------------------ ctor
    @classmethod
    def from_config(
        cls,
        *,
        source: str,
        version: str,
        arch: str,
        repo: str,
        kitten: bool = False,
    ) -> "RepoURL":
        if not version:
            raise ValueError("version is required (got empty)")

        sources = load_sources()
        if source not in sources:
            raise ValueError(
                f"unknown source: {source}. Available: {sorted(sources.keys())}"
            )

        arches = load_architectures()
        if arch not in arches:
            raise ValueError(
                f"unknown arch: {arch}. Available: {sorted(arches.keys())}"
            )

        if source in _MAJOR_MINOR_SOURCES and "." not in version:
            raise ValueError(
                f"source={source} requires version in major.minor format (e.g. 10.1), "
                f"got: {version!r}"
            )
        if source in _MAJOR_ONLY_SOURCES:
            # pungi only cares about the major; silently drop a minor component
            # so callers can pass either "10" or "10.2".
            version = version.split(".", 1)[0]
            if not version:
                raise ValueError(
                    f"source={source} requires a non-empty major version, got: {version!r}"
                )

        return cls(source=source, version=version, arch=arch, repo=repo, kitten=kitten)

    # ------------------------------------------------------------------ helpers
    def major(self) -> str:
        return self.version.split(".")[0]

    def _pungi_host(self) -> str:
        """Hostname segment of the pungi mirror. For x86_64 it's
        ``x86-64``; for the other arches it's the arch name itself.

        Per-major override (``pungi_host_by_major``) takes precedence
        over the plain ``pungi_host`` field. i686 needs this because
        AL9 never got a dedicated i686 pungi compose — its i686 packages
        live UNDER the x86_64 compose at
        ``x86-64-pungi-9.almalinux.dev/almalinux/9/i686/…``. Starting
        with AL10 the i686 compose got its own host
        (``i686-pungi-10.almalinux.dev``), so the mapping has to be
        per-major or the AL9 i686 pungi URL would 404 and parity tests
        would silently skip the drift.
        """
        arches = load_architectures()
        cfg = arches[self.arch]
        by_major = cfg.get("pungi_host_by_major") or {}
        if self.major() in by_major:
            return by_major[self.major()]
        return cfg.get("pungi_host", self.arch)

    def _stable_base_url(self) -> str:
        """URL prefix for the stable source, up to (but not including)
        the version segment.

        Defaults to ``https://repo.almalinux.org/almalinux`` — the
        public mirror layout. Per-arch overridden via
        ``architectures.yaml::stable_repo_base_url`` for arches that
        live on a different host with a different path layout
        (i686 → ``https://vault.almalinux.org`` without the
        ``/almalinux`` prefix). Returned without trailing slash so the
        caller can interpolate ``"{base}/{version}/{repo}/..."`` cleanly.
        """
        arches = load_architectures()
        return arches[self.arch].get(
            "stable_repo_base_url", "https://repo.almalinux.org/almalinux"
        )

    # ------------------------------------------------------------------ URLs
    def repo_base(self) -> str:
        """Returns baseurl up to and including /os (without trailing slash).

        For ``stable`` the host comes from :meth:`_stable_host` —
        defaults to ``repo.almalinux.org`` but is overridden per arch
        via ``architectures.yaml::stable_repo_host`` (i686 → vault).
        """
        if self.source == "stable" or self.source == "pulp":
            # ``pulp`` reuses the stable URL host but with a major-only
            # path segment (``self.version`` is already stripped to the
            # major during construction — see ``_MAJOR_ONLY_SOURCES``).
            # The unsigned internal-beta layer for the *target* version
            # is added on top by repo_inject. See module docstring.
            return f"{self._stable_base_url()}/{self.version}/{self.repo}/{self.arch}/os"

        if self.source == "beta":
            return f"https://vault.almalinux.org/{self.version}-beta/{self.repo}/{self.arch}/os"

        if self.source == "pungi":
            cfg = load_sources()[self.source]
            major = self.major()
            result_dir = (
                cfg["result_dir_kitten"] if self.kitten else cfg["result_dir_default"]
            )
            return (
                f"https://{self._pungi_host()}-pungi-{major}.almalinux.dev"
                f"/almalinux/{major}/{self.arch}/{result_dir}/compose/{self.repo}/{self.arch}/os"
            )

        raise ValueError(f"unknown source: {self.source}")

    def repomd_xml(self) -> str:
        return self.repo_base() + "/repodata/repomd.xml"

    def repomd_xml_asc(self) -> str:
        return self.repo_base() + "/repodata/repomd.xml.asc"

    def iso_dir(self) -> str:
        if self.source == "stable" or self.source == "pulp":
            # pulp doesn't ship its own ISOs; for completeness we point
            # at the major-aliased stable ISO directory (``self.version``
            # is major-only for pulp). ``test_iso_checksums`` itself is
            # skipped for pulp, so this branch is mostly a no-op there.
            return f"{self._stable_base_url()}/{self.version}/isos/{self.arch}"
        if self.source == "beta":
            return f"https://vault.almalinux.org/{self.version}-beta/isos/{self.arch}"
        if self.source == "pungi":
            cfg = load_sources()[self.source]
            major = self.major()
            result_dir = (
                cfg["result_dir_kitten"] if self.kitten else cfg["result_dir_default"]
            )
            return (
                f"https://{self._pungi_host()}-pungi-{major}.almalinux.dev"
                f"/almalinux/{major}/{self.arch}/{result_dir}/compose/isos/{self.arch}"
            )
        raise ValueError(f"unknown source: {self.source}")

    def public_iso_dir(self) -> str:
        """Public download directory for the installation media on
        ``repo.almalinux.org`` — the URL a release announcement sends
        users to (``…/almalinux/10.2/isos/aarch64`` for stable,
        ``…/almalinux/10.3-beta/isos/aarch64`` for beta).

        Why this is not :meth:`iso_dir` for beta: beta *yum* repos are
        served only from ``vault.almalinux.org`` (that is what the
        public mirrorlist hands out), and vault keeps every past beta —
        which makes it the right base for the CHECKSUM trust chain that
        ``test_iso_checksums`` verifies. The ISOs, however, are
        published to ``repo.almalinux.org`` as well, and *that* copy is
        the one users download and the one the publish step populates
        and later prunes. "Are the ISOs in place?" therefore has to be
        asked here, not on vault.

        Only stable and beta publish to the public mirror: pungi serves
        media from its own per-arch compose host and pulp ships no ISOs
        at all, so both raise instead of returning a URL that would
        404 by construction.
        """
        if self.source == "stable":
            # Same per-arch base as the stable repos (i686 → vault), so
            # this stays correct for any arch whose media ever move host.
            return f"{self._stable_base_url()}/{self.version}/isos/{self.arch}"
        if self.source == "beta":
            # Hardcoded to the public mirror rather than derived from
            # ``_stable_base_url``: the beta ISO tree exists only on
            # repo.almalinux.org under the ``-beta`` suffix.
            return (
                f"https://repo.almalinux.org/almalinux/"
                f"{self.version}-beta/isos/{self.arch}"
            )
        raise ValueError(
            f"source={self.source} publishes no ISOs on repo.almalinux.org "
            f"(only stable and beta do); use iso_dir() for the "
            f"source's own media location"
        )


def pulp_internal_beta_repo_base(*, version: str, arch: str) -> str:
    """Return the per-arch internal-beta repo base URL for ``ALMA_SOURCE=pulp``.

    The pulp source is a hybrid (see module docstring): :class:`RepoURL`
    for ``source="pulp"`` returns the major-aliased stable URL, which
    points at the **current GA** (``10.1``), not the upgrade target.
    The actual upgrade target — the build of ``almalinux-release`` whose
    ``version`` equals ``ALMA_VERSION`` (``10.2``) — lives in the layered
    internal-beta repo on ``build.almalinux.org/pulp/content/...``. That
    repo is **flat**: there is no per-component ``BaseOS``/``AppStream``
    subtree, so the returned URL has no ``{repo}`` segment.

    Used by the cross-arch parity test, which on pulp must look at this
    layer (otherwise it would re-check current GA and the
    ``version == ALMA_VERSION`` invariant would never hold by
    construction). Trailing slash stripped to match
    :meth:`RepoURL.repo_base`.
    """
    from post_check.config import load_pulp_internal_beta_urls

    major = version.split(".", 1)[0] if "." in version else version
    by_major = load_pulp_internal_beta_urls()
    urls = by_major.get(major)
    if not urls or "main" not in urls:
        raise ValueError(
            f"pulp internal-beta `main` URL not defined for AlmaLinux {major} "
            f"(known majors: {sorted(by_major)})"
        )
    return urls["main"].format(arch=arch).rstrip("/")
