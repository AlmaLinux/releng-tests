"""Suite-level structural contracts.

Why these exist
---------------
A test that imports incorrectly, has no asserts, or is mistakenly
not picked up by collection will pass silently — it just contributes
zero signal. These checks are static (AST-based) and catch the
"never-fails-because-it-never-runs" failure mode.

* every ``tests/**/test_*.py`` is importable;
* every ``def test_...`` contains at least one ``assert`` statement
  OR raises/with-pytest.raises (we treat ``with pytest.raises(...)``
  and direct ``raise`` as equivalent to an assertion);
* every test marked ``online`` actually uses ``http.session()`` (or a
  helper that does) — the marker would otherwise lie;
* every test marked ``requires_docker`` uses
  ``post_check.helpers.docker``.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_DIR.parent


# ---------------------------------------------------------------- helpers


def _iter_test_files() -> list[Path]:
    return list(TESTS_DIR.rglob("test_*.py"))


def _module_name(path: Path) -> str:
    """File path → import path, e.g. ``tests/release/test_foo.py`` → ``tests.release.test_foo``."""
    rel = path.relative_to(REPO_ROOT)
    return ".".join(rel.with_suffix("").parts)


def _test_function_nodes(tree: ast.AST) -> list[ast.FunctionDef]:
    out: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            out.append(node)
    return out


def _has_assertion_like(node: ast.AST) -> bool:
    """True if the function body contains anything that can fail a test:

    * ``assert ...``
    * ``raise ...``
    * ``with pytest.raises(...): ...``
    * ``pytest.fail(...)``
    * ``pytester.assert_outcomes(...)`` and similar attribute calls
      ending in ``assert_*``.
    """
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Assert, ast.Raise)):
            return True
        if isinstance(sub, ast.With):
            for item in sub.items:
                if _is_pytest_raises(item.context_expr):
                    return True
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute):
                if f.attr in {"fail", "xfail"} and _is_pytest_attr(f.value):
                    return True
                if f.attr.startswith("assert_"):
                    return True
            if isinstance(f, ast.Name) and f.id == "assert_":
                return True
    return False


def _is_pytest_raises(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Call):
        f = expr.func
        if isinstance(f, ast.Attribute) and f.attr == "raises":
            return _is_pytest_attr(f.value)
    return False


def _is_pytest_attr(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Name) and expr.id == "pytest"


# ---------------------------------------------------------------- tests


def test_every_test_file_is_importable():
    """ImportError in a test file makes pytest report a collection error
    (red), which is good — *unless* CI is configured to ignore collection
    errors, in which case the file disappears from the run silently. We
    assert importability explicitly so a structural break is loud
    regardless of pytest config.
    """
    failures: list[str] = []
    # The real test suite imports require ALMA_SOURCE/ALMA_VERSION to be
    # set (RuntimeConfig.from_env is called at import time of some
    # fixtures). The conftest fixture itself isn't loaded just by
    # importing the test module, but a few modules touch config at
    # top-level. Set sane defaults so the import test isn't measuring
    # env-var presence.
    import os

    os.environ.setdefault("ALMA_SOURCE", "stable")
    os.environ.setdefault("ALMA_VERSION", "10.1")
    for fpath in _iter_test_files():
        modname = _module_name(fpath)
        try:
            importlib.import_module(modname)
        except Exception as e:
            failures.append(f"{modname}: {type(e).__name__}: {e}")
    assert not failures, "Test modules failed to import:\n" + "\n".join(failures)


def test_every_test_function_has_at_least_one_assertion():
    """A ``def test_foo(): pass`` always passes and contributes no
    signal. Pin that every test function has *some* failure mechanism.
    """
    offenders: list[str] = []
    for fpath in _iter_test_files():
        tree = ast.parse(fpath.read_text())
        for fn in _test_function_nodes(tree):
            if not _has_assertion_like(fn):
                offenders.append(f"{fpath.relative_to(REPO_ROOT)}::{fn.name} (line {fn.lineno})")
    # Known exceptions:
    # * ``test_release_parity_handles_kitten_naming`` — intentionally a no-op
    #   pass, locks in the test name rather than asserting anything.
    # * ``test_workflow_yaml_is_valid_yaml`` — the assertion *is* the absence
    #   of an exception from ``yaml.load``: if the workflow YAML is invalid,
    #   ``_load()`` raises and the test fails. Idiomatic, but not visible
    #   to a static scanner.
    allow = {
        "tests/release/test_release_parity.py::test_release_parity_handles_kitten_naming",
        "tests/internal/test_workflow.py::test_workflow_yaml_is_valid_yaml",
    }
    real = [o for o in offenders if o.split(" (")[0] not in allow]
    assert not real, "Tests without any assertion / raise / with pytest.raises:\n" + "\n".join(real)


def test_no_test_file_is_assertion_less_overall():
    """Belt-and-suspenders: every ``test_*.py`` must contain at least one
    assertion-like construct *somewhere*. This catches the case where a
    file has lots of ``def test_...`` that all delegate to a private
    helper which is itself broken.
    """
    barren: list[str] = []
    for fpath in _iter_test_files():
        tree = ast.parse(fpath.read_text())
        if not _has_assertion_like(tree):
            barren.append(str(fpath.relative_to(REPO_ROOT)))
    assert not barren, "Test files without any assertion:\n" + "\n".join(barren)


def test_online_marked_tests_actually_reference_http_or_helpers():
    """If a test wears ``pytestmark = pytest.mark.online`` but never
    touches the network, the marker lies — and worse, the test contributes
    a passing result with no real coverage.

    We approximate "touches the network" with a textual check: the file
    imports ``http``, ``requests``, or one of our helpers that wrap
    HTTP (``url_builder``, ``repodata``, ``iso``, ``gpg``).
    """
    network_ish = (
        "from post_check.helpers import http",
        "from post_check.helpers.http",
        "import requests",
        "from post_check.helpers import repodata",
        "from post_check.helpers import iso",
        "from post_check.helpers import repo_inject",
        "from post_check.helpers import gpg",
        "post_check.helpers.gpg",  # ``import post_check.helpers.gpg as gpg_module``
        "from post_check.helpers.gpg",
        "from post_check.helpers.url_builder",
    )
    offenders: list[str] = []
    for fpath in _iter_test_files():
        text = fpath.read_text()
        if "pytest.mark.online" not in text:
            continue
        if not any(needle in text for needle in network_ish):
            offenders.append(str(fpath.relative_to(REPO_ROOT)))
    assert not offenders, (
        "Tests marked 'online' but with no apparent network dependency "
        "(possibly mislabeled):\n" + "\n".join(offenders)
    )


def test_requires_docker_marked_tests_actually_use_docker_helper():
    """Same idea as the online marker, but for ``requires_docker``.

    We look for the marker in its decorated form (``pytest.mark.requires_docker``)
    rather than any occurrence of the literal string — otherwise a meta-test
    that passes ``-m "not requires_docker"`` to a stub pytest as a STRING
    argument (e.g. ``test_run_local_sh.py``) is flagged as misusing the marker
    when in fact it never marks anything.
    """
    offenders: list[str] = []
    for fpath in _iter_test_files():
        text = fpath.read_text()
        if "pytest.mark.requires_docker" not in text:
            continue
        if "post_check.helpers.docker" not in text and "post_check.helpers import docker" not in text:
            offenders.append(str(fpath.relative_to(REPO_ROOT)))
    assert not offenders, (
        "Tests marked 'requires_docker' but with no docker helper import:\n"
        + "\n".join(offenders)
    )


def test_pytest_ini_lists_all_used_markers():
    """If a marker is used in a test but missing from ``pytest.ini``
    ``markers = ...``, pytest with ``--strict-markers`` (we use it) errors
    at collection. But the error wording isn't always loud — pin it
    statically too so reviewers see it on the diff.
    """
    pytest_ini = (REPO_ROOT / "pytest.ini").read_text()
    declared = set()
    for line in pytest_ini.splitlines():
        line = line.strip()
        # marker entries are of the form "name: description"
        if ":" in line and not line.startswith("#") and not line.startswith("["):
            head = line.split(":", 1)[0].strip()
            if head and " " not in head and "=" not in head:
                declared.add(head)

    used: set[str] = set()
    for fpath in _iter_test_files():
        tree = ast.parse(fpath.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
                # pytest.mark.<name>
                inner = node.value
                if (
                    isinstance(inner.value, ast.Name)
                    and inner.value.id == "pytest"
                    and inner.attr == "mark"
                ):
                    used.add(node.attr)

    # ``parametrize`` and ``skipif`` are pytest built-ins, not custom
    # markers — they don't need to be declared.
    builtin = {"parametrize", "skipif", "skip", "xfail", "usefixtures", "filterwarnings"}
    missing = (used - builtin) - declared
    assert not missing, (
        f"Markers used in tests but NOT declared in pytest.ini: {sorted(missing)}"
    )
