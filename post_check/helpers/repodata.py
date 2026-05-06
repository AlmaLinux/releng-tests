"""Parser for AlmaLinux/yum repodata: ``repomd.xml`` + streaming ``primary.xml.{gz,zst}``.

Used by parity checks (release-parity, noarch-parity) and any
others that need the list of NEVRA + sha256 packages in a repository.

Design:

* ``find_primary_xml_url`` parses the tiny ``repomd.xml`` in full —
  via ``defusedxml.ElementTree.fromstring`` (XXE protection).
* ``iter_packages`` streams ``primary.xml.{gz,zst}``: ``requests`` with
  ``stream=True`` → appropriate decompressor (``gzip.open`` for ``.gz``,
  ``zstandard.ZstdDecompressor.stream_reader`` for ``.zst``) →
  ``defusedxml.iterparse`` with ``elem.clear()`` after each ``<package>``.
  This keeps peak RSS at tens of MB even on a 50MB uncompressed
  file (~200k packages).

  The extension is determined from ``href`` in ``repomd.xml`` (not by
  ``Content-Type`` from the CDN — it often lies). AlmaLinux 10 ships
  ``primary.xml.zst`` for some repos, so without a zstd branch those
  repos cannot be streamed at all.

We deliberately do NOT parse ``filelists.xml`` / ``other.xml`` — our
checks only need NEVRA + checksum + location from ``primary``.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, cast

import requests
import zstandard
from defusedxml.ElementTree import fromstring as parse_xml, iterparse

# yum/dnf repodata namespaces. ``common`` describes <package> inside
# primary.xml; ``repo`` describes <data> inside repomd.xml; ``rpm``
# is where ``<sourcerpm>`` lives (inside <format>).
NS = {
    "common": "http://linux.duke.edu/metadata/common",
    "repo": "http://linux.duke.edu/metadata/repo",
    "rpm": "http://linux.duke.edu/metadata/rpm",
}

_COMMON_PACKAGE_TAG = "{http://linux.duke.edu/metadata/common}package"


@dataclass(frozen=True)
class Package:
    """Minimal description of an RPM package from ``primary.xml``.

    ``checksum`` — hex string (for AlmaLinux always sha256, which we
    separately verify with an online test). ``location`` — relative
    path to ``.rpm`` from the repository root (``Packages/p/foo.rpm``).
    ``sourcerpm`` — basename of the source RPM (``foo-1.0-1.el10.src.rpm``)
    as reported by ``<rpm:sourcerpm>`` inside ``<format>``; empty string
    when missing (which is rare in practice but happens for some repos
    with stripped metadata, so callers must tolerate ``""``).
    """

    name: str
    epoch: str
    version: str
    release: str
    arch: str
    checksum_type: str
    checksum: str
    location: str
    sourcerpm: str = ""

    @property
    def nevra(self) -> str:
        """``name-[epoch:]version-release.arch`` (epoch=0 is dropped)."""
        e = f"{self.epoch}:" if self.epoch and self.epoch != "0" else ""
        return f"{self.name}-{e}{self.version}-{self.release}.{self.arch}"


def find_primary_xml_url(session: requests.Session, repo_base: str) -> str:
    """Find the ``primary.xml.gz`` URL via ``repomd.xml``.

    ``repo_base`` — repository root without a trailing slash,
    e.g. ``https://repo.almalinux.org/almalinux/10/BaseOS/x86_64/os``.

    Returns an absolute URL of the form ``<repo_base>/repodata/primary.xml.gz``
    (the filename is taken from ``<location href="...">`` — it contains
    sha256 in the name and is therefore unstable across snapshots).
    """
    repomd = session.get(repo_base.rstrip("/") + "/repodata/repomd.xml")
    repomd.raise_for_status()
    root = parse_xml(repomd.content)
    for data in root.findall("repo:data", NS):
        if data.attrib.get("type") == "primary":
            location = data.find("repo:location", NS)
            if location is None or "href" not in location.attrib:
                raise ValueError("repomd.xml: primary <data> without <location href=...>")
            return repo_base.rstrip("/") + "/" + location.attrib["href"]
    raise ValueError("primary not found in repomd.xml")


def _open_decompressed(raw: IO[bytes], url: str) -> IO[bytes]:
    """Open a decompressing stream over ``raw`` based on the URL suffix.

    AlmaLinux 10 ships some repos with ``primary.xml.zst`` instead of
    ``primary.xml.gz`` — so we dispatch by the extension from ``href``
    (more reliable than ``Content-Type`` from the CDN).

    ``.xml`` (uncompressed) is also supported — so we don't fail on
    a non-standard repodata layout where primary is uncompressed.
    """
    # ignore the query-string (the CDN sometimes adds ``?ts=...``)
    path = url.split("?", 1)[0].lower()
    if path.endswith(".gz"):
        # ``GzipFile`` is duck-compatible with ``IO[bytes]`` (read/close/
        # context manager) but not a nominal subtype, so mypy needs the cast.
        return cast(IO[bytes], gzip.open(raw, "rb"))
    if path.endswith(".zst"):
        # ``stream_reader`` supports context manager and iterable reading
        # — iterparse only calls ``.read(n)``, which fits the API.
        return cast(IO[bytes], zstandard.ZstdDecompressor().stream_reader(raw))
    if path.endswith(".xml"):
        return raw
    raise ValueError(
        f"unsupported primary.xml compression in URL: {url} "
        f"(expected .gz, .zst or .xml)"
    )


def iter_packages(
    session: requests.Session,
    primary_xml_url: str,
    arch_filter: str | None = None,
) -> Iterator[Package]:
    """Stream ``Package`` from ``primary.xml.{gz,zst,xml}``.

    ``arch_filter`` — if set, only packages of that
    architecture are yielded (``"noarch"``, ``"x86_64"``, …). Exact match.

    All ``<package>`` elements are cleaned up via ``elem.clear()`` after yield —
    this guarantees constant RSS even on ~200k packages. Streaming
    runs straight from the socket: ``stream=True`` disables in-memory
    body buffering, the decompressor works on-the-fly over ``resp.raw``,
    and ``iterparse`` yields ``end`` events as it reads.

    Compression type is determined by the URL suffix — ``.gz`` → gzip,
    ``.zst`` → zstandard, ``.xml`` → no compression.
    """
    # ``Accept-Encoding: identity`` intentionally disables HTTP-level
    # gzip compression. Without this, Varnish in front of repo.almalinux.org
    # responds to a ``primary.xml.gz`` request with ``Content-Encoding: gzip``,
    # even though the body is the ``.gz`` file itself. urllib3 then tries
    # to gunzip the transport layer once more and fails with
    # ``DecodeError: inconsistent stream state`` mid-stream.
    # We ask for raw bytes of the file — we know how to unpack gz/zst ourselves.
    with session.get(
        primary_xml_url, stream=True, headers={"Accept-Encoding": "identity"}
    ) as resp:
        resp.raise_for_status()
        # ``resp.raw`` is typed as ``HTTPResponse | Any`` by requests' stubs;
        # urllib3's HTTPResponse implements the binary file protocol that
        # ``_open_decompressed`` (and the gzip/zstd decoders below it) need.
        with _open_decompressed(cast(IO[bytes], resp.raw), primary_xml_url) as f:
            for _event, elem in iterparse(f, events=("end",)):
                if elem.tag != _COMMON_PACKAGE_TAG:
                    continue
                arch = elem.findtext("common:arch", namespaces=NS) or ""
                if arch_filter is not None and arch != arch_filter:
                    elem.clear()
                    continue
                name = elem.findtext("common:name", namespaces=NS) or ""
                ver_elem = elem.find("common:version", NS)
                checksum_elem = elem.find("common:checksum", NS)
                location_elem = elem.find("common:location", NS)
                if ver_elem is None or checksum_elem is None or location_elem is None:
                    # Broken <package> — skip, don't fail the whole iteration.
                    elem.clear()
                    continue
                # ``<rpm:sourcerpm>`` lives inside ``<format>`` — present in
                # AlmaLinux primary.xml for every binary RPM. We use it to
                # group parity failures by the source package they were
                # built from. Default to "" so a stripped-metadata repo
                # still produces ``Package`` records (groupers tolerate "").
                sourcerpm = (
                    elem.findtext("common:format/rpm:sourcerpm", namespaces=NS)
                    or ""
                ).strip()
                pkg = Package(
                    name=name,
                    epoch=ver_elem.attrib.get("epoch", "0"),
                    version=ver_elem.attrib["ver"],
                    release=ver_elem.attrib["rel"],
                    arch=arch,
                    checksum_type=checksum_elem.attrib["type"],
                    checksum=(checksum_elem.text or "").strip(),
                    location=location_elem.attrib["href"],
                    sourcerpm=sourcerpm,
                )
                elem.clear()
                yield pkg
