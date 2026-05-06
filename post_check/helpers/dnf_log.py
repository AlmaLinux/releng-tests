"""Parser for dnf install logs and per-major allowlist of expected install failures.

The :func:`parse_skipped` helper extracts package names from the
``Skipped packages were:`` block dnf prints when transactions cannot be
satisfied. Pulling this regex out of the test code keeps it unit-testable
and avoids re-implementing it inline at every call site.

The :func:`load_allowlist` helper reads the per-major YAML allowlist of
packages we *expect* to fail to install (e.g. mutually-conflicting debug
packages). Per-major files give us cleaner git history than a single
mega-file.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent.parent

# dnf prints roughly: "Skipped packages were:\n  pkg1\n  pkg2\n"
# Also: "Error: Problem: ..." with names in "  - nothing provides X needed by Y"
_RE_SKIPPED_BLOCK = re.compile(
    r"Skipped\s+packages?\s+were\s*:\s*\n((?:\s+\S+.*\n)+)", re.IGNORECASE
)
_RE_PKG_LINE = re.compile(r"^\s+(\S+)", re.MULTILINE)


def parse_skipped(log_text: str) -> set[str]:
    """Return the set of package names dnf reported as skipped."""
    out: set[str] = set()
    for m in _RE_SKIPPED_BLOCK.finditer(log_text):
        for line_match in _RE_PKG_LINE.finditer(m.group(1)):
            out.add(line_match.group(1))
    return out


def load_allowlist(major: str) -> set[str]:
    """Load the allowlist of expected install failures for a major version."""
    path = ROOT / "tests" / "data" / f"allowed_install_failures-{major}.yaml"
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text()) or {}
    return set(data.get("allowed", []) or [])
