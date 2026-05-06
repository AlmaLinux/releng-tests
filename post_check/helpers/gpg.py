"""GPG helper: sandboxed keyring + dynamic fetch of the AlmaLinux release key.

The key is downloaded from ``repo.almalinux.org`` once into
``~/.cache/post-check/gpg-keys/`` and imported into a per-session sandbox
keyring. The fingerprint of the imported key is whatever the URL serves
— we do NOT cross-check it against a hardcoded list. The whole suite
already trusts HTTPS to ``repo.almalinux.org`` for repos and ISOs, so a
second hardcoded fingerprint check would only protect against a
threat model where the rest of the suite is compromised anyway.
Only AlmaLinux 9 and 10 are supported — major=8 is out of scope.

This module provides:

* :func:`fetch_release_key` — download (with cache) RPM-GPG-KEY-AlmaLinux-<major>.
* :class:`Keyring` — wrapper over python-gnupg with per-session GPGHOME, methods
  ``import_release_key``, ``verify_detached``, ``verify_clearsigned``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import gnupg

from post_check.helpers import http

SUPPORTED_MAJORS = {"9", "10"}

# The AlmaLinux release key URL is the same for every source and
# version — only {major} varies. If AlmaLinux ever moves to a different
# host, this is the single place to update.
_GPG_KEY_URL = "https://repo.almalinux.org/almalinux/RPM-GPG-KEY-AlmaLinux-{major}"


def _cache_dir() -> Path:
    base = Path(os.environ.get("POST_CHECK_CACHE", Path.home() / ".cache" / "post-check"))
    p = base / "gpg-keys"
    p.mkdir(parents=True, exist_ok=True)
    return p


def fetch_release_key(major: str, *, refresh: bool = False) -> Path:
    """Download RPM-GPG-KEY-AlmaLinux-<major> from repo.almalinux.org and return its cache path.

    Idempotent: on repeated calls uses the already-downloaded file if
    ``refresh=False``. The bytes are not validated here — that's done
    implicitly by gnupg in :meth:`Keyring.import_release_key`, which
    rejects a malformed armored block.
    """
    if major not in SUPPORTED_MAJORS:
        raise ValueError(
            f"major={major} is not supported (only {sorted(SUPPORTED_MAJORS)}). "
            f"AlmaLinux 8 out of scope."
        )
    cache = _cache_dir() / f"RPM-GPG-KEY-AlmaLinux-{major}"
    if cache.exists() and not refresh:
        return cache
    url = _GPG_KEY_URL.format(major=major)
    s = http.session()
    r = s.get(url, timeout=30)
    r.raise_for_status()
    cache.write_bytes(r.content)
    return cache


class Keyring:
    """Sandboxed wrapper over gnupg.GPG with per-session GPGHOME."""

    def __init__(self, gpg_home: Path):
        gpg_home.mkdir(parents=True, exist_ok=True)
        self.gpg = gnupg.GPG(gnupghome=str(gpg_home))

    def import_release_key(self, major: str) -> str:
        """Download (if needed) and import RPM-GPG-KEY-AlmaLinux-<major>.

        Returns the fingerprint of the imported key (40 hex characters).
        That fingerprint is whatever ``repo.almalinux.org`` serves —
        we trust HTTPS to that host (same trust we already extend for
        repos and ISOs); we do not cross-check against a hardcoded
        constant.
        """
        path = fetch_release_key(major)
        result = self.gpg.import_keys(path.read_text())
        if not result.fingerprints:
            raise RuntimeError(
                f"GPG import did not return a fingerprint for major={major}; "
                f"the key file at {path} is likely malformed or empty."
            )
        return result.fingerprints[0].upper()

    def verify_detached(self, data: bytes, signature: bytes) -> bool:
        """Verification of a detached signature (e.g. repomd.xml + repomd.xml.asc)."""
        with tempfile.NamedTemporaryFile(delete=False) as sig_f:
            sig_f.write(signature)
            sig_path = sig_f.name
        try:
            verified = self.gpg.verify_data(sig_path, data)
            return bool(verified)
        finally:
            os.unlink(sig_path)

    def verify_clearsigned(self, clearsigned_text: bytes) -> tuple[bool, bytes]:
        """Verifies a clearsigned block and returns (ok, payload).

        ``payload`` — bytes of the content between the clearsign wrapper headers and
        ``-----BEGIN PGP SIGNATURE-----``. On verification failure
        returns ``(False, b"")``.

        Parsing of the clearsign structure is done only to extract the payload —
        signature verification is delegated to python-gnupg, which satisfies the
        requirement of "not parsing the signature manually".
        """
        verified = self.gpg.verify(clearsigned_text)
        if not verified:
            return False, b""

        text = clearsigned_text.decode("utf-8", errors="replace")
        # clearsign structure:
        #   -----BEGIN PGP SIGNED MESSAGE-----
        #   Hash: SHA256
        #   <empty line>
        #   <payload>
        #   -----BEGIN PGP SIGNATURE-----
        #   ...
        #   -----END PGP SIGNATURE-----
        payload_start = text.find("\n\n")  # empty line after Hash:
        sig_start = text.find("-----BEGIN PGP SIGNATURE-----")
        if payload_start < 0 or sig_start < 0 or payload_start >= sig_start:
            return False, b""
        payload = text[payload_start + 2 : sig_start].rstrip("\n").encode()
        return True, payload
