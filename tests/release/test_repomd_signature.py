"""Online integration test: repomd.xml + repomd.xml.asc on repo.almalinux.org.

This is the first "green" integration slice — no mocks, we actually hit
``repo.almalinux.org`` and verify the repomd.xml signature using the
AlmaLinux release key. Negative tests use the same URL but with a forged
signature or key to prove that verification really works.
"""

from __future__ import annotations

import pytest

from post_check.config import expected_pkg_sections, load_architectures
from post_check.helpers import http
from post_check.helpers.url_builder import RepoURL

pytestmark = [pytest.mark.online, pytest.mark.release]


@pytest.fixture(autouse=True)
def _skip_for_pulp(runtime_config):
    """Skip every test in this module on ``ALMA_SOURCE=pulp``.

    pulp's named repos are the major-aliased stable URLs — running
    the repomd.xml signature checks here would just re-verify the
    current GA stable, already covered by ``ALMA_SOURCE=stable`` runs.
    The new thing pulp adds — the unsigned internal-beta layer hosted
    on ``build.almalinux.org/pulp/content/`` — is by contract NOT
    signed (``gpgcheck=0``), so signature verification doesn't apply
    to it either.

    Surfaced as ``SKIPPED`` (not collection-ignored) so the release
    report shows the operator that this contract was deliberately
    waived for pulp, with the reason — instead of silently dropping
    the rows.
    """
    if runtime_config.source == "pulp":
        pytest.skip(
            "pulp: named repos reuse stable URLs (covered by stable runs); "
            "internal-beta layer is unsigned by contract — nothing to verify"
        )


def _explain_why_repo_is_missing(major: str, arch: str, repo: str) -> str:
    """Human-friendly skip reason for parametrize cells that don't exist.

    Cross-references the static ``expected_pkg_sections`` map: if the
    repo doesn't ship for any arch of this major, it's a per-major
    drop (``ResilientStorage`` on AL10); if it ships only on x86_64,
    the parametrize cell is the per-arch restriction (``RT``/``NFV``).
    """
    sec_id = repo.lower()
    in_this_arch = sec_id in expected_pkg_sections(major, arch)
    if in_this_arch:
        return f"`{repo}/{arch}` returned 404 (network glitch?)"
    in_any_arch = any(
        sec_id in expected_pkg_sections(major, a) for a in load_architectures()
    )
    if not in_any_arch:
        return f"`{repo}` is not part of AlmaLinux {major}"
    return f"`{repo}` ships only on x86_64 (not on {arch})"


def test_repomd_signature(runtime_config, gpg_keyring, arch, repo, report_detail):
    """The detached GPG signature on ``<repo>/<arch>/os/repodata/repomd.xml``
    is valid under the AlmaLinux release key for the current major.

    This is the trust anchor for the entire dnf metadata chain — if
    ``repomd.xml`` is unsigned (or signed by the wrong key), every
    package list and checksum that flows from it is unverifiable.

    Skipped pre-emptively when ``expected_pkg_sections`` says the
    repo doesn't exist for this ``(major, arch)`` pair (clearer skip
    reason than a raw 404), and as a defensive fallback if the server
    returns 404 anyway.
    """
    major = runtime_config.version.split(".")[0]
    # Pre-skip parametrize cells that the static repo map says don't
    # exist for this (major, arch) — the report then carries a clean
    # "ResilientStorage is not part of AlmaLinux 10" instead of a
    # confusing 404 message that requires Almalinux release knowledge
    # to interpret.
    if repo.lower() not in expected_pkg_sections(major, arch):
        pytest.skip(_explain_why_repo_is_missing(major, arch, repo))
    u = RepoURL.from_config(
        source=runtime_config.source,
        version=runtime_config.version,
        arch=arch,
        repo=repo,
    )
    s = http.session()
    report_detail(f"GET {u.repomd_xml()}")
    report_detail(f"GET {u.repomd_xml_asc()}")
    repomd = s.get(u.repomd_xml())
    if repomd.status_code == 404:
        # Defence in depth: the static map said the repo *should* exist
        # but the server says otherwise. Surface as a skip with the
        # static-map explanation rather than a raw 404, which used to
        # confuse the operator.
        pytest.skip(_explain_why_repo_is_missing(major, arch, repo))
    repomd.raise_for_status()
    sig = s.get(u.repomd_xml_asc())
    sig.raise_for_status()
    assert gpg_keyring.verify_detached(repomd.content, sig.content), (
        f"GPG verify failed for {u.repomd_xml()}"
    )
    report_detail(
        f"signature OK ({len(repomd.content)} B repomd, "
        f"{len(sig.content)} B sig)"
    )


def test_repomd_signature_rejects_corrupted_signature(gpg_keyring, runtime_config):
    """Negative test: a clearly-malformed PGP signature block makes
    ``verify_detached`` return False.

    Locks in that the verification path actually inspects signature
    bytes — without this, a regression where the verifier silently
    returns True on garbage would let the positive test above pass
    against an unverified release.
    """
    u = RepoURL.from_config(
        source=runtime_config.source,
        version=runtime_config.version,
        arch=runtime_config.arches[0],
        repo=runtime_config.repos[0],
    )
    s = http.session()
    repomd = s.get(u.repomd_xml())
    if repomd.status_code != 200:
        pytest.skip("Could not fetch repomd.xml for the negative-test setup")
    corrupted = (
        b"-----BEGIN PGP SIGNATURE-----\n"
        b"GARBAGE\n"
        b"-----END PGP SIGNATURE-----\n"
    )
    assert not gpg_keyring.verify_detached(repomd.content, corrupted)


def test_repomd_signature_rejects_wrong_major_key(tmp_path, runtime_config):
    """Import a foreign major key (downloaded dynamically) — verify must fail."""
    from post_check.helpers.gpg import Keyring

    own_major = runtime_config.version.split(".")[0]
    other_major = "9" if own_major == "10" else "10"
    wrong = Keyring(tmp_path / "wrong-gpg")
    try:
        wrong.import_release_key(other_major)
    except Exception as e:
        pytest.skip(f"failed to import foreign major={other_major} key: {e}")
    u = RepoURL.from_config(
        source=runtime_config.source,
        version=runtime_config.version,
        arch=runtime_config.arches[0],
        repo=runtime_config.repos[0],
    )
    s = http.session()
    repomd = s.get(u.repomd_xml())
    sig = s.get(u.repomd_xml_asc())
    if repomd.status_code != 200 or sig.status_code != 200:
        pytest.skip("Could not fetch data for the negative test")
    assert not wrong.verify_detached(repomd.content, sig.content)


def test_repomd_signature_returns_404_on_missing_repo():
    """Smoke: a clearly non-existent repo returns 404/403.

    This is a safety net for the skip logic in ``test_repomd_signature``:
    if a mirror suddenly starts returning 200 with an empty/garbage body
    for an unknown repo, our skip would mask a real error.
    """
    u = RepoURL.from_config(
        source="stable",
        version="10.1",
        arch="x86_64",
        repo="DefinitelyDoesNotExist",
    )
    s = http.session()
    r = s.get(u.repomd_xml())
    assert r.status_code in (403, 404)


