"""Parser for AlmaLinux CHECKSUM files (after stripping the GPG clearsign wrapper).

Format: GNU/BSD-tag (``sha256sum --tag``):

    SHA256 (AlmaLinux-10.1-x86_64-dvd.iso) = <64 hex chars>

This is what AlmaLinux ships in ``isos/<arch>/CHECKSUM`` on the mirror.
We deliberately do NOT support the bare ``<hash>  <filename>`` layout —
AlmaLinux never publishes in that form, and supporting it would silently
accept malformed CHECKSUM files instead of failing them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


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
