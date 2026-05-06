from post_check.helpers.dnf_log import load_allowlist, parse_skipped

SAMPLE = """\
Some output above
Skipped packages were:
  foo-debug-1.0
  bar-debuginfo-2.5
Other text below
"""


def test_install_all_parser_extracts_failed_pkg_names_from_dnf_log():
    assert parse_skipped(SAMPLE) == {"foo-debug-1.0", "bar-debuginfo-2.5"}


def test_install_all_allowlist_loaded_per_version_from_yaml():
    al = load_allowlist("10")
    # The initial allowlist is empty
    assert al == set()


def test_install_all_allowlist_returns_empty_set_for_unknown_major():
    assert load_allowlist("99") == set()
