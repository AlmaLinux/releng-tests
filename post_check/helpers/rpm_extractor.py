"""Read-only RPM payload helpers.

Wrappers over `rpmfile` so callers can list and read members of an RPM
without shelling out to `rpm2cpio`/`cpio` (which are not present on
default GitHub `ubuntu-latest` runners and would force an extra apt
install in CI).
"""
from __future__ import annotations

import io

import rpmfile


def list_files(rpm_bytes: bytes) -> list[str]:
    """Return CPIO member names contained in the given RPM payload.

    Names are returned exactly as recorded inside the archive — typically
    prefixed with ``./`` (e.g. ``./etc/yum.repos.d/almalinux.repo``).
    """
    with rpmfile.open(fileobj=io.BytesIO(rpm_bytes)) as rpm:
        return [m.name for m in rpm.getmembers()]


def read_file(rpm_bytes: bytes, path: str) -> bytes:
    """Return the raw bytes of one member inside the RPM payload.

    `path` must match a name as returned by :func:`list_files`.
    Raises ``KeyError`` (propagated from rpmfile) if the member is
    missing.
    """
    with rpmfile.open(fileobj=io.BytesIO(rpm_bytes)) as rpm:
        member = rpm.getmember(path)
        return rpm.extractfile(member).read()
