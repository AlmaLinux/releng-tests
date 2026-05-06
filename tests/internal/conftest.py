"""Auto-apply the ``internal`` marker to every test in this folder.

``tests/internal/`` holds tests of *post-check itself* — helper
parsers, the docker wrapper, URL builders, repo-file injection — i.e.
the tooling we use to validate AlmaLinux, NOT AlmaLinux itself.

The directory IS the source of truth for the category: if a file lives
under ``tests/internal/``, every test in it is an internal-tooling test
by construction. This avoids the failure mode where someone adds a
helper test, forgets ``pytestmark = [pytest.mark.internal]``, and the
test silently leaks into the ``-m release`` selection (or vice versa).

Compare with ``tests/release/``: that folder does *not* auto-apply
``release``. Release markers there are placed per-file/per-test on
purpose so we can deliberately omit a single test
(``test_run_in_arch_executes_uname_m`` — a QEMU/binfmt sanity smoke
that is not AlmaLinux content and must not appear in the GA report).
"""
from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    internal_marker = pytest.mark.internal
    here = str(config.rootpath / "tests" / "internal")
    for item in items:
        if str(item.path).startswith(here):
            item.add_marker(internal_marker)
