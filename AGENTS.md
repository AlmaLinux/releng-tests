<!-- BEGIN project AGENTS.md (from .flockctl/AGENTS.md) -->
# post-check — agent guide

Post-release validation suite for AlmaLinux. Two unrelated test
categories live side by side; **always** know which one you're touching
before you change anything.

## Mental model

| Folder | What it validates | Env vars | Network | Docker |
|---|---|---|---|---|
| `tests/release/` | The AlmaLinux release itself (the product). One file per contract item — see `docs/TESTS.md`. | `ALMA_*` required | yes | sometimes |
| `tests/internal/` | post-check itself (the tool) — helpers, parsers, suite-level contracts. | none | no | no |

If a change touches both categories, that's a smell — split the PR.
Internal tests must stay fully offline; never add an `ALMA_*` dependency
or a network call there.

## Stack & tooling

- **Python 3.12+** (3.14 also works). Use `from __future__ import annotations`
  in new modules; the existing code does.
- **ruff** + **mypy** via `make lint`. Line length 100, target py312.
  Run lint before declaring anything done.
- **pytest** with `--strict-markers`. Markers are defined in `pytest.ini`:
  `requires_docker`, `slow`, `online`, `release`, `internal`. Don't invent
  new ones without updating that file — strict mode will reject them.
- Library code lives under `post_check/`. New helper? It goes in
  `post_check/helpers/<name>.py`, gets a docstring saying *why* it
  exists, and is imported (not duplicated) from tests.

## Configuration discipline

`post_check/config.py` draws a hard line:

- **Facts about the AlmaLinux release process** (source registry, repo
  list, expected `.repo` section IDs) are hardcoded constants in
  `config.py`. They never vary per-deployment.
- **Things that vary at runtime** (the arch matrix, GPG fingerprints)
  live in YAML under `config/`.

When adding a new knob, decide which category it falls into *first*.
Don't put release facts in YAML "for flexibility" — that's ceremony
with zero benefit, and the existing module docstring explicitly calls
this out.

## Tests — what good looks like

- Every `def test_*` must have an `assert`, a `pytest.raises`, or a
  raise. `tests/internal/test_coverage_contract.py` enforces this with
  AST checks; assertion-free tests pass silently and are worse than
  no tests at all.
- Markers must not lie. If you mark a test `online`, it must actually
  use `post_check.helpers.http.session()`. If you mark it
  `requires_docker`, it must use `post_check.helpers.docker`. The
  coverage contract verifies this.
- **Do not commit binary fixtures** under `tests/data/`. Synthetic RPMs,
  repodata, signatures, etc. are built in pure Python at session scope
  (`tests/conftest.py`, `tests/conftest_data.py`). Adding `rpmbuild` /
  `rpm2cpio` as a CI dependency is explicitly off-limits.
- Release tests carry `pytest.mark.release` so the markdown reporter
  picks them up. The reporter reads test docstrings — write a useful
  one-liner that says *what property of the release this asserts*, not
  what the code does. Operators read those, not the source.
- Use the `report_detail` fixture to surface concrete URLs / NEVRs /
  ISO names in the report. "passed" alone is not auditable.

## Skip behaviour

`config/architectures.yaml` (loaded via `arch_supports_major` /
`arch_skip_categories`) is the single source of truth for which arches
ship in which major. When a test is skipped, the reason string must
mention either the arch+major combo or the missing dependency
(e.g. `gpg` not installed) — the report quotes it verbatim.

`dnf upgrade` from `x.0` is auto-skipped because there's no previous
minor. Don't paper over this with a hardcoded fallback; the skip is
correct.

## Comments & commits

- Comments explain **why**, not what. The codebase already follows this
  rigorously — match the tone. If a constant has a non-obvious
  motivation (e.g. "AL10 dropped ResilientStorage"), say so inline.
- Module docstrings are mandatory for new files in `post_check/` and
  for non-trivial test files. They should answer "why does this file
  exist" in two or three sentences.
- Commit messages: imperative mood, focus on the *why*. The release
  report and CI artifacts are the user-facing surface — call out any
  change to their shape.

## Targets to know

- `make test-internal` — run before every commit. Offline, fast.
- `make test-release-host ALMA_SOURCE=stable ALMA_VERSION=10.1` — fast
  release iteration without Docker.
- `make test-release ALMA_SOURCE=… ALMA_VERSION=…` — produces the GA
  artifact under `reports/`. Run this before claiming a release-side
  change works end to end.
- `make lint` — required to be green before merge.

`make` with no target prints the help; reach for that instead of
guessing target names.

## Reports

`reports/` is gitignored. The terminal output during a release run is
intentionally minimal (per-file OK/FAIL + final tally). Verbose pytest
output goes into the markdown report. Don't "fix" the quiet stdout by
adding prints — that's a regression of an explicit UX choice.
<!-- END project AGENTS.md -->
