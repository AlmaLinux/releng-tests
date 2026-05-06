"""Offline tests for ``post_check.helpers.repodata``.

Coverage:

* ``find_primary_xml_url`` correctly extracts ``href`` from repomd.xml;
* ``iter_packages`` extracts NEVRA + sha256 + location;
* ``arch_filter`` actually filters;
* the parser streams rather than loading everything: on a ~50 MB unzipped
  file (~200,000 packages) peak RSS stays under 100 MB.

The online test (``sha256``-only) lives in ``test_release_parity.py`` —
that's where the ``runtime_config`` fixture already exists.
"""

from __future__ import annotations

import gzip
import os
import resource
from pathlib import Path

import pytest
import requests

from post_check.helpers import repodata
from tests.conftest_data import make_synthetic_primary, make_synthetic_repomd

pytest_httpserver = pytest.importorskip("pytest_httpserver")


def _serve_file(httpserver, url_path: str, body: bytes, content_type: str) -> str:
    """Register a response and return the full URL."""
    httpserver.expect_request(url_path).respond_with_data(
        body, content_type=content_type
    )
    return httpserver.url_for(url_path)


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


# --------------------------------------------------------------------------- #
# find_primary_xml_url
# --------------------------------------------------------------------------- #


def test_repodata_finds_primary_xml_gz_in_repomd(httpserver):
    """``find_primary_xml_url`` must return the href from <data type="primary">."""
    repomd = make_synthetic_repomd(primary_filename="abc123-primary.xml.gz")
    httpserver.expect_request("/BaseOS/x86_64/os/repodata/repomd.xml").respond_with_data(
        repomd, content_type="application/xml"
    )
    repo_base = httpserver.url_for("/BaseOS/x86_64/os")

    s = requests.Session()
    url = repodata.find_primary_xml_url(s, repo_base)
    assert url == repo_base + "/repodata/abc123-primary.xml.gz"


def test_repodata_find_primary_strips_trailing_slash_in_repo_base(httpserver):
    repomd = make_synthetic_repomd(primary_filename="primary.xml.gz")
    httpserver.expect_request("/r/repodata/repomd.xml").respond_with_data(
        repomd, content_type="application/xml"
    )
    s = requests.Session()
    base_with_slash = httpserver.url_for("/r/")
    url = repodata.find_primary_xml_url(s, base_with_slash)
    # No double slash should appear after /r
    assert "//repodata" not in url.replace("http://", "").replace("https://", "")
    assert url.endswith("/r/repodata/primary.xml.gz")


def test_repodata_find_primary_raises_when_no_primary_data(httpserver):
    """If repomd.xml has no <data type="primary"> — raise ValueError, not a silent None."""
    repomd_no_primary = """<?xml version="1.0"?>
    <repomd xmlns="http://linux.duke.edu/metadata/repo">
      <data type="filelists">
        <location href="repodata/filelists.xml.gz"/>
      </data>
    </repomd>"""
    httpserver.expect_request("/r/repodata/repomd.xml").respond_with_data(
        repomd_no_primary, content_type="application/xml"
    )
    s = requests.Session()
    with pytest.raises(ValueError, match="primary"):
        repodata.find_primary_xml_url(s, httpserver.url_for("/r"))


# --------------------------------------------------------------------------- #
# iter_packages
# --------------------------------------------------------------------------- #


def test_primary_xml_gz_parser_extracts_nevra_and_checksum(tmp_path, httpserver):
    p = tmp_path / "primary.xml.gz"
    make_synthetic_primary(p, n_packages=5)
    url = _serve_file(httpserver, "/p.xml.gz", _read_bytes(p), "application/x-gzip")

    s = requests.Session()
    pkgs = list(repodata.iter_packages(s, url))
    assert len(pkgs) == 5

    # round-robin: 0=x86_64, 1=aarch64, 2=noarch, 3=x86_64, 4=aarch64
    expected_archs = ["x86_64", "aarch64", "noarch", "x86_64", "aarch64"]
    for i, pkg in enumerate(pkgs):
        assert pkg.name == f"pkg-{i}"
        assert pkg.epoch == "0"
        assert pkg.version == "1.0"
        assert pkg.release == "1.el10"
        assert pkg.arch == expected_archs[i]
        assert pkg.checksum_type == "sha256"
        # 64 hex characters
        assert len(pkg.checksum) == 64
        assert all(c in "0123456789abcdef" for c in pkg.checksum)
        assert pkg.location == f"Packages/p/pkg-{i}-1.0-1.el10.{expected_archs[i]}.rpm"


def test_primary_xml_gz_parser_nevra_property(tmp_path, httpserver):
    """epoch=0 is omitted from NEVRA."""
    p = tmp_path / "primary.xml.gz"
    make_synthetic_primary(p, n_packages=1)
    url = _serve_file(httpserver, "/p.xml.gz", _read_bytes(p), "application/x-gzip")

    pkg = next(repodata.iter_packages(requests.Session(), url))
    # epoch=0 ⇒ "name-version-release.arch"
    assert pkg.nevra == "pkg-0-1.0-1.el10.x86_64"


def test_primary_xml_gz_parser_nevra_includes_nonzero_epoch(tmp_path, httpserver):
    """epoch=2 ⇒ "name-2:version-release.arch"."""
    raw = (
        '<?xml version="1.0"?>\n'
        '<metadata xmlns="http://linux.duke.edu/metadata/common">\n'
        '<package type="rpm">'
        '<name>foo</name><arch>noarch</arch>'
        '<version epoch="2" ver="3.0" rel="4.el10"/>'
        '<checksum type="sha256">' + ("a" * 64) + '</checksum>'
        '<location href="Packages/f/foo.rpm"/>'
        '</package>\n'
        '</metadata>\n'
    )
    p = tmp_path / "primary.xml.gz"
    with gzip.open(p, "wb") as f:
        f.write(raw.encode())
    url = _serve_file(httpserver, "/p.xml.gz", _read_bytes(p), "application/x-gzip")

    pkg = next(repodata.iter_packages(requests.Session(), url))
    assert pkg.epoch == "2"
    assert pkg.nevra == "foo-2:3.0-4.el10.noarch"


def test_primary_xml_gz_parser_filters_arch_noarch(tmp_path, httpserver):
    p = tmp_path / "primary.xml.gz"
    make_synthetic_primary(p, n_packages=99)  # divisible by 3 → 33 noarch
    url = _serve_file(httpserver, "/p.xml.gz", _read_bytes(p), "application/x-gzip")

    pkgs = list(repodata.iter_packages(requests.Session(), url, arch_filter="noarch"))
    archs = {pkg.arch for pkg in pkgs}
    assert archs == {"noarch"}
    assert len(pkgs) == 33


def test_primary_xml_gz_parser_filters_arch_x86_64(tmp_path, httpserver):
    p = tmp_path / "primary.xml.gz"
    make_synthetic_primary(p, n_packages=99)
    url = _serve_file(httpserver, "/p.xml.gz", _read_bytes(p), "application/x-gzip")

    pkgs = list(repodata.iter_packages(requests.Session(), url, arch_filter="x86_64"))
    assert {pkg.arch for pkg in pkgs} == {"x86_64"}
    assert len(pkgs) == 33


def test_primary_xml_gz_parser_no_filter_returns_all_arches(tmp_path, httpserver):
    p = tmp_path / "primary.xml.gz"
    make_synthetic_primary(p, n_packages=99)
    url = _serve_file(httpserver, "/p.xml.gz", _read_bytes(p), "application/x-gzip")

    pkgs = list(repodata.iter_packages(requests.Session(), url))
    assert len(pkgs) == 99
    assert {pkg.arch for pkg in pkgs} == {"x86_64", "aarch64", "noarch"}


def test_primary_xml_zst_parser_extracts_nevra_and_checksum(tmp_path, httpserver):
    """AlmaLinux 10 ships ``primary.xml.zst`` for some repos — the
    parser must correctly decompress it via zstandard.
    """
    p = tmp_path / "primary.xml.zst"
    make_synthetic_primary(p, n_packages=5, compression="zst")
    url = _serve_file(httpserver, "/p.xml.zst", _read_bytes(p), "application/zstd")

    s = requests.Session()
    pkgs = list(repodata.iter_packages(s, url))
    assert len(pkgs) == 5
    expected_archs = ["x86_64", "aarch64", "noarch", "x86_64", "aarch64"]
    for i, pkg in enumerate(pkgs):
        assert pkg.name == f"pkg-{i}"
        assert pkg.arch == expected_archs[i]
        assert len(pkg.checksum) == 64


def test_primary_xml_parser_dispatches_compression_by_url_suffix(tmp_path, httpserver):
    """The parser must distinguish ``.gz`` and ``.zst`` strictly by URL suffix,
    not by ``Content-Type`` from the CDN (which is often wrong)."""
    gz_path = tmp_path / "primary.xml.gz"
    zst_path = tmp_path / "primary.xml.zst"
    make_synthetic_primary(gz_path, n_packages=3, compression="gz")
    make_synthetic_primary(zst_path, n_packages=3, compression="zst")

    # Intentionally serve both files with the same ``Content-Type`` —
    # the choice of decompressor must depend solely on the suffix.
    gz_url = _serve_file(httpserver, "/a.xml.gz", _read_bytes(gz_path), "application/octet-stream")
    zst_url = _serve_file(httpserver, "/a.xml.zst", _read_bytes(zst_path), "application/octet-stream")

    s = requests.Session()
    assert len(list(repodata.iter_packages(s, gz_url))) == 3
    assert len(list(repodata.iter_packages(s, zst_url))) == 3


def test_primary_xml_parser_raises_on_unknown_compression(tmp_path, httpserver):
    """Unsupported suffix — explicit error, not a silent zero result."""
    p = tmp_path / "primary.xml.xz"
    p.write_bytes(b"\xfd7zXZ\x00")  # fake xz magic, we won't read it
    url = _serve_file(httpserver, "/a.xml.xz", _read_bytes(p), "application/octet-stream")

    with pytest.raises(ValueError, match="unsupported"):
        list(repodata.iter_packages(requests.Session(), url))


def test_primary_xml_gz_parser_skips_malformed_packages(tmp_path, httpserver):
    """A broken <package> without <version> — skip, do not crash."""
    raw = (
        '<?xml version="1.0"?>\n'
        '<metadata xmlns="http://linux.duke.edu/metadata/common">\n'
        # broken: no <version>, <checksum>, <location>
        '<package type="rpm"><name>broken</name><arch>noarch</arch></package>\n'
        # valid
        '<package type="rpm"><name>good</name><arch>noarch</arch>'
        '<version epoch="0" ver="1" rel="1"/>'
        '<checksum type="sha256">' + ("b" * 64) + '</checksum>'
        '<location href="Packages/g/good.rpm"/>'
        '</package>\n'
        '</metadata>\n'
    )
    p = tmp_path / "primary.xml.gz"
    with gzip.open(p, "wb") as f:
        f.write(raw.encode())
    url = _serve_file(httpserver, "/p.xml.gz", _read_bytes(p), "application/x-gzip")

    pkgs = list(repodata.iter_packages(requests.Session(), url))
    assert len(pkgs) == 1
    assert pkgs[0].name == "good"


# --------------------------------------------------------------------------- #
# Streaming / RSS
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_primary_xml_gz_parser_streaming_handles_large_file(tmp_path, httpserver):
    """The parser must stay below 100 MB peak RSS on a 50 MB unzipped file.

    200,000 synthetic packages produce roughly 50 MB after decompression
    (~5 MB gzipped). If the parser were accumulating elements in memory,
    RSS would grow linearly and easily exceed 100 MB.
    """
    p = tmp_path / "primary.xml.gz"
    make_synthetic_primary(p, n_packages=200_000)
    url = _serve_file(httpserver, "/p.xml.gz", _read_bytes(p), "application/x-gzip")

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    count = sum(1 for _ in repodata.iter_packages(requests.Session(), url))
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # macOS reports ru_maxrss in bytes; Linux — in KB.
    is_linux = os.uname().sysname == "Linux"
    delta_mb = (rss_after - rss_before) / (1024 if is_linux else 1024 * 1024)

    assert count == 200_000
    assert delta_mb < 100, f"Parser consumed {delta_mb:.1f} MB — must be < 100 MB"
