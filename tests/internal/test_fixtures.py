"""Meta-tests for fixtures and pytest hooks defined in ``tests/conftest.py``.

These pin the behavior the rest of the suite implicitly relies on:

* ``RuntimeConfig.from_env`` raises a clear ``SystemExit`` (not a silent
  default) when env vars are missing — otherwise the suite would silently
  test the wrong thing.
* ``synthetic_repos_rpm`` actually round-trips through ``rpm_extractor``,
  i.e. the in-memory RPM produced by conftest is parseable by the very
  helper the real tests use. If conftest drifts away from rpmfile's
  expectations, a lot of ``test_almalinux_repos_pkg`` paths break in
  hard-to-debug ways.
* ``pytest_generate_tests`` does not duplicate parametrization for tests
  that already declare ``@pytest.mark.parametrize("arch", ...)``
  themselves. We assert this contract in isolation rather than waiting
  for the collection to fail loudly under a future test.
"""
from __future__ import annotations

import pytest

from post_check.config import RuntimeConfig
from post_check.helpers import rpm_extractor


# ============================================================ RuntimeConfig.from_env


def _clean_env(monkeypatch):
    for var in ("ALMA_SOURCE", "ALMA_VERSION", "ALMA_ARCHES", "ALMA_REPOS"):
        monkeypatch.delenv(var, raising=False)


def test_runtime_config_requires_alma_source(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ALMA_VERSION", "10.1")
    with pytest.raises(SystemExit) as ei:
        RuntimeConfig.from_env()
    assert "ALMA_SOURCE" in str(ei.value)


def test_runtime_config_requires_alma_version(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ALMA_SOURCE", "stable")
    with pytest.raises(SystemExit) as ei:
        RuntimeConfig.from_env()
    assert "ALMA_VERSION" in str(ei.value)


def test_runtime_config_rejects_unsupported_major(monkeypatch):
    """AlmaLinux 8 is explicitly out of scope. If somebody points the
    suite at major=8 by accident, we want a hard error, not a silent
    run that exercises 9/10-only code paths.
    """
    _clean_env(monkeypatch)
    monkeypatch.setenv("ALMA_SOURCE", "stable")
    monkeypatch.setenv("ALMA_VERSION", "8.10")
    with pytest.raises(SystemExit) as ei:
        RuntimeConfig.from_env()
    assert "8" in str(ei.value)


def test_runtime_config_rejects_unknown_source(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ALMA_SOURCE", "definitely-not-a-source")
    monkeypatch.setenv("ALMA_VERSION", "10.1")
    with pytest.raises(SystemExit) as ei:
        RuntimeConfig.from_env()
    assert "definitely-not-a-source" in str(ei.value)


def test_runtime_config_arches_default_is_x86_64(monkeypatch):
    """If ALMA_ARCHES is not set, default must be ``("x86_64",)``.

    Pinned because dropping the default would silently turn ``arch``
    parametrization into an empty list and every parametrized test would
    just collect zero items (and pass without actually running).
    """
    _clean_env(monkeypatch)
    monkeypatch.setenv("ALMA_SOURCE", "stable")
    monkeypatch.setenv("ALMA_VERSION", "10.1")
    rc = RuntimeConfig.from_env()
    assert rc.arches == ("x86_64",)


def test_runtime_config_arches_strips_whitespace(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ALMA_SOURCE", "stable")
    monkeypatch.setenv("ALMA_VERSION", "10.1")
    monkeypatch.setenv("ALMA_ARCHES", " x86_64 , aarch64 ")
    rc = RuntimeConfig.from_env()
    assert rc.arches == ("x86_64", "aarch64")


# ============================================================ synthetic_repos_rpm


def test_synthetic_repos_rpm_round_trips_through_rpm_extractor(synthetic_repos_rpm):
    """The conftest builds a hand-crafted RPM in pure Python. If the
    layout drifts away from rpmfile's expectations, we want to find out
    here — not in ``test_almalinux_repos_pkg`` where the failure would
    look like "no .repo files found" and confuse the operator.
    """
    files = rpm_extractor.list_files(synthetic_repos_rpm)
    assert "./etc/yum.repos.d/almalinux.repo" in files

    body = rpm_extractor.read_file(synthetic_repos_rpm, "./etc/yum.repos.d/almalinux.repo")
    text = body.decode()
    assert "[almalinux-baseos]" in text
    assert "gpgcheck=1" in text


def test_synthetic_repos_rpm_starts_with_rpm_lead_magic(synthetic_repos_rpm):
    """First 4 bytes must be the RPM lead magic ``\\xed\\xab\\xee\\xdb``.

    If the conftest builder breaks this header, rpmfile fails with an
    obscure error far from the root cause. Pin the magic explicitly.
    """
    assert synthetic_repos_rpm[:4] == b"\xed\xab\xee\xdb"


# ============================================================ pytest_generate_tests


def test_already_parametrized_returns_true_for_self_parametrized_arch():
    """The private ``_already_parametrized`` in conftest is the guard
    against doubled parametrization. We test it directly with a mock
    metafunc rather than spinning up a sub-pytest, because the real
    failure mode (``duplicate parametrization``) would surface at
    collection of the *real* suite — too late to be useful.
    """
    from unittest.mock import MagicMock

    from tests.conftest import _already_parametrized

    metafunc = MagicMock()
    marker = MagicMock()
    marker.args = ("arch",)
    metafunc.definition.iter_markers.return_value = [marker]
    assert _already_parametrized(metafunc, "arch") is True


def test_already_parametrized_returns_false_when_no_self_parametrize():
    from unittest.mock import MagicMock

    from tests.conftest import _already_parametrized

    metafunc = MagicMock()
    metafunc.definition.iter_markers.return_value = []
    assert _already_parametrized(metafunc, "arch") is False


def test_already_parametrized_returns_true_for_combined_argnames():
    """``@pytest.mark.parametrize("arch,repo", [...])`` declares both
    names in one comma-separated string. ``_already_parametrized`` must
    handle that — otherwise conftest would happily double-parametrize
    ``arch`` of a test that combines both.
    """
    from unittest.mock import MagicMock

    from tests.conftest import _already_parametrized

    metafunc = MagicMock()
    marker = MagicMock()
    marker.args = ("arch,repo",)
    metafunc.definition.iter_markers.return_value = [marker]
    assert _already_parametrized(metafunc, "arch") is True
    assert _already_parametrized(metafunc, "repo") is True
    assert _already_parametrized(metafunc, "version") is False


# ============================================================ _extract_failure_message
#
# This helper drives what an operator sees in the markdown release
# report. Without these pins, a future pytest version that tweaks its
# longrepr format could silently re-introduce the noisy "full test
# source + arg reprs + traceback" output the helper exists to strip.


def test_extract_failure_message_strips_pytest_source_and_arg_reprs():
    """Real-shape input — fixture arg reprs + def block + ``E`` lines.
    Output must be ONLY the assertion message, no source, no fixtures.
    """
    from tests.conftest import _extract_failure_message

    longrepr = """\
runtime_config = RuntimeConfig(source='stable', version='10.1', arches=('x86_64',), repos=('BaseOS',))

    def test_noarch_packages_identical_across_arches_across_all_repos(runtime_config):
        \"\"\"Both noarch parity invariants — see module docstring.\"\"\"
        all_repos = list(load_sources()[runtime_config.source]["repos"])
        ...
>       assert not blocks, "\\n\\n".join(blocks)
E       AssertionError: [B] noarch parity: 1 of 2978 (repo, name) pair(s) differ in 1 group(s).
E
E           AppStream/mingw-qemu-ga-win — version skew
E               [aarch64, ppc64le]: 108.0.1-1.el10
E               [x86_64, x86_64_v2]: 110.0.2-1.el10
E       assert not ['[B] noarch parity: ... long expression repr ...']

tests/release/test_noarch_parity.py:372: AssertionError
"""
    out = _extract_failure_message(longrepr)
    # The deliberate message lines survive.
    assert "[B] noarch parity: 1 of 2978" in out
    assert "AppStream/mingw-qemu-ga-win — version skew" in out
    assert "[aarch64, ppc64le]: 108.0.1-1.el10" in out
    assert "[x86_64, x86_64_v2]: 110.0.2-1.el10" in out
    # The noise is gone.
    assert "runtime_config" not in out
    assert "def test_" not in out
    assert "AssertionError:" not in out  # class prefix stripped
    assert "tests/release/test_noarch_parity.py:372" not in out  # file footer
    # The auto-appended ``assert not [...]`` repr line is dropped —
    # otherwise the operator sees the same content twice (once as the
    # message, once as the expression repr).
    assert "long expression repr" not in out


def test_extract_failure_message_handles_pytest_fail_message():
    """``pytest.fail("...")`` produces a slightly different longrepr
    where the prefix on the first ``E`` line is ``Failed:`` instead of
    ``AssertionError:``. Both should be stripped.
    """
    from tests.conftest import _extract_failure_message

    longrepr = """\
    def test_thing():
>       pytest.fail("BaseOS not published for stable/10.3 on any arch")
E       Failed: BaseOS not published for stable/10.3 on any arch

tests/foo.py:42: Failed
"""
    out = _extract_failure_message(longrepr)
    assert out == "BaseOS not published for stable/10.3 on any arch"


def test_extract_failure_message_preserves_blank_lines_in_message():
    """Multi-line assertion messages with blank separators (a common
    pattern in our test outputs) should keep their structure — pytest
    encodes blanks as ``E`` (or ``E ``) on a line by themselves.
    """
    from tests.conftest import _extract_failure_message

    longrepr = """\
>       assert False, "first paragraph\\n\\nsecond paragraph"
E       AssertionError: first paragraph
E
E       second paragraph
E       assert False
"""
    out = _extract_failure_message(longrepr)
    assert out == "first paragraph\n\nsecond paragraph"


def test_extract_failure_message_returns_empty_for_empty_input():
    """Defensive: a missing/empty longrepr (rare but possible during
    teardown failures) must not crash the report renderer.
    """
    from tests.conftest import _extract_failure_message

    assert _extract_failure_message("") == ""
    assert _extract_failure_message(None) == ""
