"""Online tests for isos/<arch>/CHECKSUM on repo.almalinux.org.

Checks:
* the clearsigned GPG signature of the CHECKSUM file is valid against the AlmaLinux release key;
* CHECKSUM lists all expected ISO kinds (dvd / boot / minimal);
* every ISO listed in CHECKSUM is physically reachable via HEAD;
* a tampered payload is rejected by GPG verification.

The ``runtime_config``, ``gpg_keyring``, ``arch`` fixtures are defined in
``tests/conftest.py`` (M0/Slice0/Task04).
"""

from __future__ import annotations

import pytest

from post_check.helpers import http, iso
from post_check.helpers.url_builder import RepoURL

pytestmark = [pytest.mark.online, pytest.mark.release]


@pytest.fixture(autouse=True)
def _skip_for_pulp(runtime_config):
    """ISO checksums are not part of the pulp contract.

    pulp doesn't publish its own ISOs — the layered internal-beta is
    a yum repo only. ISO validity for the underlying stable major is
    covered by ``ALMA_SOURCE=stable`` runs, so re-checking here would
    add nothing.
    """
    if runtime_config.source == "pulp":
        pytest.skip(
            "pulp: no ISOs are published for this source; ISO contract "
            "for the underlying stable is covered by stable runs"
        )


# The expected flavour set lives in the helper (``iso.ISO_KINDS``) —
# ``test_iso_presence`` asserts against the same list, and two copies
# would eventually disagree about what a release must publish.
EXPECTED_ISO_KINDS = iso.ISO_KINDS


def _fetch_checksum(runtime_config, arch):
    """Download isos/<arch>/CHECKSUM. Returns (session, url_builder, response)."""
    u = RepoURL.from_config(
        source=runtime_config.source,
        version=runtime_config.version,
        arch=arch,
        repo="BaseOS",
    )
    s = http.session()
    r = s.get(u.iso_dir() + "/CHECKSUM")
    return s, u, r


def test_iso_checksum_file_signature_valid(runtime_config, gpg_keyring, arch, report_detail):
    """The clearsigned GPG signature on ``isos/<arch>/CHECKSUM`` is
    valid under the AlmaLinux release key for the current major.

    This is the trust anchor for ISO downloads — if the CHECKSUM
    file's own signature can't be verified, every other ISO claim
    on the mirror becomes unverifiable too.

    Skipped if the file isn't published yet for this
    ``(source, version, arch)`` triple (legitimate during a partial
    publish — not an "expected failure").
    """
    _, u, r = _fetch_checksum(runtime_config, arch)
    checksum_url = u.iso_dir() + "/CHECKSUM"
    report_detail(f"GET {checksum_url} → {r.status_code}")
    if r.status_code != 200:
        # Not xfail: if CHECKSUM is not published for this (source/version/arch),
        # the test is not applicable, not "expected to fail".
        pytest.skip(
            f"isos/{arch}/CHECKSUM not present on "
            f"{runtime_config.source}/{runtime_config.version}"
        )
    ok, payload = gpg_keyring.verify_clearsigned(r.content)
    report_detail(
        f"clearsigned signature: {'valid' if ok else 'INVALID'} "
        f"({len(payload) if ok else 0} B payload)"
    )
    assert ok, f"GPG verify failed for isos/{arch}/CHECKSUM"
    assert payload, "Payload empty after verify"


def test_iso_checksum_file_lists_all_expected_iso_names(
    runtime_config, gpg_keyring, arch, report_detail
):
    """``CHECKSUM`` lists all three expected ISO flavours for the arch:
    ``dvd``, ``boot``, ``minimal``.

    A release that drops one of these flavours is a release-management
    regression we want to catch at the gate, not after users notice
    that the boot ISO is missing.
    """
    _, _, r = _fetch_checksum(runtime_config, arch)
    if r.status_code != 200:
        pytest.skip(f"isos/{arch}/CHECKSUM not published")
    ok, payload = gpg_keyring.verify_clearsigned(r.content)
    assert ok
    pairs = iso.parse_checksum(payload.decode())
    assert pairs, "parse_checksum returned an empty list — has the CHECKSUM format changed?"
    for p in pairs:
        report_detail(f"CHECKSUM lists: `{p.filename}`")
    names = " ".join(p.filename.lower() for p in pairs)
    for kind in EXPECTED_ISO_KINDS:
        assert kind in names, f"CHECKSUM has no ISO of kind {kind}"


def test_iso_files_exist_via_head(runtime_config, gpg_keyring, arch, report_detail):
    """Every ISO listed in the (verified) ``CHECKSUM`` is physically
    reachable: HTTP HEAD returns 200 and a non-empty
    ``Content-Length``.

    Catches the classic "we updated CHECKSUM but forgot to upload one
    of the ISOs" mismatch — users would get a 404 on download with no
    obvious cause.
    """
    s, u, r = _fetch_checksum(runtime_config, arch)
    if r.status_code != 200:
        pytest.skip(f"isos/{arch}/CHECKSUM not published")
    ok, payload = gpg_keyring.verify_clearsigned(r.content)
    assert ok
    pairs = iso.parse_checksum(payload.decode())
    assert pairs, "parse_checksum returned an empty list"
    for p in pairs:
        iso_url = u.iso_dir() + "/" + p.filename
        head = s.head(iso_url, allow_redirects=True, timeout=15)
        size = head.headers.get("Content-Length", "?")
        try:
            size_h = f"{int(size) / (1024 * 1024):.0f} MiB"
        except (ValueError, TypeError):
            size_h = "?"
        report_detail(f"HEAD `{p.filename}` → {head.status_code} ({size_h})")
        assert head.status_code == 200, f"ISO {p.filename} -> {head.status_code}"
        assert head.headers.get("Content-Length"), f"ISO {p.filename} without Content-Length"


def test_iso_checksum_rejects_tampered_payload(runtime_config, gpg_keyring, arch):
    """Negative test: flipping a single byte inside the signed payload
    of ``CHECKSUM`` makes GPG verification fail.

    Locks in that the signature check actually inspects the payload
    (not just the presence of an ``-----BEGIN PGP SIGNATURE-----``
    block) — without this, the positive test above could pass even
    if our verification code silently accepted anything.
    """
    _, _, r = _fetch_checksum(runtime_config, arch)
    if r.status_code != 200:
        pytest.skip("setup")
    tampered = bytearray(r.content)
    # Change a byte in the payload (between Hash: and BEGIN PGP SIGNATURE)
    blank = tampered.find(b"\n\n")
    assert blank >= 0, "CHECKSUM has no blank line after Hash: — format is broken"
    idx = blank + 2
    tampered[idx + 5] ^= 0xFF
    ok, _ = gpg_keyring.verify_clearsigned(bytes(tampered))
    assert not ok, "GPG verify must fail on a modified payload"
