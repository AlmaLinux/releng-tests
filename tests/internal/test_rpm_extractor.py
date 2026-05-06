from post_check.helpers.rpm_extractor import list_files, read_file


def test_rpm_extractor_lists_repo_files_in_etc_yum_repos_d(synthetic_repos_rpm):
    files = list_files(synthetic_repos_rpm)
    assert any(
        f.startswith("./etc/yum.repos.d/") and f.endswith(".repo") for f in files
    )


def test_rpm_extractor_read_file_returns_repo_contents(synthetic_repos_rpm):
    files = list_files(synthetic_repos_rpm)
    repo_path = next(
        f for f in files if f.startswith("./etc/yum.repos.d/") and f.endswith(".repo")
    )
    content = read_file(synthetic_repos_rpm, repo_path)
    assert isinstance(content, bytes)
    text = content.decode()
    assert "[almalinux-baseos]" in text
    assert "baseurl=" in text
