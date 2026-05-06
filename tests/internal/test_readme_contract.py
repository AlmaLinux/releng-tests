from pathlib import Path

README = Path(__file__).parent.parent.parent / "README.md"


def test_readme_has_quickstart_for_each_source():
    text = README.read_text()
    assert "ALMA_SOURCE=stable" in text
    assert "ALMA_SOURCE=beta" in text
    assert "ALMA_SOURCE=pungi" in text


def test_readme_has_env_vars_table():
    text = README.read_text()
    for var in ("ALMA_SOURCE", "ALMA_VERSION", "ALMA_ARCHES"):
        assert var in text, f"README does not mention {var}"


def test_readme_has_screenshot_checklist_mapping():
    text = README.read_text()
    expected = [
        "test_dnf_install_all.py",
        "test_almalinux_repos_pkg.py",
        "test_os_release.py",
        "test_dnf_upgrade.py",
        "test_mirrorlist.py",
        "test_iso_checksums.py",
        "test_repomd_signature.py",
        "test_noarch_parity.py",
        "test_release_parity.py",
    ]
    for fname in expected:
        assert fname in text, f"README does not mention {fname}"
