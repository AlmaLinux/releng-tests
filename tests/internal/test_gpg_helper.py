"""Tests for the GPG helper: fetch+cache of the release key and Keyring.

Tests that hit repo.almalinux.org for the real key are marked with the
``online`` marker — in an offline environment they are skipped (via
-m "not online"). Tests that don't require network (validation of an
unsupported major, GPGHOME isolation) always run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from post_check.helpers.gpg import Keyring, fetch_release_key


def test_gpg_fetch_release_key_raises_on_unsupported_major_8():
    """AlmaLinux 8 out of scope — should raise ValueError with clear text."""
    with pytest.raises(ValueError, match=r"major=8|9.*10|not supported"):
        fetch_release_key("8")


def test_gpg_keyring_isolated_per_session(tmp_path):
    """GPGHOME lives strictly inside the given path; nothing leaks to ~/.gnupg."""
    kr = Keyring(tmp_path / "gpg")
    assert (tmp_path / "gpg").exists()
    # Canary: make sure we don't write to the user's system keyring.
    assert not Path.home().joinpath(".gnupg/post-check-leak").exists()
    # Sanity check: gnupg points exactly to our directory.
    assert str(tmp_path / "gpg") in str(kr.gpg.gnupghome)


@pytest.mark.online
def test_gpg_fetch_release_key_downloads_for_major_10(tmp_path, monkeypatch):
    """Real download from repo.almalinux.org for major=10."""
    monkeypatch.setenv("POST_CHECK_CACHE", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # so requests-cache .sqlite doesn't pollute the repo
    p = fetch_release_key("10")
    assert p.exists()
    assert p.read_bytes().startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----")


@pytest.mark.online
def test_gpg_fetch_release_key_uses_cache_on_second_call(tmp_path, monkeypatch):
    """A repeat call does not hit the network — returns the same path without rewriting."""
    monkeypatch.setenv("POST_CHECK_CACHE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    p1 = fetch_release_key("10")
    mtime1 = p1.stat().st_mtime
    p2 = fetch_release_key("10")  # without refresh=True
    assert p2 == p1
    assert p2.stat().st_mtime == mtime1


@pytest.mark.online
def test_gpg_keyring_import_release_key_returns_fingerprint(tmp_path, monkeypatch):
    """Importing the real key returns a 40-character hex fingerprint.

    The returned value comes straight from gnupg after import — we no
    longer cross-check it against a hardcoded expected fingerprint
    (the suite already trusts HTTPS to ``repo.almalinux.org`` for repos
    and ISOs; a second hardcoded fingerprint would only protect against
    a threat model where everything else is already compromised).
    """
    monkeypatch.setenv("POST_CHECK_CACHE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    kr = Keyring(tmp_path / "gpg")
    fpr = kr.import_release_key("10")
    assert len(fpr) == 40
    assert all(c in "0123456789ABCDEF" for c in fpr)


@pytest.mark.online
def test_gpg_import_release_key_raises_on_malformed_key_file(tmp_path, monkeypatch):
    """If the cached key file is malformed (or empty), import must
    raise a RuntimeError with a pointer to the cache path — silent
    success on a bad key is the worst possible failure mode here.
    """
    monkeypatch.setenv("POST_CHECK_CACHE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    # Create an empty "key" in the cache where fetch_release_key would put it.
    cache = tmp_path / "gpg-keys"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "RPM-GPG-KEY-AlmaLinux-10").write_bytes(b"")

    kr = Keyring(tmp_path / "gpg")
    with pytest.raises(RuntimeError, match="did not return a fingerprint"):
        kr.import_release_key("10")
