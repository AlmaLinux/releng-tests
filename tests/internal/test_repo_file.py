from post_check.helpers.repo_file import parse, substitute


def test_repo_file_parser_returns_baseurl_gpgkey_and_enabled():
    text = """
[almalinux-baseos]
name=AlmaLinux $releasever - BaseOS
baseurl=https://repo.almalinux.org/almalinux/$releasever/BaseOS/$basearch/os/
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10
gpgcheck=1
enabled=1
"""
    sections = parse(text)
    assert len(sections) == 1
    s = sections[0]
    assert s.section_id == "almalinux-baseos"
    assert s.name == "AlmaLinux $releasever - BaseOS"
    assert s.baseurl == "https://repo.almalinux.org/almalinux/$releasever/BaseOS/$basearch/os/"
    assert s.gpgkey == "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10"
    assert s.gpgcheck is True
    assert s.enabled is True


def test_repo_file_parser_defaults_when_keys_missing():
    text = """
[minimal]
baseurl=https://example.test/
"""
    sections = parse(text)
    assert len(sections) == 1
    s = sections[0]
    # name defaults to section_id, gpgcheck defaults to False, enabled to True, sslverify to True
    assert s.name == "minimal"
    assert s.gpgcheck is False
    assert s.enabled is True
    assert s.sslverify is True
    assert s.gpgkey is None
    assert s.metalink is None
    assert s.mirrorlist is None


def test_repo_file_parser_handles_multiple_sections():
    text = """
[a]
baseurl=https://a.test/

[b]
baseurl=https://b.test/
enabled=0
"""
    sections = parse(text)
    assert [s.section_id for s in sections] == ["a", "b"]
    assert sections[1].enabled is False


def test_baseurl_substitutes_basearch_releasever_dollar_signs():
    out = substitute("https://x/$releasever/$basearch/os", basearch="aarch64", releasever="10")
    assert out == "https://x/10/aarch64/os"
