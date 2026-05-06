"""Shared pytest fixtures.

`synthetic_repos_rpm` builds a tiny RPM (lead + minimal signature/main
headers + gzipped cpio payload) in pure Python that mimics the layout
of ``almalinux-repos`` — a single ``.repo`` file under
``/etc/yum.repos.d/``. Building it in Python avoids depending on
``rpmbuild``/``rpm2cpio`` which are not installed on
``ubuntu-latest`` by default and would otherwise add an apt step in
CI. The artefact lives in the session tmp dir; we never commit a
binary RPM under ``tests/data/``.
"""
from __future__ import annotations

import gzip
import inspect
import os
import struct
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from post_check.config import (
    RuntimeConfig,
    arch_skip_categories,
    arch_supports_major,
    load_architectures,
    load_sources,
)

# ============================================================ release reporter
# Per-nodeid outcome accumulator for tests carrying ``pytest.mark.release``.
# Populated in ``pytest_runtest_logreport``; rendered in ``pytest_unconfigure``.
# Outcome priority (higher = stickier): failed/error > skipped > passed.
_RELEASE_OUTCOMES: dict[str, tuple[str, str]] = {}
_RELEASE_PRIORITY = {"failed": 4, "error": 4, "skipped": 2, "passed": 1}

# Per-test-function docstring captured at collection time, keyed by the
# parametrize-stripped nodeid (e.g. ``tests/release/test_repomd_signature.py::
# test_repomd_signature``). Lets the markdown report show *what* every
# test verifies, not just whether it passed — a release reviewer doesn't
# always know the suite well enough to read intent off the test name.
_RELEASE_DOCSTRINGS: dict[str, str] = {}

# Per-nodeid (parametrize variant included!) free-form list of
# "concrete things this test ran against" — populated by tests via the
# ``report_detail`` fixture below. Examples: a URL that returned 200,
# the NEVR of a package that matched, an ISO name + size, etc. Surfaced
# under each variant's bullet in the markdown report so an operator
# doesn't have to read source code to know what was actually checked.
_RELEASE_DETAILS: dict[str, list[str]] = {}


def _record_release_outcome(nodeid: str, outcome: str, message: str) -> None:
    """Set the outcome for ``nodeid`` if it's worse than what's already there.

    A test goes through up to three phases (setup/call/teardown). We want
    the FINAL operator-visible outcome — the worst of all phases — and we
    don't want a passing teardown to overwrite a failing call.
    """
    prev = _RELEASE_OUTCOMES.get(nodeid)
    if prev is not None:
        prev_outcome, _ = prev
        if _RELEASE_PRIORITY.get(prev_outcome, 0) >= _RELEASE_PRIORITY.get(outcome, 0):
            return
    _RELEASE_OUTCOMES[nodeid] = (outcome, message)


def _extract_skip_message(report) -> str:
    """Pull the human-readable reason out of a ``skipped`` TestReport."""
    if isinstance(report.longrepr, tuple) and len(report.longrepr) >= 3:
        msg = report.longrepr[2]
        if isinstance(msg, str) and msg.startswith("Skipped: "):
            msg = msg[len("Skipped: ") :]
        return str(msg)
    return str(report.longrepr) if report.longrepr else ""


def pytest_runtest_logreport(report) -> None:
    """Capture per-test outcomes for tests marked ``pytest.mark.release``.

    Only release-marked tests are recorded — everything else (unit tests
    of helpers, suite-level contract tests for YAML/CI) is irrelevant to
    the operator-facing release report.
    """
    if "release" not in report.keywords:
        return

    if report.when == "setup":
        if report.outcome == "skipped":
            _record_release_outcome(report.nodeid, "skipped", _extract_skip_message(report))
        elif report.outcome == "failed":
            _record_release_outcome(report.nodeid, "error", report.longreprtext or "setup failed")
    elif report.when == "call":
        if report.outcome == "passed":
            _record_release_outcome(report.nodeid, "passed", "")
        elif report.outcome == "failed":
            _record_release_outcome(report.nodeid, "failed", report.longreprtext or "")
        elif report.outcome == "skipped":
            _record_release_outcome(report.nodeid, "skipped", _extract_skip_message(report))
    elif report.when == "teardown" and report.outcome == "failed":
        # Teardown failure is rare; record it only if nothing worse exists.
        _record_release_outcome(report.nodeid, "error", report.longreprtext or "teardown failed")


def _aggregate_by_file() -> list[tuple[str, list[tuple[str, str, str]]]]:
    """Group recorded outcomes by source file, sorted alphabetically."""
    by_file: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for nodeid, (outcome, message) in _RELEASE_OUTCOMES.items():
        file, _, test_id = nodeid.partition("::")
        by_file[file].append((test_id or nodeid, outcome, message))
    return sorted(by_file.items(), key=lambda kv: kv[0])


def _status_label(passed: int, skipped: int, failed: int) -> str:
    """Pick the file/function-level verdict label.

    Three labels, in priority order:

    * ``FAIL`` — anything failed/errored. Has to win over skips, since
      a file with one fail and ten skips is still a regression.
    * ``SKIPPED`` — nothing failed and nothing actually executed (all
      tests were skipped). Calling this ``OK`` was misleading: the
      report would read "test_X — OK" while the body said
      "0 passed, 5 skipped, 0 failed", which mis-claims coverage.
    * ``OK`` — at least one test passed and nothing failed.

    Empty input collapses to ``OK`` for safety, but in practice an
    entry with zero of everything never reaches the report (it would
    not have been recorded).
    """
    if failed:
        return "FAIL"
    if passed == 0 and skipped > 0:
        return "SKIPPED"
    return "OK"


def _print_release_summary_to_stdout() -> None:
    """One line per release test FILE: ``test_X - OK`` / ``test_X - FAIL``.

    Goes to ``sys.stdout`` directly (no pytest writer) because we run
    inside ``pytest_unconfigure``, which fires after the terminal reporter
    has finished — the writer would be torn down by then.
    """
    import sys

    files = _aggregate_by_file()
    if not files:
        return

    sys.stdout.write("\n")
    sys.stdout.write("=" * 28 + " Release tests " + "=" * 28 + "\n")
    for file, entries in files:
        name = Path(file).stem
        passed = sum(1 for _, o, _ in entries if o == "passed")
        failed = sum(1 for _, o, _ in entries if o in ("failed", "error"))
        skipped = sum(1 for _, o, _ in entries if o == "skipped")
        status = _status_label(passed, skipped, failed)
        line = f"  {name:<32s} - {status}"
        # Show counts only when there's something interesting beyond "all passed".
        if failed or skipped:
            line += f"  ({passed} passed, {skipped} skipped, {failed} failed)"
        sys.stdout.write(line + "\n")
    sys.stdout.write("=" * 71 + "\n")


def _function_id_and_variant(test_id: str) -> tuple[str, str | None]:
    """Split a parametrized ``nodeid`` test_id into ``(func_name, variant)``.

    Examples:
      ``test_foo``                 → ``("test_foo", None)``
      ``test_foo[x86_64]``         → ``("test_foo", "x86_64")``
      ``test_foo[aarch64-BaseOS]`` → ``("test_foo", "aarch64-BaseOS")``
    """
    if "[" not in test_id:
        return test_id, None
    name, _, rest = test_id.partition("[")
    return name, rest.rstrip("]")


def _extract_failure_message(longreprtext: str | None) -> str:
    """Pull the operator-readable failure message out of pytest's longrepr.

    pytest's traceback for a failed test bundles a lot of stuff most
    release reviewers do not want in the markdown artefact:

    * the full ``def test_…`` source (docstring + body, often 30+ lines),
    * fixture argument reprs (``runtime_config = RuntimeConfig(...)``),
    * the failing source line marked with ``>``,
    * the file:line footer (``tests/foo.py:42: AssertionError``),
    * a trailing ``assert <expr-repr>`` line auto-emitted by pytest's
      assertion rewriter — duplicates the message we already have.

    The bit that actually answers "what failed" is the contiguous
    block of lines prefixed with ``E`` + whitespace (the assertion
    message and its continuation lines for multi-line messages).
    Pytest's prefix width depends on the ``--tb`` style (``E   ``
    under ``--tb=short``, ``E       `` under ``--tb=long`` or
    ``--tb=auto`` for an indented test) — we detect the prefix from
    the first ``E``-marked line and strip exactly that many chars
    from every subsequent line, preserving the message's internal
    indentation.

    Final cleanup:

    * drop the auto-appended ``assert <expr-repr>`` tail (and any
      ``+ where`` / ``+ and`` follow-ups pytest emits for it);
    * strip the exception-class prefix (``AssertionError: ``,
      ``Failed: ``) from the first line — when the test author
      wrote a deliberate message, the class name is just noise.

    Returns ``""`` for empty/None input — caller falls back to a
    placeholder.
    """
    if not longreprtext:
        return ""

    # Detect the ``E`` + N-spaces prefix from the first matching line.
    # Lines that are exactly ``E`` (no spaces, no content) are valid
    # blank continuations but don't establish the prefix — we keep
    # scanning for a content line.
    prefix: str | None = None
    for line in longreprtext.splitlines():
        if line.startswith("E ") and len(line) > 2 and line[2:].strip():
            i = 1
            while i < len(line) and line[i] == " ":
                i += 1
            prefix = line[:i]
            break
    if prefix is None:
        # No content-bearing E-line at all (e.g. teardown failure with
        # only a class name) — return empty so the caller falls back
        # to "(no message captured)".
        return ""

    e_lines: list[str] = []
    for line in longreprtext.splitlines():
        if line.startswith(prefix):
            e_lines.append(line[len(prefix):])
        elif line == "E" or (line.startswith("E ") and line[1:].strip() == ""):
            # Blank continuation line inside a multi-paragraph message.
            e_lines.append("")

    # pytest's assert rewriter appends a final ``assert <expr-repr>``
    # line (and sometimes ``+ where ...`` / ``+ and ...`` follow-ups)
    # that just repeats the same content in expression form. Strip it.
    while e_lines and (
        e_lines[-1].lstrip().startswith("assert ")
        or e_lines[-1].lstrip().startswith("+ where ")
        or e_lines[-1].lstrip().startswith("+ and ")
    ):
        e_lines.pop()

    # Drop the exception-class prefix on the first line — when the
    # message is deliberately written by the test, the class name
    # ("AssertionError:", "Failed:") adds nothing the operator needs.
    if e_lines:
        first = e_lines[0]
        for cls_prefix in ("AssertionError: ", "Failed: "):
            if first.startswith(cls_prefix):
                e_lines[0] = first[len(cls_prefix):]
                break

    return "\n".join(e_lines).rstrip()


def _format_docstring_as_blockquote(doc: str) -> str:
    """Render a Python docstring as a tidy markdown blockquote.

    pytest gives us ``inspect.getdoc``-cleaned text (leading whitespace
    stripped, common indentation removed), so we just prepend ``> `` to
    each line. Blank lines inside the docstring map to ``>`` (a "blank"
    blockquote line) so the rendered output keeps paragraph structure.
    """
    out = []
    for line in doc.splitlines():
        out.append(f"> {line}" if line.strip() else ">")
    return "\n".join(out)


def _write_release_report_file(report_path: Path) -> None:
    """Render the detailed markdown report under ``reports/release-report-<ts>.md``.

    Layout (per release test file → per test function → per parametrize
    variant):

    * **File header** — overall OK/FAIL + per-status counts.
    * **Per-function block** — function name + the test's own docstring
      (so a release reviewer reads *what is being verified* without
      having to open the test source). Variant statuses are listed
      under the description.
    * **Failure detail** — full traceback inside a fenced code block,
      so a green release report stays scannable but a red one carries
      everything needed to debug.
    """
    files = _aggregate_by_file()
    if not files:
        return

    report_path.parent.mkdir(parents=True, exist_ok=True)

    total_passed = total_failed = total_skipped = 0
    for _, entries in files:
        total_passed += sum(1 for _, o, _ in entries if o == "passed")
        total_failed += sum(1 for _, o, _ in entries if o in ("failed", "error"))
        total_skipped += sum(1 for _, o, _ in entries if o == "skipped")
    verdict = "PASS" if total_failed == 0 else "FAIL"

    status_glyph = {"passed": "✅", "skipped": "⚠️", "failed": "❌", "error": "❌"}

    # The operator's ``ALMA_ARCHES`` env var drives per-arch tests
    # (test_repomd_signature[<arch>-...], test_iso_*, test_mirrorlist[<arch>],
    # the docker-based upgrade tests…) but parity tests deliberately
    # ignore it — N-E-V-R parity / noarch checksum parity are
    # release-level invariants and must hold across every published
    # arch. We surface both in the header so a reader who sees a parity
    # result mentioning ``aarch64`` while ALMA_ARCHES=x86_64 isn't
    # confused.
    arches_env = os.environ.get("ALMA_ARCHES", "x86_64")
    parity_arches = ",".join(sorted(load_architectures().keys()))

    with report_path.open("w") as f:
        f.write("# AlmaLinux Release Test Report\n\n")
        f.write(
            f"- **Generated**: `{datetime.now(timezone.utc).isoformat(timespec='seconds')}`\n"
        )
        f.write(f"- **Source**: `{os.environ.get('ALMA_SOURCE', '?')}`\n")
        f.write(f"- **Version**: `{os.environ.get('ALMA_VERSION', '?')}`\n")
        f.write(f"- **Arches (per-arch tests)**: `{arches_env}`\n")
        f.write(
            f"- **Arches (parity tests, always full matrix)**: "
            f"`{parity_arches}`\n"
        )
        f.write(f"- **Verdict**: **`{verdict}`**\n")
        f.write(
            f"- **Totals**: {total_passed} passed, {total_skipped} skipped, "
            f"{total_failed} failed\n\n"
        )

        f.write("## Summary by file\n\n")
        f.write("| File | Status | Passed | Skipped | Failed |\n")
        f.write("|---|---|---:|---:|---:|\n")
        for file, entries in files:
            name = Path(file).stem
            passed = sum(1 for _, o, _ in entries if o == "passed")
            failed = sum(1 for _, o, _ in entries if o in ("failed", "error"))
            skipped = sum(1 for _, o, _ in entries if o == "skipped")
            status = _status_label(passed, skipped, failed)
            f.write(f"| `{name}` | {status} | {passed} | {skipped} | {failed} |\n")
        f.write("\n")

        for file, entries in files:
            name = Path(file).stem
            file_passed = sum(1 for _, o, _ in entries if o == "passed")
            file_failed = sum(1 for _, o, _ in entries if o in ("failed", "error"))
            file_skipped = sum(1 for _, o, _ in entries if o == "skipped")
            file_status = _status_label(file_passed, file_skipped, file_failed)
            f.write(f"## `{name}` — {file_status}\n\n")

            # Group entries by function name (parametrize variants share
            # docstring + code path, so showing them as a single block is
            # both more compact and more informative).
            by_func: dict[str, list[tuple[str | None, str, str]]] = {}
            func_order: list[str] = []
            for tid, outcome, msg in entries:
                func, variant = _function_id_and_variant(tid)
                if func not in by_func:
                    by_func[func] = []
                    func_order.append(func)
                by_func[func].append((variant, outcome, msg))

            for func in func_order:
                func_entries = by_func[func]
                p = sum(1 for _, o, _ in func_entries if o == "passed")
                s = sum(1 for _, o, _ in func_entries if o == "skipped")
                fl = sum(1 for _, o, _ in func_entries if o in ("failed", "error"))
                func_status = _status_label(p, s, fl)
                # Counts only when the picture isn't "all passed".
                counts = ""
                if fl or s:
                    counts = f"  ({p} passed, {s} skipped, {fl} failed)"

                f.write(f"### `{func}` — {func_status}{counts}\n\n")

                # What the test actually checks — straight from its
                # docstring. Without this, the report just says "passed";
                # with it, the operator sees *which invariant* passed.
                doc = _RELEASE_DOCSTRINGS.get(f"{file}::{func}")
                if doc:
                    f.write(_format_docstring_as_blockquote(doc) + "\n\n")
                else:
                    f.write("> _(no docstring on the test function)_\n\n")

                # Per-variant outcomes. Bullets are short — variant id +
                # glyph; failure tracebacks come below; each test's
                # "what was checked" lines (set via ``report_detail``)
                # nest under the bullet so the operator sees concrete
                # URLs / NEVRs / ISO names without opening the source.
                for variant, outcome, msg in func_entries:
                    glyph = status_glyph.get(outcome, "•")
                    label = f"`[{variant}]`" if variant else "(no parametrize)"
                    if outcome == "skipped":
                        if msg:
                            f.write(f"- {glyph} {label} — _skipped:_ {msg}\n")
                        else:
                            f.write(f"- {glyph} {label} — _skipped_\n")
                    else:
                        f.write(f"- {glyph} {label}\n")

                    # Reconstruct full nodeid to look up details. The
                    # ``test_id`` field of an entry already includes
                    # the parametrize id, so we can rebuild it 1:1.
                    test_id_for_lookup = (
                        f"{func}[{variant}]" if variant else func
                    )
                    nodeid = f"{file}::{test_id_for_lookup}"
                    for detail in _RELEASE_DETAILS.get(nodeid, []):
                        f.write(f"    - {detail}\n")
                f.write("\n")

                # Failure detail folds in once per failing variant —
                # kept out of the summary list above so a green run
                # stays clean and a red run stays debuggable. We render
                # only the assertion message (not the full pytest
                # traceback with test source + arg reprs); for a
                # release artefact "what failed" is enough, and the
                # full traceback is still in stdout / pytest's own
                # output for anyone debugging.
                for variant, outcome, msg in func_entries:
                    if outcome not in ("failed", "error"):
                        continue
                    label = f"[{variant}]" if variant else func
                    summary = (
                        "Failure detail" if outcome == "failed" else "Error detail"
                    )
                    f.write(
                        f"<details><summary>{summary} — <code>{label}</code>"
                        f"</summary>\n\n"
                    )
                    f.write("```\n")
                    extracted = _extract_failure_message(msg or "")
                    rendered_lines = (
                        extracted.splitlines()
                        if extracted
                        else ["(no message captured)"]
                    )
                    for line in rendered_lines:
                        f.write(f"{line}\n")
                    f.write("```\n\n</details>\n\n")
# ============================================================ end release reporter


@pytest.fixture(scope="session")
def runtime_config() -> RuntimeConfig:
    """Run parameters: source/version/arches/repos from env vars."""
    return RuntimeConfig.from_env()


@pytest.fixture
def report_detail(request):
    """Sink for per-test "what specifically was checked" lines.

    Tests call ``report_detail("GET https://... → 200")`` once per
    artifact verified; the lines show up indented under the test's
    bullet in the markdown release report. The point: a release
    reviewer reading a green ✅ should still be able to see the
    concrete URLs / NEVRs / ISO names that the assertion ran
    against, instead of having to crack open the source.

    Each call appends; ordering is preserved. Strings should be
    short single lines — anything longer goes in the test's
    docstring (rendered above the bullets).
    """
    nodeid = request.node.nodeid

    def _record(item: str) -> None:
        _RELEASE_DETAILS.setdefault(nodeid, []).append(item)

    return _record


@pytest.fixture(scope="session")
def gpg_key_path(runtime_config) -> Path:
    """Path to the dynamically downloaded key for the ALMA_VERSION major.

    Cache — ``~/.cache/post-check/gpg-keys/RPM-GPG-KEY-AlmaLinux-<major>``.
    The import is done lazily so that offline unit tests don't fail when
    there is no network: if the key cannot be downloaded, the test is
    skipped rather than failing.
    """
    from post_check.helpers import gpg

    major = runtime_config.version.split(".")[0]
    try:
        return gpg.fetch_release_key(major)
    except Exception as e:  # network/HTTP failure
        pytest.skip(f"failed to download GPG key for major={major}: {e}")


@pytest.fixture(scope="session")
def gpg_keyring(runtime_config, tmp_path_factory):
    """Sandboxed Keyring with the imported release key for the ALMA_VERSION major.

    If the key cannot be downloaded/imported (no network, fingerprint
    issue, etc.), the test is skipped so that offline scenarios don't fail
    for infrastructure reasons.
    """
    from post_check.helpers.gpg import Keyring

    home = tmp_path_factory.mktemp("gpg-session")
    kr = Keyring(home / "gpg")
    major = runtime_config.version.split(".")[0]
    try:
        kr.import_release_key(major)
    except Exception as e:
        pytest.skip(f"failed to import GPG key for major={major}: {e}")
    return kr


@pytest.fixture(scope="session")
def sources_cfg() -> dict:
    return load_sources()


@pytest.fixture(scope="session")
def architectures_cfg() -> dict:
    return load_architectures()


# ---------------------------------------------------------------- arch-driven skips
# Single source of truth for "which test files implement which test
# category". Read by ``_skip_unsupported_arch`` together with the per-arch
# ``skip_categories`` field in ``config/architectures.yaml`` to gracefully
# skip tests that don't apply to a given arch (e.g. i686 has no
# AlmaLinux container image and no ISO media — both are skipped via the
# YAML rather than littering each test with ``if arch == "i686":`` checks).
_ARCH_SKIP_FILE_CATEGORIES: dict[str, str] = {
    "tests/release/test_iso_checksums.py": "iso",
    "tests/release/test_dnf_upgrade.py": "upgrade",
}


@pytest.fixture(autouse=True)
def _skip_unsupported_arch(request):
    """Skip arch-parametrized tests that don't apply to the variant's arch.

    Two skip rules, both driven by ``config/architectures.yaml``:

    1. **supported_majors** — if the arch's ``supported_majors`` list
       does not include ``ALMA_VERSION``'s major, every test variant for
       that arch is skipped (e.g. ``x86_64_v2`` only on AL10).
    2. **skip_categories** — for each category the arch opts out of
       (``container``/``iso``/``upgrade``), tests in the matching files
       (or carrying ``requires_docker`` for ``container``) are skipped.

    Tests without an ``arch`` parametrization are unaffected — those
    are typically the cross-arch parity tests, which always cover the
    full matrix and are not arch-specific.
    """
    callspec = getattr(request.node, "callspec", None)
    if callspec is None:
        return
    arch = callspec.params.get("arch")
    if arch is None:
        return

    # rule 1 — major support
    version = os.environ.get("ALMA_VERSION", "")
    major = version.split(".")[0] if version else ""
    if major and not arch_supports_major(arch, major):
        pytest.skip(
            f"arch={arch} not supported on AlmaLinux {major} "
            f"(see config/architectures.yaml supported_majors)"
        )

    # rule 2 — per-arch test-category opt-outs
    cats = arch_skip_categories(arch)
    if not cats:
        return
    if "container" in cats and "requires_docker" in request.node.keywords:
        pytest.skip(
            f"arch={arch}: container tests skipped "
            f"(see config/architectures.yaml skip_categories: container)"
        )
    test_file = request.node.nodeid.split("::", 1)[0]
    file_cat = _ARCH_SKIP_FILE_CATEGORIES.get(test_file)
    if file_cat and file_cat in cats:
        pytest.skip(
            f"arch={arch}: {file_cat} tests skipped "
            f"(see config/architectures.yaml skip_categories: {file_cat})"
        )


def _already_parametrized(metafunc, argname: str) -> bool:
    """True if the test already parametrizes ``argname`` itself via ``@pytest.mark.parametrize``.

    Without this check, conftest would add a second parametrization on top
    of the existing one, and pytest would fail with ``duplicate
    parametrization`` at the collection stage (for example,
    ``test_run_in_arch_executes_uname_m`` in ``tests/internal/test_docker_helper.py``
    iterates over all 4 arches itself).
    """
    for marker in metafunc.definition.iter_markers("parametrize"):
        if not marker.args:
            continue
        names = [n.strip() for n in str(marker.args[0]).split(",")]
        if argname in names:
            return True
    return False


def pytest_generate_tests(metafunc):
    """Parametrization over arches/repos from env vars.

    A test with the `arch` fixture runs once per arch from ALMA_ARCHES;
    a test with the `repo` fixture runs per repo from ALMA_REPOS (or
    the source's default repo set if ALMA_REPOS is not set).

    If the test parametrizes `arch`/`repo` itself via
    ``@pytest.mark.parametrize`` (for example, the docker helper
    integration test always wants all 4 arches), we don't duplicate —
    otherwise collection fails with ``duplicate parametrization``.
    """
    needs_arch = "arch" in metafunc.fixturenames and not _already_parametrized(metafunc, "arch")
    needs_repo = "repo" in metafunc.fixturenames and not _already_parametrized(metafunc, "repo")
    if not (needs_arch or needs_repo):
        return
    cfg = RuntimeConfig.from_env()
    if needs_arch:
        metafunc.parametrize("arch", cfg.arches, ids=lambda a: a)
    if needs_repo:
        metafunc.parametrize("repo", cfg.repos, ids=lambda r: r)


def pytest_collection_modifyitems(config, items):
    """Automatic marking of parametrized tests.

    Adds the ``slow`` marker to ``test_dnf_install_all`` on the slow
    arches (s390x/ppc64le under QEMU can run for hours).

    This used to also skip incompatible ``(arch, source)`` pairs based
    on ``architectures.yaml::sources_supported``, but on AlmaLinux 9/10
    all four arches support all three sources, so that field was
    deleted as dead. If a future arch ever lacks support for one of
    the sources, the skip logic should be added back right here.
    """
    slow_arches = {"s390x", "ppc64le"}

    for item in items:
        # ---- capture release-test docstrings for the markdown report ----
        # Done once per *function* (parametrize variants share a docstring),
        # keyed by the parametrize-stripped nodeid: ``file::func``. The
        # docstring is the test's own description of what's being checked
        # — exactly what the report consumer wants to see next to a status.
        if "release" in item.keywords:
            file_func = item.nodeid.split("[", 1)[0]
            if file_func not in _RELEASE_DOCSTRINGS:
                func = getattr(item, "function", None)
                doc = inspect.getdoc(func) if func is not None else None
                if doc:
                    _RELEASE_DOCSTRINGS[file_func] = doc

        # ---- slow marker for QEMU-emulated container tests ----
        params = getattr(item, "callspec", None)
        if not params:
            continue
        arch_param = params.params.get("arch")
        if arch_param in slow_arches and "test_dnf_install_all" in item.name:
            item.add_marker(pytest.mark.slow)


_REPO_FILE_BODY = b"""\
[almalinux-baseos]
name=AlmaLinux $releasever - BaseOS
baseurl=https://repo.almalinux.org/almalinux/$releasever/BaseOS/$basearch/os/
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10
gpgcheck=1
enabled=1
"""


def _cpio_entry(name: bytes, content: bytes, mode: int) -> bytes:
    """Encode a single entry in cpio "new ASCII" format (magic 070701).

    13 hex fields × 8 chars each, then the NUL-terminated name padded
    to a 4-byte boundary, then the data padded to a 4-byte boundary.
    Matches what ``rpmfile.RPMInfo._read_new`` expects to parse.
    """
    name_with_nul = name + b"\x00"
    fields = [
        0,                  # inode
        mode,               # mode (file type bits | perms)
        0,                  # uid
        0,                  # gid
        1,                  # nlink
        0,                  # mtime
        len(content),       # filesize
        0, 0, 0, 0,         # devmajor/minor/rdev major/minor
        len(name_with_nul), # namesize incl. trailing NUL
        0,                  # check
    ]
    header = b"070701" + b"".join(f"{v:08x}".encode("ascii") for v in fields)
    entry = header + name_with_nul
    entry += b"\x00" * ((4 - (len(entry) % 4)) % 4)
    entry += content
    entry += b"\x00" * ((4 - (len(entry) % 4)) % 4)
    return entry


def _empty_rpm_header() -> bytes:
    """Header section with zero entries.

    Layout (per RPM 'header structure'):
    - magic ``\\x8e\\xad\\xe8`` + version ``0x01``
    - 4 reserved bytes
    - 4-byte big-endian num_entries
    - 4-byte big-endian header_structure_size (the "store")
    - entries × 16 bytes (none here)
    - store bytes (none here)
    """
    return (
        b"\x8e\xad\xe8\x01"  # magic + version
        + b"\x00\x00\x00\x00"  # reserved
        + struct.pack("!i", 0)  # num_entries
        + struct.pack("!i", 0)  # store size
    )


def _build_synthetic_rpm() -> bytes:
    repo_path = b"./etc/yum.repos.d/almalinux.repo"
    file_entry = _cpio_entry(repo_path, _REPO_FILE_BODY, mode=0o100644)
    # cpio archive ends with a "TRAILER!!!" entry (size=0)
    trailer = _cpio_entry(b"TRAILER!!!", b"", mode=0)
    payload = gzip.compress(file_entry + trailer)

    # 96-byte lead: !4sBBhh66shh16s
    lead = struct.pack(
        "!4sBBhh66shh16s",
        b"\xed\xab\xee\xdb",      # magic
        3, 0,                     # major, minor
        0,                        # type (binary)
        1,                        # archnum (i386 — value is irrelevant for tests)
        b"synthetic-repos-1.0-1", # name (NUL-padded by struct)
        1,                        # osnum
        5,                        # signature_type
        b"\x00" * 16,             # reserved
    )

    sig_header = _empty_rpm_header()
    main_header = _empty_rpm_header()
    return lead + sig_header + main_header + payload


@pytest.fixture(scope="session")
def synthetic_repos_rpm() -> bytes:
    """Bytes of a minimal RPM with one `.repo` under /etc/yum.repos.d/.

    Built in-memory; no on-disk artefact is created or committed.
    """
    return _build_synthetic_rpm()


# ============================================================ almalinux-repos package fixtures
# Shared by:
#   * test_almalinux_repos_pkg.py — direct package-content checks
#   * test_mirrorlist.py — derives the (section_id, mirrorlist, baseurl)
#     list from the package itself instead of hardcoding it from config
#
# Lives here (rather than in either test file) so collection works
# regardless of test-file order and we don't introduce cross-test imports.


def _download_baseos_pkg(runtime_config, name: str) -> bytes | None:
    """Download the ``.rpm`` of package ``name`` from BaseOS of ``runtime_config``.

    Returns None if the package is not in BaseOS's ``primary.xml``. Callers
    decide whether that's a hard fail (almalinux-repos must exist) or a
    soft skip (almalinux-gpg-keys is optional pre-AL10).

    For ``pulp`` this returns the **current GA** package (the major-alias
    BaseOS) — used by the upgrade flow as the base layer's ``.repo``
    files, on top of which the unsigned internal-beta is layered.
    Validation of the upgrade-target package itself goes through
    :func:`_download_target_pkg` instead.
    """
    from post_check.helpers import http, repodata
    from post_check.helpers.url_builder import RepoURL

    s = http.session()
    arch = runtime_config.arches[0]
    u = RepoURL.from_config(
        source=runtime_config.source,
        version=runtime_config.version,
        arch=arch,
        repo="BaseOS",
    )
    primary = repodata.find_primary_xml_url(s, u.repo_base())
    pkg = next(
        (p for p in repodata.iter_packages(s, primary) if p.name == name),
        None,
    )
    if pkg is None:
        return None
    return s.get(u.repo_base() + "/" + pkg.location).content


def _download_target_pkg(runtime_config, name: str) -> bytes | None:
    """Download ``name`` from the **version-under-test** repository.

    Distinct from :func:`_download_baseos_pkg` only on ``pulp``:

    * stable / beta / pungi — the package shipped in BaseOS *is* the
      version under test (the operator's ``ALMA_VERSION`` matches the
      published release), so this just delegates to BaseOS.
    * pulp — ``ALMA_VERSION`` is the upgrade target (e.g. ``10.2``)
      whose package lives in the layered internal-beta repo on
      ``build.almalinux.org/pulp/content/...``, NOT on the
      major-aliased stable URL (which carries the current GA,
      ``10.1``). Re-using ``_download_baseos_pkg`` here would
      validate the wrong package and the operator would end up
      reading the current GA's ``.repo`` files in a "10.2 release
      report".

    The pulp internal-beta is a flat per-arch repo (no
    BaseOS / AppStream split), so we look for ``name`` in its single
    ``primary.xml``. Returns None on absence — same contract as
    ``_download_baseos_pkg`` (callers distinguish hard vs. soft).
    """
    from post_check.helpers import http, repodata
    from post_check.helpers.url_builder import pulp_internal_beta_repo_base

    if runtime_config.source != "pulp":
        return _download_baseos_pkg(runtime_config, name)

    s = http.session()
    arch = runtime_config.arches[0]
    repo_base = pulp_internal_beta_repo_base(
        version=runtime_config.version, arch=arch
    )
    primary = repodata.find_primary_xml_url(s, repo_base)
    # The internal-beta typically carries multiple builds of the same
    # package (e.g. ``almalinux-repos-10.2-0.3.el10`` AND
    # ``-0.4.el10``). We want the **latest** — that's what dnf would
    # actually pick during the upgrade — so iterate through all and
    # keep the one with the highest E-V-R.
    from post_check.helpers.rpm_evr import evr_cmp

    latest = None
    for p in repodata.iter_packages(s, primary):
        if p.name != name:
            continue
        if latest is None or evr_cmp(
            (p.epoch, p.version, p.release),
            (latest.epoch, latest.version, latest.release),
        ) > 0:
            latest = p
    if latest is None:
        return None
    return s.get(repo_base + "/" + latest.location).content


@pytest.fixture(scope="session")
def almalinux_repos_pkg_bytes(runtime_config) -> bytes:
    """Current-GA ``almalinux-repos`` package — used by the upgrade
    flow as the base-layer ``.repo`` files. On non-pulp sources this
    is identical to the version-under-test package; on pulp it's the
    major-aliased current GA. Validation of the version-under-test
    package goes through :func:`target_repos_pkg_bytes`.
    """
    rpm_bytes = _download_baseos_pkg(runtime_config, "almalinux-repos")
    assert rpm_bytes, "almalinux-repos not found in BaseOS"
    return rpm_bytes


@pytest.fixture(scope="session")
def almalinux_release_pkg_bytes(runtime_config) -> bytes:
    """Current-GA ``almalinux-release`` package — see
    :func:`almalinux_repos_pkg_bytes` for the per-source semantics.
    """
    rpm_bytes = _download_baseos_pkg(runtime_config, "almalinux-release")
    assert rpm_bytes, "almalinux-release not found in BaseOS"
    return rpm_bytes


@pytest.fixture(scope="session")
def almalinux_gpg_keys_pkg_bytes(runtime_config) -> bytes | None:
    """Bytes of current-GA ``almalinux-gpg-keys`` if it is published
    separately (AL10+). Returns ``None`` for AL9, where the keys
    still live inside ``almalinux-release`` and the standalone
    package may not exist.
    """
    return _download_baseos_pkg(runtime_config, "almalinux-gpg-keys")


# ---- version-under-test ("target") variants ---------------------------------
# Same packages but downloaded from the location that actually carries the
# version the operator is validating. Used by ``test_almalinux_repos_pkg`` —
# checking the current GA's package would be a no-op duplicate of stable runs.

@pytest.fixture(scope="session")
def target_repos_pkg_bytes(runtime_config) -> bytes:
    """``almalinux-repos`` of the version-under-test (= upgrade target
    on pulp; same as ``almalinux_repos_pkg_bytes`` elsewhere).
    """
    rpm_bytes = _download_target_pkg(runtime_config, "almalinux-repos")
    assert rpm_bytes, (
        f"almalinux-repos not found for "
        f"`{runtime_config.source}/{runtime_config.version}`"
    )
    return rpm_bytes


@pytest.fixture(scope="session")
def target_release_pkg_bytes(runtime_config) -> bytes:
    """``almalinux-release`` of the version-under-test."""
    rpm_bytes = _download_target_pkg(runtime_config, "almalinux-release")
    assert rpm_bytes, (
        f"almalinux-release not found for "
        f"`{runtime_config.source}/{runtime_config.version}`"
    )
    return rpm_bytes


@pytest.fixture(scope="session")
def target_gpg_keys_pkg_bytes(runtime_config) -> bytes | None:
    """``almalinux-gpg-keys`` of the version-under-test, or None on AL9
    where the keys still live inside ``almalinux-release``.
    """
    return _download_target_pkg(runtime_config, "almalinux-gpg-keys")


def pytest_unconfigure(config):
    """Render a one-screen Summary block AFTER pytest's own final line.

    pytest's terminal output ends with two pieces:
      1. the ``-ra`` "short test summary info" section (verbose list of
         skips/fails);
      2. the green/red one-liner ``=== 301 passed, 19 skipped in 36s ===``.

    Both get visually lost when the run has hundreds of test names above
    them. We add an explicit multi-line block AFTER (1) AND (2) so the
    operator's eye lands on it last.

    Why ``pytest_unconfigure`` instead of ``pytest_terminal_summary`` /
    ``pytest_sessionfinish``: TerminalReporter wraps both of those with
    its own ``tryfirst=True hookwrapper``, which makes its finalization
    run *after* anything we register at the same level. ``pytest_unconfigure``
    fires strictly after the whole hook chain, so writing to ``sys.stdout``
    here lands as the very last terminal output.

    Order of output here is important:
      1. release-tests per-file block (only if release tests ran)
      2. global Summary block (always, when any tests ran)
    """
    import sys

    # ---- 1. release-tests block + detailed markdown report ----
    if _RELEASE_OUTCOMES:
        _print_release_summary_to_stdout()
        # Per-run timestamped filename — keeps history for repeated runs and
        # makes it obvious in CI artifacts which run a report belongs to.
        # UTC so reports from different runners/timezones sort consistently.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report_path = (
            Path(str(config.rootdir)) / "reports" / f"release-report-{ts}.md"
        )
        _write_release_report_file(report_path)
        try:
            rel = report_path.relative_to(Path(str(config.rootdir)))
        except ValueError:
            rel = report_path
        sys.stdout.write(f"Detailed release report: {rel}\n")

    # ---- 2. global Summary block ----
    tr = config.pluginmanager.getplugin("terminalreporter")
    if tr is None:
        return  # ``-p no:terminal`` or similar — nothing to summarize.
    stats = tr.stats
    passed = len(stats.get("passed", []))
    failed = len(stats.get("failed", []))
    skipped = len(stats.get("skipped", []))
    errors = len(stats.get("error", []))
    deselected = len(stats.get("deselected", []))
    xfailed = len(stats.get("xfailed", []))
    xpassed = len(stats.get("xpassed", []))
    total = passed + failed + skipped + errors + xfailed + xpassed

    # No tests collected — nothing to summarize. This avoids printing an
    # empty "Summary" block during ``--collect-only`` or when pytest
    # exits early due to a config error.
    if total == 0:
        return

    sep = "=" * 30 + " Summary " + "=" * 30
    end = "=" * len(sep)
    lines = [
        "",  # blank line before our block
        sep,
        f"  Passed:     {passed}",
    ]
    if xpassed:
        lines.append(f"  XPassed:    {xpassed}")
    lines.append(f"  Skipped:    {skipped}")
    if xfailed:
        lines.append(f"  XFailed:    {xfailed}")
    if deselected:
        lines.append(f"  Deselected: {deselected}")
    if failed:
        lines.append(f"  Failed:     {failed}")
    if errors:
        lines.append(f"  Errors:     {errors}")
    lines.append(f"  Total run:  {total}")
    verdict = "OK" if (failed == 0 and errors == 0) else "FAILED"
    lines.append(f"  Verdict:    {verdict}")
    lines.append(end)

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
