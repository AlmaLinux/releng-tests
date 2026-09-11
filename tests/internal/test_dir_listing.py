"""Unit tests for ``post_check.helpers.dir_listing``.

Pure parser checks — no network. The fixtures below are trimmed copies
of what ``repo.almalinux.org`` (nginx autoindex) and an Apache
``fancyindex`` mirror actually serve, because the whole point of the
helper is that ``test_iso_presence`` can treat the directory index as
the source of truth for "is the ISO in place".
"""

from __future__ import annotations

from post_check.helpers.dir_listing import files, parse_hrefs

# Verbatim shape of https://repo.almalinux.org/almalinux/10.3-beta/isos/aarch64/
# (name / date / size columns; the parent link is the first entry).
NGINX_INDEX = """<html>
<head><title>Index of /almalinux/10.3-beta/isos/aarch64/</title></head>
<body>
<h1>Index of /almalinux/10.3-beta/isos/aarch64/</h1><hr><pre><a href="../">../</a>
<a href="AlmaLinux-10-latest-beta-aarch64-boot.iso">AlmaLinux-10-latest-beta-aarch64-boot.iso</a>   11-Sep-2026 07:03   1030670336
<a href="AlmaLinux-10.3-beta-1-aarch64-boot.iso">AlmaLinux-10.3-beta-1-aarch64-boot.iso</a>      11-Sep-2026 07:03   1030670336
<a href="AlmaLinux-10.3-beta-1-aarch64-boot.iso.manifest">AlmaLinux-10.3-beta-1-aarch64-boot.iso.manifest</a> 11-Sep-2026 07:03   285
<a href="CHECKSUM">CHECKSUM</a>                                    11-Sep-2026 07:04   1954
</pre><hr></body>
</html>
"""

# Apache's index adds column-sort query links and an absolute parent
# breadcrumb, and links each entry twice (icon + name).
APACHE_INDEX = """<html><body>
<h1>Index of /almalinux/10.2/isos/x86_64</h1>
<table>
<tr><th><a href="?C=N;O=D">Name</a></th><th><a href="?C=S;O=A">Size</a></th></tr>
<tr><td><a href="/almalinux/10.2/isos/">Parent Directory</a></td></tr>
<tr><td><a href="AlmaLinux-10.2-x86_64-dvd.iso"><img src="/icons/disk.gif"></a>
        <a href="AlmaLinux-10.2-x86_64-dvd.iso">AlmaLinux-10.2-x86_64-dvd.iso</a></td></tr>
<tr><td><a href="x86_64_v2/">x86_64_v2/</a></td></tr>
</table></body></html>
"""


def test_parse_hrefs_returns_entries_in_listing_order():
    names = parse_hrefs(NGINX_INDEX)
    assert names == [
        "AlmaLinux-10-latest-beta-aarch64-boot.iso",
        "AlmaLinux-10.3-beta-1-aarch64-boot.iso",
        "AlmaLinux-10.3-beta-1-aarch64-boot.iso.manifest",
        "CHECKSUM",
    ]


def test_parse_hrefs_drops_navigation_links():
    """Parent links, absolute breadcrumbs and sort links are navigation,
    not directory content. If they leaked through, ``files()`` would
    report phantom entries and a "no ISO published" failure could be
    masked by a link to the parent directory.
    """
    names = parse_hrefs(APACHE_INDEX)
    assert "../" not in names
    assert "/almalinux/10.2/isos/" not in names
    assert not [n for n in names if n.startswith("?")]


def test_parse_hrefs_collapses_duplicate_entries():
    """Apache links the same file twice (icon + name); the entry must
    appear once so callers can count what is published."""
    names = parse_hrefs(APACHE_INDEX)
    assert names.count("AlmaLinux-10.2-x86_64-dvd.iso") == 1


def test_files_excludes_directories():
    assert "x86_64_v2/" not in files(APACHE_INDEX)
    assert files(APACHE_INDEX) == ["AlmaLinux-10.2-x86_64-dvd.iso"]


def test_files_filters_by_suffix_and_excludes_manifests():
    """``.iso.manifest`` must not be counted as an ISO — otherwise a
    directory holding only manifests would read as "media published".
    """
    isos = files(NGINX_INDEX, suffix=".iso")
    assert isos == [
        "AlmaLinux-10-latest-beta-aarch64-boot.iso",
        "AlmaLinux-10.3-beta-1-aarch64-boot.iso",
    ]


def test_files_suffix_match_is_case_insensitive():
    html = '<pre><a href="Foo.ISO">Foo.ISO</a></pre>'
    assert files(html, suffix=".iso") == ["Foo.ISO"]


def test_parse_hrefs_returns_empty_for_non_index_page():
    """An error page served with 200 must yield no entries, so the
    caller's ``assert isos`` fires instead of a confusing later failure.
    """
    assert parse_hrefs("") == []
    assert parse_hrefs("<html><body>404 Not Found</body></html>") == []
