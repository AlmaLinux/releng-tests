"""AlmaLinux ``isos/<arch>/`` contract: CHECKSUM parsing + ISO file names.

CHECKSUM format (after stripping the GPG clearsign wrapper) is
GNU/BSD-tag (``sha256sum --tag``), with a size comment per file that
the AlmaLinux publish step adds:

    # AlmaLinux-10.1-x86_64-dvd.iso: 10107617280 bytes
    SHA256 (AlmaLinux-10.1-x86_64-dvd.iso) = <64 hex chars>

This is what AlmaLinux ships in ``isos/<arch>/CHECKSUM`` on the mirror.
We deliberately do NOT support the bare ``<hash>  <filename>`` layout —
AlmaLinux never publishes in that form, and supporting it would silently
accept malformed CHECKSUM files instead of failing them.

The ISO *name* helpers live here too, next to the format they have to
agree with: both ISO test modules need to say "this flavour is missing"
in terms of the exact file name a release is expected to publish.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The three flavours every AlmaLinux release publishes for every media
# arch. A release that drops one of them is a release-management
# regression we want to catch at the gate, not after users notice that
# the boot ISO is missing. Consumed by both ISO test modules — keep it
# here so the two cannot drift apart.
ISO_KINDS: tuple[str, ...] = ("dvd", "boot", "minimal")


@dataclass(frozen=True)
class IsoChecksum:
    """Pair of (ISO filename, expected sha256)."""

    filename: str
    sha256: str


# "SHA256 (AlmaLinux-10.1-x86_64-dvd.iso) = abc123..." (sha256sum --tag).
# ``{64}`` is intentional — anything shorter is not a valid sha256 and
# must be rejected, not treated as a "best effort" match.
_RE_GNU = re.compile(
    r"^SHA256\s*\(\s*(?P<name>[^)]+?)\s*\)\s*=\s*(?P<hash>[0-9a-f]{64})\s*$",
    re.MULTILINE,
)


def parse_checksum(payload: str) -> list[IsoChecksum]:
    """Extract all ``(filename, sha256)`` pairs from a CHECKSUM payload.

    Returns an empty list if no valid lines are found — callers are
    expected to assert non-empty result and surface a "has the CHECKSUM
    format changed?" message rather than silently skipping verification.
    """
    return [
        IsoChecksum(filename=m.group("name"), sha256=m.group("hash"))
        for m in _RE_GNU.finditer(payload)
    ]


# "# AlmaLinux-10.3-beta-1-aarch64-boot.iso: 1030670336 bytes" — a
# comment line ``sha256sum`` does not emit but the AlmaLinux publish
# step does. It is the release's own statement of how large each file
# should be, which is what lets a consumer tell a complete upload from
# a truncated or still-syncing one without downloading the ISO.
_RE_DECLARED_SIZE = re.compile(
    r"^#\s*(?P<name>\S+?):\s*(?P<size>\d+)\s+bytes\s*$",
    re.MULTILINE,
)


def declared_sizes(payload: str) -> dict[str, int]:
    """Map ``{iso filename: declared size in bytes}`` from CHECKSUM comments.

    Returns an empty dict when the file carries no size comments (they
    are not part of the ``sha256sum --tag`` format, so an older or
    hand-made CHECKSUM may have none). Callers must treat a missing
    entry as "nothing to cross-check here", not as a failure — the
    hashes remain the authoritative statement about content.
    """
    return {
        m.group("name"): int(m.group("size"))
        for m in _RE_DECLARED_SIZE.finditer(payload)
    }


def latest_alias_name(*, major: str, arch: str, kind: str, beta: bool) -> str:
    """Name of the version-independent alias for one ISO flavour.

    ``AlmaLinux-10-latest-aarch64-dvd.iso`` for stable,
    ``AlmaLinux-10-latest-beta-aarch64-dvd.iso`` for beta.

    These are the names the website, the docs and downstream tooling
    link to, so they have to exist for every arch that ships media: a
    publish that uploads only the versioned ISOs leaves every "latest"
    download link 404ing while the release itself looks complete.
    """
    beta_seg = "-beta" if beta else ""
    return f"AlmaLinux-{major}-latest{beta_seg}-{arch}-{kind}.iso"


def versioned_iso_re(
    *, version: str, arch: str, kind: str, beta: bool
) -> re.Pattern[str]:
    """Pattern matching the version-stamped ISO name for one flavour.

    Stable: ``AlmaLinux-10.2-aarch64-dvd.iso``.
    Beta: ``AlmaLinux-10.3-beta-1-aarch64-dvd.iso``.

    Beta needs a pattern rather than a predicted name because of that
    trailing counter: it is the respin number, a re-rolled beta
    publishes ``-beta-2``, and nothing in ``ALMA_VERSION`` says which
    respin is current. The counter is still required to be *present* —
    a beta ISO published without it would not be the file the release
    notes point at.
    """
    respin = r"-beta-\d+" if beta else ""
    return re.compile(
        rf"^AlmaLinux-{re.escape(version)}{respin}"
        rf"-{re.escape(arch)}-{re.escape(kind)}\.iso$"
    )
