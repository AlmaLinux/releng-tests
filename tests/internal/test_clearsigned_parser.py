"""Unit tests for post_check.helpers.iso.parse_checksum.

These tests do NOT hit the network and do NOT require GPG — this is a
pure parser check.
"""

from post_check.helpers.iso import parse_checksum


def test_clearsigned_checksum_parser_extracts_pairs():
    payload = "SHA256 (AlmaLinux-10.1-x86_64-dvd.iso) = " + "a" * 64 + "\n"
    pairs = parse_checksum(payload)
    assert len(pairs) == 1
    assert pairs[0].filename == "AlmaLinux-10.1-x86_64-dvd.iso"
    assert pairs[0].sha256 == "a" * 64


def test_clearsigned_checksum_parser_extracts_multiple_pairs():
    payload = (
        f"SHA256 (AlmaLinux-10.1-x86_64-dvd.iso) = {'a' * 64}\n"
        f"SHA256 (AlmaLinux-10.1-x86_64-boot.iso) = {'b' * 64}\n"
        f"SHA256 (AlmaLinux-10.1-x86_64-minimal.iso) = {'c' * 64}\n"
    )
    pairs = parse_checksum(payload)
    assert len(pairs) == 3
    names = [p.filename for p in pairs]
    assert "AlmaLinux-10.1-x86_64-dvd.iso" in names
    assert "AlmaLinux-10.1-x86_64-boot.iso" in names
    assert "AlmaLinux-10.1-x86_64-minimal.iso" in names


def test_clearsigned_checksum_parser_returns_empty_for_garbage():
    """Boundary check: empty input and random text must yield an empty
    list, not an exception. ``test_iso_checksums.py`` then asserts
    ``assert pairs, "has the CHECKSUM format changed?"`` — that
    operator-facing message only ever fires if the parser is well-
    behaved on garbage input.
    """
    assert parse_checksum("") == []
    assert parse_checksum("not a checksum file at all\n") == []


def test_clearsigned_checksum_parser_rejects_short_hash():
    """A hash shorter than 64 hex characters should not be recognized."""
    payload = f"SHA256 (foo.iso) = {'a' * 32}\n"
    assert parse_checksum(payload) == []
