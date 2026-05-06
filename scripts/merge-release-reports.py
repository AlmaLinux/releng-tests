#!/usr/bin/env python3
"""Merge per-job ``release-report-*.md`` files into one consolidated report.

Why this exists
---------------
The pytest plugin in ``tests/conftest.py`` writes one markdown report per
pytest invocation. In CI we run pytest five times (host + four
container jobs), so the artifacts directory ends up with five separate
``release-report-*.md`` files. Pasting them straight into the run's
Summary tab produces five ``# AlmaLinux Release Test Report`` headers
and five ``## Summary by file`` tables — technically correct, but
operators have to mentally stitch them together to get the release-wide
verdict.

This script does that stitching mechanically: one header (with
aggregated totals), one ``Summary by file`` table whose columns are
the per-scope counts (host, x86_64, aarch64, …), and the verbose
per-test details collapsed under one ``<details>`` per scope so the
Summary tab stays scannable.

Why parse markdown instead of junit XML
---------------------------------------
The per-test details surfaced via ``report_detail`` (URLs, NEVRs, ISO
names) and the test docstrings rendered as blockquotes only live in
the markdown — they are not in junit. Reconstructing those from XML
would require a JSON sidecar from pytest (a bigger refactor); parsing
the markdown produced by the plugin is a self-contained alternative
that does not change the test layer.

Usage
-----
    python3 scripts/merge-release-reports.py <reports-dir> > merged.md

``<reports-dir>`` must be the directory ``actions/download-artifact``
unpacks into — i.e. one subdirectory per artifact, each holding the
job's ``release-report-*.md``. Subdir names are used as scope labels
in the merged ``Summary by file`` table (``host-report`` →
``host``, ``container-report-x86_64`` → ``x86_64``).

Pure stdlib on purpose: the summary job runs on the bare Ubuntu
runner without installing any packages.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------- parsing

# A single row of the per-report ``## Summary by file`` table.
@dataclass
class FileCounts:
    status: str
    passed: int
    skipped: int
    failed: int


@dataclass
class ParsedReport:
    """Structured view of one ``release-report-*.md`` file.

    Only the fields we actually need for merging are captured. The
    detail sections are kept as raw markdown — we paste them straight
    into the merged report, just nested under the scope's
    ``<details>`` block.
    """

    scope: str
    source: str = "?"
    version: str = "?"
    arches_per: str = "?"
    arches_parity: str = "?"
    verdict: str = "?"
    generated: str = "?"
    counts: dict[str, FileCounts] = field(default_factory=dict)
    details_md: str = ""


_HEADER_BULLETS = {
    "source": re.compile(r"^- \*\*Source\*\*: `([^`]*)`"),
    "version": re.compile(r"^- \*\*Version\*\*: `([^`]*)`"),
    "arches_per": re.compile(r"^- \*\*Arches \(per-arch tests\)\*\*: `([^`]*)`"),
    "arches_parity": re.compile(
        r"^- \*\*Arches \(parity tests, always full matrix\)\*\*: `([^`]*)`"
    ),
    "verdict": re.compile(r"^- \*\*Verdict\*\*: \*\*`([^`]*)`\*\*"),
    "generated": re.compile(r"^- \*\*Generated\*\*: `([^`]*)`"),
}

_SUMMARY_ROW = re.compile(
    r"^\| `([^`]+)` \| (\S+) \| (\d+) \| (\d+) \| (\d+) \|"
)


def _scope_from_dir(dirname: str) -> str:
    """``host-report`` → ``host``; ``container-report-x86_64`` → ``x86_64``.

    Anything that doesn't match these two known artifact-name patterns
    falls back to the directory name itself, so a renamed artifact
    still shows up labelled rather than silently disappearing.
    """
    if dirname == "host-report":
        return "host"
    if dirname.startswith("container-report-"):
        return dirname[len("container-report-") :]
    return dirname


def parse_report(md_path: Path, scope: str) -> ParsedReport:
    text = md_path.read_text()
    rep = ParsedReport(scope=scope)

    # 1. Header — the bullet list immediately after the title. Stop at
    # the first blank line that is not preceded by another bullet
    # (i.e. the blank between the bullets and ``## Summary by file``).
    for line in text.splitlines():
        if line.startswith("## "):
            break
        for field_name, pattern in _HEADER_BULLETS.items():
            m = pattern.match(line)
            if m:
                setattr(rep, field_name, m.group(1))

    # 2. Summary-by-file table. Sit between ``## Summary by file`` and
    # the next top-level ``## `` (which starts the per-file details).
    in_summary = False
    for line in text.splitlines():
        if line.startswith("## Summary by file"):
            in_summary = True
            continue
        if in_summary and line.startswith("## "):
            break
        if not in_summary:
            continue
        m = _SUMMARY_ROW.match(line)
        if m:
            name, status, p, s, fl = m.groups()
            rep.counts[name] = FileCounts(
                status=status,
                passed=int(p),
                skipped=int(s),
                failed=int(fl),
            )

    # 3. Detail sections — everything from the first per-file ``## `` to
    # the end. The plugin writes the file detail as ``## `<name>` —
    # <STATUS>``; the Summary table heading is the only other ``## ``
    # in the document and we've already passed it.
    detail_start: int | None = None
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("## `"):
            detail_start = idx
            break
    if detail_start is not None:
        rep.details_md = "\n".join(lines[detail_start:]).rstrip() + "\n"

    return rep


# ---------------------------------------------------------------- rendering

# Stable column order: host first, then arches alphabetically. Matches
# the order the plugin uses in its own ``Summary by file`` so a reader
# who knows the per-job report finds the merged one familiar.
def _scope_sort_key(scope: str) -> tuple[int, str]:
    return (0, "") if scope == "host" else (1, scope)


def _aggregate_status(passed: int, skipped: int, failed: int) -> str:
    """Mirror ``_status_label`` in tests/conftest.py."""
    if failed:
        return "FAIL"
    if passed == 0 and skipped > 0:
        return "SKIPPED"
    return "OK"


def _cell(counts: FileCounts | None) -> str:
    """Render one cell of the merged Summary-by-file table.

    ``None`` means the file did not run in this scope at all (e.g. the
    container-only ``test_dnf_install_all`` did not run on host) —
    show a ``—`` instead of zeros, otherwise the table reads as if the
    file ran and produced nothing.
    """
    if counts is None:
        return "—"
    return f"{counts.status} ({counts.passed}/{counts.skipped}/{counts.failed})"


def render_merged(reports: list[ParsedReport]) -> str:
    if not reports:
        return "_(no release reports found)_\n"

    # Source/Version are assumed identical across jobs (same workflow
    # input). If they're not, surface the divergence rather than
    # silently picking one — that would be a CI bug.
    sources = sorted({r.source for r in reports})
    versions = sorted({r.version for r in reports})
    parity_arches = sorted({r.arches_parity for r in reports})
    arches_per = sorted({a for r in reports for a in r.arches_per.split(",") if a})
    generated = sorted(r.generated for r in reports)

    # Aggregate totals across every scope's Summary table. We compute
    # them straight from the per-file counts so a single source of
    # truth drives both the table and the verdict.
    total_passed = sum(c.passed for r in reports for c in r.counts.values())
    total_skipped = sum(c.skipped for r in reports for c in r.counts.values())
    total_failed = sum(c.failed for r in reports for c in r.counts.values())
    verdict = "PASS" if total_failed == 0 else "FAIL"

    scopes = sorted({r.scope for r in reports}, key=_scope_sort_key)
    scope_counts: dict[str, dict[str, FileCounts]] = {r.scope: r.counts for r in reports}
    files = sorted({name for r in reports for name in r.counts})

    out: list[str] = []
    out.append("# AlmaLinux Release Test Report")
    out.append("")
    out.append(f"- **Generated**: `{generated[-1]}` (latest of {len(reports)} job report(s))")
    out.append(f"- **Source**: `{', '.join(sources)}`")
    out.append(f"- **Version**: `{', '.join(versions)}`")
    out.append(f"- **Arches (per-arch tests)**: `{','.join(arches_per)}`")
    out.append(
        f"- **Arches (parity tests, always full matrix)**: `{', '.join(parity_arches)}`"
    )
    out.append(f"- **Scopes covered**: {', '.join(f'`{s}`' for s in scopes)}")
    out.append(f"- **Verdict**: **`{verdict}`**")
    out.append(
        f"- **Totals**: {total_passed} passed, {total_skipped} skipped, "
        f"{total_failed} failed"
    )
    out.append("")

    # ---- Summary by file × scope.
    # Cell format: ``STATUS (P/S/F)``. Per-scope cells let the reader
    # see at a glance "the parity test failed only on host" vs.
    # "the dnf-install test failed only on s390x", which is exactly the
    # information that gets lost when we emit five separate tables.
    out.append("## Summary by file")
    out.append("")
    out.append("Cell format: `STATUS (passed/skipped/failed)`. `—` means the file did not run in that scope.")
    out.append("")
    header_cells = ["File", "Overall"] + scopes
    out.append("| " + " | ".join(header_cells) + " |")
    out.append("|" + "|".join(["---"] * len(header_cells)) + "|")

    for name in files:
        per_scope = [scope_counts.get(scope, {}).get(name) for scope in scopes]
        # Overall = sum across scopes.
        p = sum(c.passed for c in per_scope if c is not None)
        s = sum(c.skipped for c in per_scope if c is not None)
        fl = sum(c.failed for c in per_scope if c is not None)
        overall = f"{_aggregate_status(p, s, fl)} ({p}/{s}/{fl})"
        row = [f"`{name}`", overall] + [_cell(c) for c in per_scope]
        out.append("| " + " | ".join(row) + " |")
    out.append("")

    # ---- Per-scope detail blocks. We do NOT try to merge per-file
    # detail sections across scopes — the same function may run with
    # different parametrize variants per scope, and the docstrings /
    # ``report_detail`` lines are most naturally read together with
    # their siblings under the same job. Wrap each scope's full
    # detail section in <details> so the page stays compact.
    out.append("## Details by scope")
    out.append("")
    for rep in sorted(reports, key=lambda r: _scope_sort_key(r.scope)):
        if not rep.details_md:
            continue
        out.append(
            f"<details><summary><strong>Scope <code>{rep.scope}</code></strong> "
            f"&mdash; verdict <code>{rep.verdict}</code></summary>"
        )
        out.append("")
        out.append(rep.details_md)
        out.append("</details>")
        out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------- entry

def main(reports_dir: str) -> int:
    root = Path(reports_dir)
    if not root.is_dir():
        print(f"_(reports dir {root} does not exist)_", file=sys.stdout)
        return 0

    parsed: list[ParsedReport] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        # Each artifact contains exactly one md by name; if multiple
        # exist (e.g. xdist worker writes interleaved with the rename
        # step) we take the freshest by name, which is also the one
        # the rename step targeted.
        mds = sorted(sub.glob("release-report-*.md"))
        if not mds:
            continue
        parsed.append(parse_report(mds[-1], _scope_from_dir(sub.name)))

    print(render_merged(parsed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "reports"))
