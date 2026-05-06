"""Generator of synthetic ``primary.xml.gz`` / ``primary.xml.zst`` for
offline parser tests.

Deliberately NOT ``conftest.py`` — these are not fixtures but a helper
function that tests import explicitly. Each test generates its own
fixture into ``tmp_path`` at the size it needs (a 1-package smoke vs.
a 200k-package RSS-bound test), so there's no checked-in artifact to
keep in sync.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Literal

import zstandard

# Sha256 hash of length 64. The specific bytes don't matter — we only
# check that the parser propagates the string correctly. We use different
# hashes per package, otherwise tests for "checksum is unique" would be
# trivial.
_HEX = "0123456789abcdef"


def _fake_sha256(i: int) -> str:
    s = f"{i:016x}"
    # 64 hex characters: 16 from the index + 48 padding
    return s + ("0" * (64 - len(s)))


def _build_primary_xml_bytes(
    n_packages: int, arches: tuple[str, ...]
) -> bytes:
    """Build the body of ``primary.xml`` (uncompressed) — shared part for gz/zst."""
    chunks: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        f'<metadata xmlns="http://linux.duke.edu/metadata/common" packages="{n_packages}">\n',
    ]
    for i in range(n_packages):
        arch = arches[i % len(arches)]
        chunks.append(
            f'<package type="rpm">'
            f'<name>pkg-{i}</name>'
            f'<arch>{arch}</arch>'
            f'<version epoch="0" ver="1.0" rel="1.el10"/>'
            f'<checksum type="sha256" pkgid="YES">{_fake_sha256(i)}</checksum>'
            f'<location href="Packages/p/pkg-{i}-1.0-1.el10.{arch}.rpm"/>'
            f'</package>\n'
        )
    chunks.append("</metadata>\n")
    return "".join(chunks).encode("utf-8")


def make_synthetic_primary(
    path: str | Path,
    n_packages: int = 10_000,
    *,
    arches: tuple[str, ...] = ("x86_64", "aarch64", "noarch"),
    compression: Literal["gz", "zst"] = "gz",
) -> Path:
    """Write a compressed ``primary.xml`` to ``path``.

    ``compression`` — ``"gz"`` (default) or ``"zst"``. AlmaLinux 10.1
    already ships some repos with ``primary.xml.zst``, so it's useful
    for tests to be able to synthesize both variants.

    Architecture layout is round-robin (``i % len(arches)``), not random:
    tests get deterministic expectations (n // len). For 10k packages
    the file is ~250 KB, for 200k — ~5 MB gzipped (~50 MB uncompressed),
    which fits within the "<100 MB peak RSS" requirement.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = _build_primary_xml_bytes(n_packages, arches)
    if compression == "gz":
        p.write_bytes(gzip.compress(raw))
    elif compression == "zst":
        p.write_bytes(zstandard.ZstdCompressor().compress(raw))
    else:  # pragma: no cover — guarded by Literal
        raise ValueError(f"unsupported compression: {compression!r}")
    return p


_REPOMD_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo"
        xmlns:rpm="http://linux.duke.edu/metadata/rpm">
  <revision>1700000000</revision>
  <data type="primary">
    <checksum type="sha256">{checksum}</checksum>
    <open-checksum type="sha256">{checksum}</open-checksum>
    <location href="repodata/{primary_filename}"/>
    <timestamp>1700000000</timestamp>
    <size>123</size>
    <open-size>456</open-size>
  </data>
  <data type="filelists">
    <checksum type="sha256">{checksum}</checksum>
    <location href="repodata/filelists.xml.gz"/>
    <timestamp>1700000000</timestamp>
  </data>
  <data type="other">
    <checksum type="sha256">{checksum}</checksum>
    <location href="repodata/other.xml.gz"/>
    <timestamp>1700000000</timestamp>
  </data>
</repomd>
"""


def make_synthetic_repomd(primary_filename: str = "primary.xml.gz") -> str:
    """Return a valid ``repomd.xml`` (as a string) pointing at
    ``repodata/<primary_filename>``. Used by the
    ``find_primary_xml_url`` test."""
    return _REPOMD_TEMPLATE.format(
        checksum="0" * 64,
        primary_filename=primary_filename,
    )
