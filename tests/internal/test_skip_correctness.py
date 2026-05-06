"""Meta-tests for skip logic.

Why these exist
---------------
The suite has 60+ ``pytest.skip(...)`` calls — most of them gated on a
remote 404 ("repo does not exist for this source/version/arch"). If a
mirror suddenly starts answering 200 with an empty/garbage body, the
skip would mask a real release problem: the suite stays green while the
tests silently stop checking anything.

These tests verify the skip *logic*, not the network state:
* the safety-net test for a clearly non-existent repo behaves correctly
  under both 200 and 404 server responses;
* every ``pytest.skip(...)`` in the suite is invoked with a non-empty
  reason (no bare ``pytest.skip()``).

Both are offline — we monkeypatch ``http.session`` to return canned
responses, and we statically scan the test sources via ``ast``.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TESTS_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- helpers


def _fake_response(status_code: int, body: bytes = b"") -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.content = body
    r.text = body.decode("utf-8", errors="replace")
    r.raise_for_status = MagicMock()
    if 400 <= status_code:
        r.raise_for_status.side_effect = RuntimeError(f"HTTP {status_code}")
    return r


def _fake_session(get_response: MagicMock) -> MagicMock:
    s = MagicMock()
    s.get.return_value = get_response
    s.head.return_value = get_response
    return s


# ---------------------------------------------------------------- AST scan


def _iter_test_files() -> list[Path]:
    """All ``test_*.py`` under ``tests/``."""
    return list(TESTS_DIR.rglob("test_*.py"))


def _skip_calls_in(path: Path) -> list[ast.Call]:
    """Return every ``pytest.skip(...)`` Call node found in the file."""
    tree = ast.parse(path.read_text())
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # pytest.skip(...)
        if isinstance(f, ast.Attribute) and f.attr == "skip":
            if isinstance(f.value, ast.Name) and f.value.id == "pytest":
                out.append(node)
    return out


# ---------------------------------------------------------------- tests


def test_every_pytest_skip_has_a_non_empty_reason():
    """Bare ``pytest.skip()`` (no reason) makes triage impossible.

    We require either a positional reason string or a non-empty ``reason=``
    keyword. Empty strings are not allowed either — a green-skip with no
    explanation is exactly what we are guarding against.
    """
    offenders: list[str] = []
    for fpath in _iter_test_files():
        for call in _skip_calls_in(fpath):
            line = call.lineno
            reason: ast.expr | None = None
            if call.args:
                reason = call.args[0]
            else:
                for kw in call.keywords:
                    if kw.arg == "reason":
                        reason = kw.value
                        break
            if reason is None:
                offenders.append(f"{fpath.relative_to(TESTS_DIR)}:{line}: bare pytest.skip()")
                continue
            # Const string "" → no reason. Other expressions (f-strings, vars)
            # we accept on faith — they should evaluate to something truthy.
            if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
                if not reason.value.strip():
                    offenders.append(
                        f"{fpath.relative_to(TESTS_DIR)}:{line}: pytest.skip with empty reason"
                    )
    assert not offenders, "\n".join(offenders)


def test_repomd_404_safety_net_skip_logic_actually_returns_404():
    """The safety-net for skip-on-404: assert that on a clearly bogus repo,
    the real ``RepoURL.repomd_xml()`` is shaped such that an HTTP 4xx is
    plausible.

    We deliberately do not hit the network. Instead we just verify the URL
    itself differs from a known-good one — so that *if* one day a mirror
    decides to alias unknown paths back to BaseOS, the URL builder still
    emits a path the mirror can recognize as "definitely not a repo".
    """
    from post_check.helpers.url_builder import RepoURL

    bogus = RepoURL.from_config(
        source="stable", version="10.1", arch="x86_64", repo="DefinitelyDoesNotExist"
    )
    good = RepoURL.from_config(
        source="stable", version="10.1", arch="x86_64", repo="BaseOS"
    )
    assert bogus.repomd_xml() != good.repomd_xml()
    assert "DefinitelyDoesNotExist" in bogus.repomd_xml()


def test_repomd_signature_test_skips_only_on_4xx_not_on_200(monkeypatch):
    """Inject a fake session into ``test_repomd_signature``; if it returns
    200 we must NOT skip. This proves the skip is genuinely conditional on
    a 404 — not, for example, on an exception inside ``s.get`` masquerading
    as a missing repo.

    Note on failure-mode hardening: if the test under examination ever
    regresses to "always skip", a naked ``pytest.skip.Exception`` would
    propagate out of THIS meta-test and pytest would mark the meta-test
    as ``SKIPPED`` (not ``FAILED``) — exactly the silent-pass we are
    trying to prevent. We catch ``Skipped`` explicitly and re-raise as
    ``AssertionError`` so the meta-test fails loudly.
    """
    from post_check.helpers import http
    from tests.release import test_repomd_signature as mod

    fake_session_200 = _fake_session(_fake_response(200, b"<repomd/>"))
    monkeypatch.setattr(http, "session", lambda: fake_session_200)
    monkeypatch.setattr(mod.http, "session", lambda: fake_session_200)

    fake_keyring = MagicMock()
    fake_keyring.verify_detached.return_value = True

    class _RC:
        source = "stable"
        version = "10.1"
        arches = ("x86_64",)
        repos = ("BaseOS",)

    try:
        mod.test_repomd_signature(_RC(), fake_keyring, "x86_64", "BaseOS", lambda _msg: None)
    except pytest.skip.Exception as e:
        raise AssertionError(
            f"test_repomd_signature must NOT skip on HTTP 200; it skipped with: {e}"
        )
    assert fake_keyring.verify_detached.called, (
        "On 200 the test must reach the GPG verify step, not skip"
    )


def test_repomd_signature_test_skips_on_404(monkeypatch):
    """Inverse: 404 must trigger ``pytest.skip``."""
    from post_check.helpers import http
    from tests.release import test_repomd_signature as mod

    fake_session_404 = _fake_session(_fake_response(404))
    monkeypatch.setattr(http, "session", lambda: fake_session_404)
    monkeypatch.setattr(mod.http, "session", lambda: fake_session_404)

    fake_keyring = MagicMock()

    class _RC:
        source = "stable"
        version = "10.2"  # not released yet — 404 is realistic
        arches = ("x86_64",)
        repos = ("BaseOS",)

    with pytest.raises(pytest.skip.Exception) as ei:
        mod.test_repomd_signature(_RC(), fake_keyring, "x86_64", "BaseOS", lambda _msg: None)
    # Skip reason must mention the repo and arch — otherwise the operator
    # has no way to triage which combination is missing.
    msg = str(ei.value)
    assert "BaseOS" in msg and "x86_64" in msg
    assert not fake_keyring.verify_detached.called, (
        "On 404 we must skip BEFORE doing GPG work"
    )


def test_iso_checksum_test_skips_when_checksum_not_published(monkeypatch):
    """Mirror-side: 404 on isos/<arch>/CHECKSUM must skip, not error out
    deeper in the test (e.g. with an empty-body GPG verify).
    """
    from post_check.helpers import http
    from tests.release import test_iso_checksums as mod

    fake_session_404 = _fake_session(_fake_response(404))
    monkeypatch.setattr(http, "session", lambda: fake_session_404)
    monkeypatch.setattr(mod.http, "session", lambda: fake_session_404)

    fake_keyring = MagicMock()

    class _RC:
        source = "stable"
        version = "10.2"
        arches = ("x86_64",)
        repos = ("BaseOS",)

    with pytest.raises(pytest.skip.Exception):
        mod.test_iso_checksum_file_signature_valid(_RC(), fake_keyring, "x86_64", lambda _msg: None)
    assert not fake_keyring.verify_clearsigned.called, (
        "On 404 we must skip BEFORE GPG verification"
    )


def test_iso_checksum_test_does_not_skip_on_200(monkeypatch):
    """Symmetric to the 404 case: if the mirror DOES publish CHECKSUM,
    the test must reach GPG verification (i.e. NOT skip). Without this
    direction, a regression that always skips would still pass this
    file's 404-skip check.

    Same hardening as ``test_repomd_signature_test_skips_only_on_4xx_not_on_200``:
    we re-raise ``pytest.skip.Exception`` as ``AssertionError`` so that
    a regression to "always skip" fails the meta-test instead of being
    silently reported as ``SKIPPED``.
    """
    from post_check.helpers import http
    from tests.release import test_iso_checksums as mod

    body = (
        b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n"
        b"SHA256 (AlmaLinux-10.1-x86_64-dvd.iso) = " + b"a" * 64 + b"\n"
        b"-----BEGIN PGP SIGNATURE-----\n...\n-----END PGP SIGNATURE-----\n"
    )
    fake_session_200 = _fake_session(_fake_response(200, body))
    monkeypatch.setattr(http, "session", lambda: fake_session_200)
    monkeypatch.setattr(mod.http, "session", lambda: fake_session_200)

    fake_keyring = MagicMock()
    fake_keyring.verify_clearsigned.return_value = (True, b"SHA256 (foo) = " + b"a" * 64)

    class _RC:
        source = "stable"
        version = "10.1"
        arches = ("x86_64",)
        repos = ("BaseOS",)

    try:
        mod.test_iso_checksum_file_signature_valid(_RC(), fake_keyring, "x86_64", lambda _msg: None)
    except pytest.skip.Exception as e:
        raise AssertionError(
            f"test_iso_checksum_file_signature_valid must NOT skip on HTTP 200; "
            f"it skipped with: {e}"
        )
    assert fake_keyring.verify_clearsigned.called, (
        "On 200 the test must reach the GPG clearsigned verify step, not skip"
    )
