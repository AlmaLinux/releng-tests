"""Parser for `.repo` (yum/dnf) configuration files.

Read-only: we only need to inspect repo metadata (baseurl, gpgkey,
gpgcheck, …) shipped inside ``almalinux-repos`` RPMs. Writing back
into the .repo INI format is intentionally out of scope.
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass


@dataclass(frozen=True)
class RepoSection:
    """One ``[section_id]`` block from a `.repo` file."""

    section_id: str
    name: str
    baseurl: str | None
    metalink: str | None
    mirrorlist: str | None
    gpgkey: str | None
    gpgcheck: bool
    enabled: bool
    sslverify: bool


def parse(text: str) -> list[RepoSection]:
    """Parse the contents of a `.repo` file into :class:`RepoSection`s.

    `interpolation=None` is mandatory: yum/dnf repo files routinely
    contain ``%`` characters (e.g. ``$releasever``) that would otherwise
    blow up `configparser`'s ``%(...)s`` interpolation.
    """
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(text)
    out: list[RepoSection] = []
    for section_id in cp.sections():
        s = cp[section_id]
        out.append(
            RepoSection(
                section_id=section_id,
                name=s.get("name", section_id),
                baseurl=s.get("baseurl"),
                metalink=s.get("metalink"),
                mirrorlist=s.get("mirrorlist"),
                gpgkey=s.get("gpgkey"),
                gpgcheck=s.getboolean("gpgcheck", fallback=False),
                enabled=s.getboolean("enabled", fallback=True),
                sslverify=s.getboolean("sslverify", fallback=True),
            )
        )
    return out


def substitute(url: str, *, basearch: str, releasever: str) -> str:
    """Expand the two yum/dnf variables we care about in repo URLs."""
    return url.replace("$basearch", basearch).replace("$releasever", releasever)
