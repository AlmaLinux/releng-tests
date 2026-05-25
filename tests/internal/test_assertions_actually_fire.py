"""Mutation-style meta-tests: feed broken data to the helpers that the
real tests depend on, and prove the helpers report the breakage.

Why these exist
---------------
A test that calls ``parser(data)`` and then ``assert pairs`` only catches
total parser failure. If ``parser`` returns a *plausible-but-wrong*
result on garbage input, the test passes. These meta-tests pin the
helpers' behavior on the boundary cases the real tests rely on:

* ``iso.parse_checksum`` — empty input, garbage input, malformed lines.
* ``repo_file.parse`` — ``gpgcheck=0`` is correctly flagged as ``False``;
  ``sslverify=0`` likewise. (If those ever started returning ``True`` by
  default, ``test_almalinux_repos_pkg_gpgcheck_is_1_for_every_repo``
  would silently pass.)
* ``repodata.iter_packages`` on a synthetic ``primary.xml.gz`` —
  yields the package, with the right NEVRA + sha256, so that
  ``test_noarch_parity`` actually has data to compare.
"""
from __future__ import annotations

import gzip
import io

import pytest

from post_check.helpers import iso, repo_file, repodata


# ============================================================ iso.parse_checksum


def test_parse_checksum_empty_input_returns_empty_list():
    assert iso.parse_checksum("") == []


def test_parse_checksum_garbage_returns_empty_list():
    """Defensive: random text without ``SHA256 (...)`` or ``<hash>  <name>``
    must produce no entries. Otherwise ``test_iso_checksum_file_lists_all_expected_iso_names``
    would assert against a list of nonsense.
    """
    payload = "Hello world\nNot a checksum file at all\n"
    assert iso.parse_checksum(payload) == []


def test_parse_checksum_truncated_hash_is_rejected():
    """39 hex chars instead of 64 — must NOT match either format.

    This guards against a regex with too-loose anchors silently catching
    short strings as valid sha256.
    """
    short = "a" * 63  # one short
    payload = f"SHA256 (foo.iso) = {short}\n"
    assert iso.parse_checksum(payload) == []


def test_parse_checksum_gnu_format_round_trips():
    """Sanity: the format produced by ``sha256sum --tag`` is parsed.

    AlmaLinux's ``isos/<arch>/CHECKSUM`` is published only in this
    format. The bare ``<hash>  <filename>`` (default ``sha256sum``
    output) is intentionally NOT supported — see iso.py docstring.
    """
    h = "0" * 64
    payload = f"SHA256 (AlmaLinux-10.1-x86_64-dvd.iso) = {h}\n"
    [pair] = iso.parse_checksum(payload)
    assert pair.filename == "AlmaLinux-10.1-x86_64-dvd.iso"
    assert pair.sha256 == h


def test_parse_checksum_rejects_bare_sha256sum_output():
    """The default ``sha256sum`` (no ``--tag``) layout must be rejected.

    AlmaLinux never publishes CHECKSUM in this form; if a mirror were to
    serve such a body, accepting it would silently bypass verification
    on a malformed input. We pin "rejected" rather than "best-effort
    parsed".
    """
    payload = "f" * 64 + "  AlmaLinux-10.1-x86_64-boot.iso\n"
    assert iso.parse_checksum(payload) == []


# ============================================================ repo_file.parse


_REPO_TEMPLATE = """\
[almalinux-baseos]
name=AlmaLinux 10 - BaseOS
baseurl=https://repo.almalinux.org/almalinux/10/BaseOS/x86_64/os/
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10
gpgcheck={gpgcheck}
sslverify={sslverify}
enabled={enabled}
"""


def test_repo_file_parse_flags_gpgcheck_zero_as_false():
    """If ``test_almalinux_repos_pkg_gpgcheck_is_1_for_every_repo`` is to
    catch a vendor regression, ``parse`` must report ``gpgcheck=0`` as
    ``sec.gpgcheck is False``. This is the contract the real test relies
    on.
    """
    text = _REPO_TEMPLATE.format(gpgcheck=0, sslverify=1, enabled=1)
    [sec] = repo_file.parse(text)
    assert sec.gpgcheck is False


def test_repo_file_parse_flags_gpgcheck_one_as_true():
    text = _REPO_TEMPLATE.format(gpgcheck=1, sslverify=1, enabled=1)
    [sec] = repo_file.parse(text)
    assert sec.gpgcheck is True


def test_repo_file_parse_flags_sslverify_zero_as_false():
    text = _REPO_TEMPLATE.format(gpgcheck=1, sslverify=0, enabled=1)
    [sec] = repo_file.parse(text)
    assert sec.sslverify is False


def test_repo_file_parse_default_sslverify_is_true_when_absent():
    """If ``sslverify`` line is missing, configparser must fall back to
    ``True``, otherwise the no-sslverify-disabled test would flag every
    valid repo file in the world.
    """
    text = """\
[s]
name=s
baseurl=https://example/
gpgkey=file:///etc/pki/rpm-gpg/X
gpgcheck=1
"""
    [sec] = repo_file.parse(text)
    assert sec.sslverify is True


def test_repo_file_parse_handles_dollar_signs_without_interpolation_error():
    """``configparser`` with default interpolation would blow up on
    ``$releasever`` / ``%(...)s``. The helper sets ``interpolation=None``;
    if that ever regresses, every almalinux-repos test fails to even
    parse the input. This pins the behavior.
    """
    text = """\
[s]
name=AlmaLinux $releasever
baseurl=https://repo.almalinux.org/almalinux/$releasever/BaseOS/$basearch/os/
gpgcheck=1
"""
    [sec] = repo_file.parse(text)
    assert "$releasever" in (sec.baseurl or "")


def test_repo_file_substitute_replaces_both_vars():
    out = repo_file.substitute(
        "https://x/$releasever/$basearch/os", basearch="aarch64", releasever="10"
    )
    assert out == "https://x/10/aarch64/os"


# ============================================================ repodata.iter_packages


def _build_primary_xml(packages: list[dict]) -> bytes:
    """Build a minimal valid ``primary.xml`` with the given packages.

    Mirrors the schema ``post_check.helpers.repodata`` actually parses
    (common namespace, ``<package>`` / ``<name>`` / ``<version>`` /
    ``<checksum>`` / ``<location>``).
    """
    head = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<metadata xmlns="http://linux.duke.edu/metadata/common" '
        'xmlns:rpm="http://linux.duke.edu/metadata/rpm" '
        f'packages="{len(packages)}">\n'
    )
    body = ""
    for p in packages:
        body += (
            f'  <package type="rpm">\n'
            f'    <name>{p["name"]}</name>\n'
            f'    <arch>{p["arch"]}</arch>\n'
            f'    <version epoch="{p.get("epoch", "0")}" '
            f'ver="{p["version"]}" rel="{p["release"]}"/>\n'
            f'    <checksum type="sha256">{p["checksum"]}</checksum>\n'
            f'    <location href="{p["location"]}"/>\n'
            f'  </package>\n'
        )
    tail = "</metadata>\n"
    return (head + body + tail).encode()


@pytest.fixture
def synthetic_primary_url(tmp_path):
    """Write a synthetic ``primary.xml.gz`` to disk and return a ``file://`` URL."""
    pkgs = [
        dict(
            name="almalinux-release",
            arch="x86_64",
            epoch="0",
            version="10.1",
            release="16.el10",
            checksum="a" * 64,
            location="Packages/a/almalinux-release-10.1-16.el10.x86_64.rpm",
        ),
        dict(
            name="almalinux-release",
            arch="aarch64",
            epoch="0",
            version="10.1",
            release="16.el10",
            checksum="b" * 64,
            location="Packages/a/almalinux-release-10.1-16.el10.aarch64.rpm",
        ),
        dict(
            name="basesystem",
            arch="noarch",
            epoch="0",
            version="11",
            release="13.el10",
            checksum="c" * 64,
            location="Packages/b/basesystem-11-13.el10.noarch.rpm",
        ),
    ]
    raw = _build_primary_xml(pkgs)
    target = tmp_path / "primary.xml.gz"
    target.write_bytes(gzip.compress(raw))
    return target.as_uri()


def test_iter_packages_yields_each_package_with_correct_nevra(synthetic_primary_url, monkeypatch):
    """End-to-end smoke for the parser the parity tests depend on.

    We don't go through HTTP — we monkeypatch the session to read from
    the local file. If this ever returns 0 packages on a valid input,
    every parity test would skip with "no shared NEVRAs" instead of
    failing.
    """

    class _LocalSession:
        def get(self, url, *_, **__):
            from urllib.parse import urlparse

            path = urlparse(url).path
            data = open(path, "rb").read()
            r = MagicMock_response(200, data)
            return r

        def head(self, *_, **__):
            return MagicMock_response(200, b"")

    s = _LocalSession()
    pkgs = list(repodata.iter_packages(s, synthetic_primary_url))
    assert len(pkgs) == 3
    nevras = sorted(p.nevra for p in pkgs)
    assert nevras == sorted(
        [
            "almalinux-release-10.1-16.el10.x86_64",
            "almalinux-release-10.1-16.el10.aarch64",
            "basesystem-11-13.el10.noarch",
        ]
    )


def test_iter_packages_arch_filter_drops_other_arches(synthetic_primary_url):
    class _LocalSession:
        def get(self, url, *_, **__):
            from urllib.parse import urlparse

            path = urlparse(url).path
            data = open(path, "rb").read()
            return MagicMock_response(200, data)

    s = _LocalSession()
    pkgs = list(repodata.iter_packages(s, synthetic_primary_url, arch_filter="noarch"))
    assert len(pkgs) == 1
    assert pkgs[0].arch == "noarch"


# ============================================================ helpers


def MagicMock_response(status_code: int, body: bytes):
    """Tiny local helper mimicking just the bits ``iter_packages`` uses.

    ``iter_packages`` uses ``session.get(url, stream=True, ...)`` as a
    context manager and reads ``resp.raw``. We have to support both the
    context manager protocol and the ``raw`` attribute.
    """

    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.raw = io.BytesIO(body)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    return _Resp()


# ============================================================ Keyring


def test_keyring_verify_detached_returns_false_on_garbage_signature(tmp_path):
    """The negative path of GPG verification is the whole reason
    ``test_repomd_signature_rejects_corrupted_signature`` exists. If
    ``verify_detached`` ever started returning a truthy object on a
    clearly bogus signature, that test would still pass (because it
    asserts ``not gpg_keyring.verify_detached(...)`` — and a buggy
    "always True" verifier would fail loudly here, not there).

    No network: we build an empty sandbox keyring and feed it junk.
    Expected: ``False``. We do NOT require gpg to be installed; if
    python-gnupg cannot find a binary, skip.
    """
    from post_check.helpers.gpg import Keyring

    try:
        kr = Keyring(tmp_path / "gpg")
    except Exception as e:  # gpg binary missing on host
        pytest.skip(f"gnupg not available: {e}")

    # No keys imported, garbage payload, garbage sig — must be False.
    assert kr.verify_detached(b"hello", b"-----BEGIN PGP SIGNATURE-----\nGARBAGE\n-----END PGP SIGNATURE-----\n") is False


def test_keyring_verify_clearsigned_returns_false_on_garbage(tmp_path):
    from post_check.helpers.gpg import Keyring

    try:
        kr = Keyring(tmp_path / "gpg")
    except Exception as e:
        pytest.skip(f"gnupg not available: {e}")
    ok, payload = kr.verify_clearsigned(b"not a clearsigned message at all")
    assert ok is False
    assert payload == b""


# ============================================================ rpm_evr.vercmp / evr_cmp
#
# Pinning the comparator's behavior is critical for ``test_noarch_parity``
# invariant (B): if ``evr_cmp`` ever started reporting "1.1 > 1.2" or
# tied them, the test would silently choose the wrong "latest" per arch
# and either pass on a real version skew or fail on a legal stale build.
# These cases are the canonical rpmvercmp ones from rpm's own test
# suite — they exist to catch a regression in our port.


def _v(a, b):
    from post_check.helpers.rpm_evr import vercmp
    return vercmp(a, b)


@pytest.mark.parametrize(
    "a,b,expected",
    [
        # equality
        ("1.0", "1.0", 0),
        ("", "", 0),
        # plain numeric ordering
        ("1.1", "1.2", -1),
        ("1.2", "1.1", 1),
        ("1.10", "1.2", 1),  # 10 > 2 numerically (not lexically)
        # leading zeros are stripped
        ("1.010", "1.10", 0),
        ("1.0", "1", 1),  # "1.0" has an extra segment after the dot — wins
        # alpha vs numeric: numeric wins on type mismatch
        ("1a", "1", 1),
        ("1", "1a", -1),
        ("1.a", "1.b", -1),
        # tilde sorts lower than anything (incl. empty)
        ("1.0~rc1", "1.0", -1),
        ("1.0", "1.0~rc1", 1),
        ("1.0~rc1", "1.0~rc2", -1),
        ("1.0~rc2", "1.0~rc1", 1),
        # caret sorts higher than empty, lower than alphanumeric continuation
        ("1.0^", "1.0", 1),
        ("1.0", "1.0^", -1),
        ("1.0^20240101", "1.0^20231231", 1),
        # release-style strings
        ("16.el10", "8.el10", 1),
        ("16.el10", "16.el10_1", -1),
    ],
)
def test_rpm_vercmp_canonical_cases(a, b, expected):
    assert _v(a, b) == expected, f"vercmp({a!r}, {b!r}) != {expected}"


def test_rpm_evr_cmp_compares_epoch_first():
    from post_check.helpers.rpm_evr import evr_cmp

    # Higher epoch dominates a lower V-R.
    assert evr_cmp(("1", "1.0", "1.el10"), ("0", "99.0", "99.el10")) == 1
    # Empty / "0" / None epoch all mean 0.
    assert evr_cmp(("", "1.0", "1"), ("0", "1.0", "1")) == 0
    assert evr_cmp((None, "1.0", "1"), ("0", "1.0", "1")) == 0


def test_rpm_evr_cmp_falls_through_to_release_when_version_ties():
    from post_check.helpers.rpm_evr import evr_cmp

    assert evr_cmp(("0", "1.0", "1.el10"), ("0", "1.0", "2.el10")) == -1
    assert evr_cmp(("0", "1.0", "2.el10"), ("0", "1.0", "1.el10")) == 1
    assert evr_cmp(("0", "1.0", "1.el10"), ("0", "1.0", "1.el10")) == 0


def test_rpm_evr_str_drops_zero_epoch():
    from post_check.helpers.rpm_evr import evr_str

    assert evr_str(("0", "1.0", "1.el10")) == "1.0-1.el10"
    assert evr_str(("", "1.0", "1.el10")) == "1.0-1.el10"
    assert evr_str(("2", "1.0", "1.el10")) == "2:1.0-1.el10"


# ============================================================ noarch parity logic
#
# These tests feed `compute_noarch_parity_failures` synthetic cells and
# prove each invariant fires on the right shape of breakage AND stays
# silent on the legal-but-asymmetric shapes (arch-specific noarch,
# stale historical builds on one arch). Without these, the online test
# could silently pass on a real release skew (the whole reason
# invariant (B) was added) and we'd never know until a user reported it.
#
# We hit the pure check function directly — no network, no XML parsing.
# The XML/HTTP path is separately covered by
# ``test_iter_packages_yields_each_package_with_correct_nevra``.


def _pkg(name, version, release, sha, arch="noarch", epoch="0", sourcerpm=None):
    """Tiny helper to spell out a noarch ``Package`` in one line.

    ``sourcerpm`` defaults to ``"<name>-<version>-<release>.src.rpm"``
    so synthetic cases automatically carry a plausible sourcerpm. Tests
    that need the "no sourcerpm" branch can pass ``sourcerpm=""``
    explicitly.
    """
    from post_check.helpers.repodata import Package
    if sourcerpm is None:
        sourcerpm = f"{name}-{version}-{release}.src.rpm"
    return Package(
        name=name,
        epoch=epoch,
        version=version,
        release=release,
        arch=arch,
        checksum_type="sha256",
        checksum=sha,
        location=f"Packages/{name[0]}/{name}-{version}-{release}.{arch}.rpm",
        sourcerpm=sourcerpm,
    )


def test_noarch_parity_skips_modular_packages():
    """Modular packages (``.module`` in release) must be filtered out
    of the parity check.

    Real-world reason: AL9.7 ships ``apache-commons-cli`` as a
    modular RPM (``1.9.0-4.module_el9.6.0+148+fb6dc857``) on the four
    main arches, but i686 only has the legacy non-modular
    ``1.4-18.el9_5`` because modules aren't published for i686 at all.
    Without filtering, ~90 packages flag i686 as "drifting" — but
    that's a property of the modularity contract (modules are not
    cross-arch by design), not a release bug. The filter lives in
    :func:`tests.release.test_noarch_parity._is_modular` and runs at
    collection time so neither (A) nor (B) ever sees modular pkgs.
    """
    from tests.release.test_noarch_parity import _is_modular

    modular = _pkg(
        "apache-commons-cli", "1.9.0", "4.module_el9.6.0+148+fb6dc857", "a" * 64,
    )
    assert _is_modular(modular)

    plain = _pkg("ant", "1.10.9", "15.el9", "b" * 64)
    assert not _is_modular(plain)

    # Real-life pattern from AL9 vault: legacy non-modular release
    # has no ``.module`` segment even though it lives next to a
    # modular content set.
    legacy = _pkg("apache-commons-cli", "1.4", "18.el9_5", "c" * 64)
    assert not _is_modular(legacy)


def test_noarch_parity_clean_release_has_no_failures():
    """Baseline: identical noarch in every arch of every repo → both
    invariants silent. If this ever starts reporting failures, the
    check is over-eager and every release would falsely fail.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    pkg = _pkg("basesystem", "11", "13.el10", "c" * 64)
    cells = {
        ("BaseOS", "x86_64"):  [pkg],
        ("BaseOS", "aarch64"): [pkg],
        ("BaseOS", "ppc64le"): [pkg],
        ("BaseOS", "s390x"):   [pkg],
    }
    a, b = compute_noarch_parity_failures(cells)
    assert a == [] and b == [], (a, b)


def test_noarch_parity_invariant_a_fires_on_same_nevra_different_sha():
    """Same NEVRA, different bytes across arches — (A) must fire.

    Real-world cause: someone re-published a noarch RPM under the same
    NEVRA into one arch's repo without rebuilding everywhere. Before
    invariant (A) existed, this would silently break ``dnf`` clients
    that pulled the file from a different arch's mirror.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        ("BaseOS", "x86_64"):  [_pkg("foo", "1.0", "1.el10", "a" * 64)],
        ("BaseOS", "aarch64"): [_pkg("foo", "1.0", "1.el10", "b" * 64)],
    }
    a, b = compute_noarch_parity_failures(cells)
    assert a, "invariant (A) should fire on NEVRA collision with different sha256"
    assert "foo-1.0-1.el10.noarch" in "\n".join(a)
    # (B) must ALSO fire — the latest-per-arch tuples (evr, sha) differ
    # because the sha differs. That's correct: a divergent rebuild is
    # both an (A) violation and a (B) violation.
    assert b


def test_noarch_parity_invariant_b_fires_on_version_skew():
    """The user's case: x86_64 has foo-1.2, aarch64 has foo-1.1.
    Different NEVRAs, so (A) is silent. (B) must fire — that's the
    whole reason it was added.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        ("BaseOS", "x86_64"):  [_pkg("foo", "1.2", "1.el10", "a" * 64)],
        ("BaseOS", "aarch64"): [_pkg("foo", "1.1", "1.el10", "b" * 64)],
    }
    a, b = compute_noarch_parity_failures(cells)
    assert a == [], "invariant (A) keys by NEVRA; both NEVRAs are singletons → silent"
    assert b, "invariant (B) must fire on version skew between arches"
    msg = "\n".join(b)
    # New compact format: single-package failures put the
    # ``<repo>/<name>`` in the group header — no separate package list
    # line for 1-package groups.
    assert "BaseOS/foo" in msg, msg
    assert "1.2-1.el10" in msg and "1.1-1.el10" in msg


def test_noarch_parity_invariant_b_silent_when_only_one_arch_has_the_higher_version():
    """Symmetric case to the previous one — aarch64 is ahead, x86_64
    is behind. Same bug, different direction. Must fire all the same.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        ("BaseOS", "x86_64"):  [_pkg("foo", "1.1", "1.el10", "a" * 64)],
        ("BaseOS", "aarch64"): [_pkg("foo", "1.2", "1.el10", "b" * 64)],
        ("BaseOS", "ppc64le"): [_pkg("foo", "1.2", "1.el10", "b" * 64)],
        ("BaseOS", "s390x"):   [_pkg("foo", "1.2", "1.el10", "b" * 64)],
    }
    a, b = compute_noarch_parity_failures(cells)
    assert a == []
    assert b
    msg = "\n".join(b)
    assert "BaseOS/foo" in msg, msg


def test_noarch_parity_invariant_b_silent_on_stale_build_on_one_arch():
    """Legal asymmetry: x86_64 has BOTH the current build (2.8.2) AND
    a leftover stale one (2.8.1) from a previous minor. Other arches
    have only 2.8.2. ``dnf install`` resolves to 2.8.2 on every arch,
    so this is not a release defect.

    (B) compares only the latest EVR per arch, so the stale 2.8.1
    on x86_64 must NOT make the test fail. (A) skips because the
    stale NEVRA is in a single cell.

    If (B) ever stops doing the per-arch max and starts comparing EVR
    *sets*, this test will catch it.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    current = _pkg("virt-v2v-bash-completion", "2.8.2", "1.el10", "c" * 64)
    stale = _pkg("virt-v2v-bash-completion", "2.8.1", "8.el10", "d" * 64)
    cells = {
        ("AppStream", "x86_64"):  [current, stale],
        ("AppStream", "aarch64"): [current],
        ("AppStream", "ppc64le"): [current],
        ("AppStream", "s390x"):   [current],
    }
    a, b = compute_noarch_parity_failures(cells)
    assert a == [], a
    assert b == [], b


def test_noarch_parity_invariant_b_silent_on_arch_specific_noarch():
    """Legal asymmetry: ``syslinux-nonlinux`` only makes sense on
    x86_64 and is shipped there only. The name is in exactly one
    arch, so (B) skips by construction (no allow-list needed).
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        ("BaseOS", "x86_64"):  [_pkg("syslinux-nonlinux", "6.04", "1.el10", "e" * 64)],
        ("BaseOS", "aarch64"): [],
        ("BaseOS", "ppc64le"): [],
        ("BaseOS", "s390x"):   [],
    }
    a, b = compute_noarch_parity_failures(cells)
    assert a == [] and b == []


def test_noarch_parity_invariant_b_scoped_per_repo_not_cross_repo():
    """(B) is keyed by ``(repo, name)``, not by ``name`` alone. If a
    package legitimately ships a different version in BaseOS vs
    AppStream (rare, but possible during a minor bump where AppStream
    ran ahead), that's not a parity defect — they're separate repos.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        # foo-1.1 in BaseOS on every arch (consistent)
        ("BaseOS", "x86_64"):     [_pkg("foo", "1.1", "1.el10", "a" * 64)],
        ("BaseOS", "aarch64"):    [_pkg("foo", "1.1", "1.el10", "a" * 64)],
        # foo-1.2 in AppStream on every arch (consistent within AppStream)
        ("AppStream", "x86_64"):  [_pkg("foo", "1.2", "1.el10", "b" * 64)],
        ("AppStream", "aarch64"): [_pkg("foo", "1.2", "1.el10", "b" * 64)],
    }
    a, b = compute_noarch_parity_failures(cells)
    assert a == []
    assert b == [], "different versions in different repos must NOT be flagged"


def test_noarch_parity_invariant_b_fires_when_new_build_missed_one_arch():
    """The realistic publication-skew bug: a new build of ``foo``
    landed in three arches but the publish to s390x failed/was
    skipped. dnf upgrade on s390x silently keeps the old build.

    (A) is silent — no NEVRA collision. (B) must fire and name
    s390x as the laggard.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    new = _pkg("foo", "1.2", "1.el10", "n" * 64)
    old = _pkg("foo", "1.1", "1.el10", "o" * 64)
    cells = {
        ("BaseOS", "x86_64"):  [new],
        ("BaseOS", "aarch64"): [new],
        ("BaseOS", "ppc64le"): [new],
        ("BaseOS", "s390x"):   [old],
    }
    a, b = compute_noarch_parity_failures(cells)
    assert a == []
    assert b
    msg = "\n".join(b)
    # New format: full NVRA per arch-cohort, e.g.
    #   ``foo-1.1-1.el10.noarch  on [s390x]``
    #   ``foo-1.2-1.el10.noarch  on [aarch64, ppc64le, x86_64]``
    # We assert each arch appears with its NVRA — the exact bracketing
    # and column-spacing is incidental.
    assert "foo-1.1-1.el10.noarch" in msg, msg
    assert "foo-1.2-1.el10.noarch" in msg, msg
    # The laggard arch must be visible alongside its old NVRA on the
    # same line — checks the grouping wasn't accidentally inverted.
    s390x_line = next(ln for ln in b if "s390x" in ln and "on" in ln)
    assert "foo-1.1-1.el10.noarch" in s390x_line, s390x_line


def test_noarch_parity_invariant_b_fires_on_same_evr_different_sha_across_arches():
    """Edge case: same name AND same latest EVR in every arch, but
    sha256 differs. This is really just invariant (A) by another
    name (the NEVRAs are identical), but (B)'s tuple comparison
    happens to also catch it — which is fine, redundancy here is a
    feature, not a bug.

    Pinned so a future "compare only EVR, ignore sha in (B)"
    refactor doesn't quietly weaken the check.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        ("BaseOS", "x86_64"):  [_pkg("foo", "1.0", "1.el10", "a" * 64)],
        ("BaseOS", "aarch64"): [_pkg("foo", "1.0", "1.el10", "b" * 64)],
    }
    a, b = compute_noarch_parity_failures(cells)
    assert a, "(A) catches it — same NEVRA, different sha"
    assert b, "(B) also catches it — same latest EVR but different sha"


def test_noarch_parity_empty_cells_produces_no_failures():
    """No data → no failures. Guard against a check that would crash
    or false-positive when, say, ``ALMA_REPOS`` filters everything out
    or every repo 404s for the chosen source/version.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    a, b = compute_noarch_parity_failures({})
    assert a == [] and b == []


# ---- output-format pins for the grouped (B) renderer --------------------


def test_noarch_parity_b_groups_by_srpm_and_classifies_version_skew():
    """Real-world shape: many subpackages of one SRPM all flip
    together when a rebuild misses one arch. The renderer must
    collapse them into a single group whose header names the SRPM
    and labels the cause as ``version skew``.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    # Three subpackages of the same SRPM ``gnome-shell-extensions``,
    # built at 49.0-2 on x86_64/aarch64 and at 49.0-3 on i686.
    src_old = "gnome-shell-extensions-49.0-2.el10.src.rpm"
    src_new = "gnome-shell-extensions-49.0-3.el10.src.rpm"
    cells: dict = {
        ("AppStream", "x86_64"): [
            _pkg("gnome-shell-extension-apps-menu", "49.0", "2.el10", "1" * 64, sourcerpm=src_old),
            _pkg("gnome-shell-extension-auto-move-windows", "49.0", "2.el10", "2" * 64, sourcerpm=src_old),
            _pkg("gnome-shell-extension-common", "49.0", "2.el10", "3" * 64, sourcerpm=src_old),
        ],
        ("AppStream", "aarch64"): [
            _pkg("gnome-shell-extension-apps-menu", "49.0", "2.el10", "1" * 64, sourcerpm=src_old),
            _pkg("gnome-shell-extension-auto-move-windows", "49.0", "2.el10", "2" * 64, sourcerpm=src_old),
            _pkg("gnome-shell-extension-common", "49.0", "2.el10", "3" * 64, sourcerpm=src_old),
        ],
        ("AppStream", "i686"): [
            _pkg("gnome-shell-extension-apps-menu", "49.0", "3.el10", "4" * 64, sourcerpm=src_new),
            _pkg("gnome-shell-extension-auto-move-windows", "49.0", "3.el10", "5" * 64, sourcerpm=src_new),
            _pkg("gnome-shell-extension-common", "49.0", "3.el10", "6" * 64, sourcerpm=src_new),
        ],
    }
    _a, b = compute_noarch_parity_failures(cells)
    msg = "\n".join(b)
    # Single group block — three names collapsed into one.
    assert "in 1 group(s)" in msg, msg
    # Cause classified as version skew (the headline for the operator).
    assert "version skew" in msg, msg
    # Multi-package group: SRPM name surfaces in the header.
    assert "gnome-shell-extensions" in msg, msg
    # Each binary appears once per arch-cohort, with its full NVRA.
    # Two arch-cohorts here (49.0-2 group and i686-only 49.0-3 group),
    # so we expect each binary name to appear twice in NVRA form, once
    # per cohort, but the two NVRAs differ by version-release.
    assert msg.count("- gnome-shell-extension-apps-menu-49.0-2.el10.noarch") == 1
    assert msg.count("- gnome-shell-extension-apps-menu-49.0-3.el10.noarch") == 1
    assert msg.count("- gnome-shell-extension-common-49.0-2.el10.noarch") == 1
    assert msg.count("- gnome-shell-extension-common-49.0-3.el10.noarch") == 1
    # The arch-cohort header lists arches with their EVR group; the
    # i686 cohort (alone) carries the 49.0-3 NVRAs.
    i686_header = next(ln for ln in b if "i686" in ln and "on" in ln)
    assert "i686" in i686_header
    # And the i686 cohort's NVRA lines show the 49.0-3 release.
    i686_idx = b.index(i686_header)
    cohort_lines = b[i686_idx + 1 : i686_idx + 4]
    assert any("49.0-3.el10" in ln for ln in cohort_lines), cohort_lines


def test_noarch_parity_b_classifies_rebuild_without_version_bump():
    """Same EVR everywhere, different sha256 — the cause label must
    say ``rebuild without version bump``, not ``version skew``.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    # Same NEVRA on two arches but bytes differ. Only one binary, so
    # the SRPM grouping is trivial — but the cause label is what's
    # being pinned here.
    src = "foo-1.0-1.el10.src.rpm"
    cells = {
        ("BaseOS", "x86_64"):  [_pkg("foo", "1.0", "1.el10", "a" * 64, sourcerpm=src)],
        ("BaseOS", "aarch64"): [_pkg("foo", "1.0", "1.el10", "b" * 64, sourcerpm=src)],
    }
    _a, b = compute_noarch_parity_failures(cells)
    msg = "\n".join(b)
    assert "rebuild without version bump" in msg, msg
    assert "version skew" not in msg, msg
    # Single-package group puts the name in the header
    # (``BaseOS/foo — rebuild without version bump``) — no separate
    # ``- foo`` list line.
    assert "BaseOS/foo" in msg, msg


def test_noarch_parity_b_separate_groups_when_different_srpm_pattern():
    """Two SRPMs broken on different arch patterns must produce two
    distinct groups, not be lumped together. Otherwise the operator
    can't tell which rebuild missed which arch.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    # SRPM A: i686 ahead of x86_64.
    # SRPM B: s390x ahead of x86_64.
    cells = {
        ("AppStream", "x86_64"): [
            _pkg("aaa-bin", "1.0", "1.el10", "1" * 64, sourcerpm="aaa-1.0-1.el10.src.rpm"),
            _pkg("bbb-bin", "2.0", "1.el10", "2" * 64, sourcerpm="bbb-2.0-1.el10.src.rpm"),
        ],
        ("AppStream", "i686"): [
            _pkg("aaa-bin", "1.0", "2.el10", "3" * 64, sourcerpm="aaa-1.0-2.el10.src.rpm"),
            _pkg("bbb-bin", "2.0", "1.el10", "2" * 64, sourcerpm="bbb-2.0-1.el10.src.rpm"),
        ],
        ("AppStream", "s390x"): [
            _pkg("aaa-bin", "1.0", "1.el10", "1" * 64, sourcerpm="aaa-1.0-1.el10.src.rpm"),
            _pkg("bbb-bin", "2.0", "2.el10", "4" * 64, sourcerpm="bbb-2.0-2.el10.src.rpm"),
        ],
    }
    _a, b = compute_noarch_parity_failures(cells)
    msg = "\n".join(b)
    # Two distinct groups — one per SRPM-pattern.
    assert "in 2 group(s)" in msg, msg
    # Single-package groups put the name in the header as
    # ``AppStream/aaa-bin``; check both groups appear once.
    assert msg.count("AppStream/aaa-bin") == 1, msg
    assert msg.count("AppStream/bbb-bin") == 1, msg


def test_noarch_parity_b_truncates_huge_groups_at_50_packages():
    """A pathological SRPM can ship hundreds of subpackages — the
    output should cap the per-group package list to keep release
    reports readable.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    src_a = "huge-1.0-1.el10.src.rpm"
    src_b = "huge-1.0-2.el10.src.rpm"
    pkgs_x86 = [
        _pkg(f"huge-sub-{i:03d}", "1.0", "1.el10", "1" * 64, sourcerpm=src_a)
        for i in range(60)
    ]
    pkgs_i686 = [
        _pkg(f"huge-sub-{i:03d}", "1.0", "2.el10", "2" * 64, sourcerpm=src_b)
        for i in range(60)
    ]
    cells = {
        ("AppStream", "x86_64"): pkgs_x86,
        ("AppStream", "i686"):   pkgs_i686,
    }
    _a, b = compute_noarch_parity_failures(cells)
    msg = "\n".join(b)
    # Header counts all 60 affected — wording is ``60 noarch packages``.
    assert "60 noarch packages" in msg, msg
    # But only first 50 are listed; the rest are summarised.
    assert "- huge-sub-049" in msg
    assert "- huge-sub-050" not in msg
    assert "and 10 more" in msg


def test_noarch_parity_a_groups_arches_by_sha():
    """When the same NEVRA carries different bytes across arches, the
    (A) renderer should bucket arches by their sha256 prefix so the
    operator immediately sees which arches diverged from which.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    # foo-1.0 — bytes ``aaa…`` on x86_64+s390x, bytes ``bbb…`` on aarch64.
    cells = {
        ("BaseOS", "x86_64"):  [_pkg("foo", "1.0", "1.el10", "a" * 64)],
        ("BaseOS", "s390x"):   [_pkg("foo", "1.0", "1.el10", "a" * 64)],
        ("BaseOS", "aarch64"): [_pkg("foo", "1.0", "1.el10", "b" * 64)],
    }
    a, _b = compute_noarch_parity_failures(cells)
    msg = "\n".join(a)
    assert "foo-1.0-1.el10.noarch" in msg
    # Both sha prefixes present, with their arch lists.
    assert "aaaaaaaaaaaa" in msg
    assert "bbbbbbbbbbbb" in msg
    assert "BaseOS/x86_64" in msg and "BaseOS/aarch64" in msg


# ---- known_drift_b allowlist contract -----------------------------------
#
# The (B) allowlist (config/noarch_parity_known_drift.yaml) silences
# specific (repo, name) pairs whose drift the release process has
# accepted, AND only for the explicit set of arches whose lag was
# accepted. Today every entry is ``[i686]``: AL9 i686 is stuck on a
# Z-stream tag while the four main arches moved on, and there will be
# no rebuild. Operationally the allowlist is the only "make red green"
# knob the parity test exposes, so its contract has to be tight:
#
#  * suppression is keyed by (repo, name) AND by the set of drifting
#    arches — listing ``ant: [i686]`` does NOT silence a future
#    ppc64le rebuild that misses x86_64;
#  * suppression is keyed exact (repo, name) — listing the same name
#    in a different repo doesn't widen the silence cross-repo;
#  * suppression applies to (B) only — (A) keeps full reach even on
#    listed names (mirror divergence / re-publish under same NEVRA
#    must still surface);
#  * suppression is a post-check filter — listing a clean pair has no
#    effect (no false-pass risk);
#  * the headline reports the suppression count whenever real (B)
#    failures coexist with suppressions, so a reviewer can tell
#    "5 of 5 real" from "5 real + 60 known".
#
# If any of these regress, the allowlist either silences too much
# (every future drift slides past, including arches whose drift was
# never accepted) or too little (operator maintains the contract in
# two places). The tests below pin each property.


def test_noarch_parity_known_drift_b_silences_listed_arch_drift():
    """Baseline: a real (B) drift on a listed (repo, name) where the
    drifting arch is in the allowed set is dropped from failures_b.
    Exactly the production use-case — AppStream/ant drifts on i686,
    YAML accepts ``[i686]``, test passes silently.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        ("AppStream", "x86_64"):  [_pkg("ant", "1.10.9", "15.el9", "a" * 64)],
        ("AppStream", "aarch64"): [_pkg("ant", "1.10.9", "15.el9", "a" * 64)],
        ("AppStream", "i686"):    [_pkg("ant", "1.10.9", "11.el9_5", "b" * 64)],
    }
    a, b = compute_noarch_parity_failures(
        cells, known_drift_b={("AppStream", "ant"): frozenset({"i686"})}
    )
    assert a == [], a
    assert b == [], (
        "drift on an allowed arch must produce no (B) lines, "
        "otherwise the release report still shows it as a failure"
    )


def test_noarch_parity_known_drift_b_fires_when_drift_extends_beyond_allowed_arches():
    """The arch-scoped property — the whole point of listing arches
    in the YAML. ``ant`` is allowed to lag on i686, but if a NEW
    rebuild also misses ppc64le, the drift now extends beyond what
    the operator accepted. Subtracting i686 leaves
    ``{x86_64, aarch64, ppc64le, s390x}`` and they still disagree
    (ppc64le on the old build, the rest on the new), so the test
    fires. If this didn't fire we'd be silencing real cross-arch
    publication skew under the cover of an accepted i686 lag.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        ("AppStream", "x86_64"):  [_pkg("ant", "1.10.9", "15.el9", "a" * 64)],
        ("AppStream", "aarch64"): [_pkg("ant", "1.10.9", "15.el9", "a" * 64)],
        ("AppStream", "s390x"):   [_pkg("ant", "1.10.9", "15.el9", "a" * 64)],
        # ppc64le drifted — NOT in the allowed set.
        ("AppStream", "ppc64le"): [_pkg("ant", "1.10.9", "13.el9", "c" * 64)],
        # i686 drifted — IS in the allowed set.
        ("AppStream", "i686"):    [_pkg("ant", "1.10.9", "11.el9_5", "b" * 64)],
    }
    _a, b = compute_noarch_parity_failures(
        cells, known_drift_b={("AppStream", "ant"): frozenset({"i686"})}
    )
    msg = "\n".join(b)
    assert "AppStream/ant" in msg, (
        "drift wider than the allowed arch set must still fire — "
        "otherwise listing i686 would silently green-light ppc64le drift too"
    )
    # The ppc64le NVRA shows up on its own arch-cohort line.
    assert "1.10.9-13.el9" in msg, msg


def test_noarch_parity_known_drift_b_allowed_arches_must_include_every_drifting_arch():
    """Symmetric to the previous test, expressed as a unit case: the
    YAML lists ``[i686]`` but the actual drift is between three
    cohorts (main / i686 / ppc64le). Suppression requires every
    drifting arch to be in the allowed set; partial coverage is not
    enough. Pins the "subtract-and-recheck" semantic rather than a
    looser "any allowed arch present" rule.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        ("AppStream", "x86_64"):  [_pkg("foo", "2.0", "1.el9", "a" * 64)],
        ("AppStream", "aarch64"): [_pkg("foo", "2.0", "1.el9", "a" * 64)],
        ("AppStream", "ppc64le"): [_pkg("foo", "1.5", "1.el9", "c" * 64)],  # new drift
        ("AppStream", "i686"):    [_pkg("foo", "1.0", "1.el9_5", "b" * 64)],
    }
    _a, b = compute_noarch_parity_failures(
        cells, known_drift_b={("AppStream", "foo"): frozenset({"i686"})}
    )
    assert b, (
        "ppc64le drift on top of the allowed i686 drift must still fire"
    )


def test_noarch_parity_known_drift_b_suppresses_when_every_drifting_arch_is_allowed():
    """Multi-arch acceptance: the YAML lists both i686 and ppc64le as
    allowed laggards; both actually drift; the four other arches
    agree. After subtracting both allowed arches the remaining
    arches are in parity, so the test silences. This validates that
    the allowed set can carry more than one arch when needed (a
    future scenario; today only i686 is listed in the shipped YAML).
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        ("AppStream", "x86_64"):  [_pkg("foo", "2.0", "1.el9", "a" * 64)],
        ("AppStream", "aarch64"): [_pkg("foo", "2.0", "1.el9", "a" * 64)],
        ("AppStream", "s390x"):   [_pkg("foo", "2.0", "1.el9", "a" * 64)],
        ("AppStream", "ppc64le"): [_pkg("foo", "1.5", "1.el9", "c" * 64)],
        ("AppStream", "i686"):    [_pkg("foo", "1.0", "1.el9_5", "b" * 64)],
    }
    _a, b = compute_noarch_parity_failures(
        cells,
        known_drift_b={("AppStream", "foo"): frozenset({"i686", "ppc64le"})},
    )
    assert b == [], (
        "when every drifting arch is in the allowed set the test "
        "must silence — that's the multi-arch acceptance case"
    )


def test_noarch_parity_known_drift_b_does_not_widen_to_other_repos():
    """Suppression is keyed by (repo, name) exactly. Listing
    ``AppStream/cockpit-doc`` must NOT silence the same drift in
    ``BaseOS/cockpit-doc`` — those are independent release channels.
    Without this property a single YAML entry could mask drift in any
    repo, defeating the per-repo scope the parity test enforces.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    # Same name drifts in BOTH repos. Allowlist only the AppStream half.
    cells = {
        ("AppStream", "x86_64"): [_pkg("cockpit-doc", "356", "1.el9", "a" * 64)],
        ("AppStream", "i686"):   [_pkg("cockpit-doc", "311.2", "1.el9_4", "b" * 64)],
        ("BaseOS", "x86_64"):    [_pkg("cockpit-doc", "356", "1.el9", "c" * 64)],
        ("BaseOS", "i686"):      [_pkg("cockpit-doc", "311.2", "1.el9_4", "d" * 64)],
    }
    _a, b = compute_noarch_parity_failures(
        cells,
        known_drift_b={("AppStream", "cockpit-doc"): frozenset({"i686"})},
    )
    msg = "\n".join(b)
    # BaseOS half survives; AppStream half silenced. The new
    # single-package format places the surviving pair in a
    # ``BaseOS/cockpit-doc`` header line.
    assert "BaseOS/cockpit-doc" in msg, msg
    assert "AppStream/cockpit-doc" not in msg, msg


def test_noarch_parity_known_drift_b_does_not_affect_invariant_a():
    """(A) — same NEVRA carries different sha256 across arches — must
    keep firing on listed names regardless of the allowed-arches set.
    This is the load-bearing safety net: if a mirror serves a
    tampered/re-published noarch under a NEVRA we've allow-listed for
    (B), the (A) signal is the only thing that catches it. Silencing
    (A) by accident would turn the allowlist into a security
    regression vector.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        ("AppStream", "x86_64"): [_pkg("ant", "1.10.9", "15.el9", "a" * 64)],
        ("AppStream", "i686"):   [_pkg("ant", "1.10.9", "15.el9", "b" * 64)],
    }
    a, _b = compute_noarch_parity_failures(
        cells, known_drift_b={("AppStream", "ant"): frozenset({"i686"})}
    )
    assert a, "(A) must still fire on same-NEVRA / different-sha even for allowlisted names"
    assert "ant-1.10.9-15.el9.noarch" in "\n".join(a)


def test_noarch_parity_known_drift_b_listing_clean_pair_is_noop():
    """Listing a (repo, name) that is NOT actually drifting does
    nothing — no false pass, no spurious headline. The suppression
    runs AFTER parity is computed, so the allowlist can only mask
    real failures, never invent passing tests. Editing the YAML
    therefore can't accidentally green a test by typo.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    # foo is fully in parity. Listing it should change nothing.
    pkg = _pkg("foo", "1.0", "1.el10", "a" * 64)
    cells = {
        ("BaseOS", "x86_64"):  [pkg],
        ("BaseOS", "aarch64"): [pkg],
    }
    a, b = compute_noarch_parity_failures(
        cells, known_drift_b={("BaseOS", "foo"): frozenset({"i686"})}
    )
    assert a == [] and b == []


def test_noarch_parity_known_drift_b_reports_suppression_count_in_headline():
    """When (B) has BOTH residual real failures AND silenced ones, the
    headline must surface the suppression count so a reviewer can tell
    a regression ("0 known + 1 real") from churn within the allowlist
    ("60 known + 1 new ant subpackage"). Without this signal the
    "n of N failed" line under-reports the work the allowlist did.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        # Two binaries in the same (repo, arch) cell:
        #   ``ant`` — listed in the allowlist for i686, expected silenced.
        #   ``newpkg`` — NOT listed, expected to fire as a fresh drift.
        ("AppStream", "x86_64"): [
            _pkg("ant", "1.10.9", "15.el9", "a" * 64),
            _pkg("newpkg", "2.0", "1.el9", "c" * 64),
        ],
        ("AppStream", "i686"): [
            _pkg("ant", "1.10.9", "11.el9_5", "b" * 64),
            _pkg("newpkg", "1.0", "1.el9_5", "d" * 64),
        ],
    }
    _a, b = compute_noarch_parity_failures(
        cells, known_drift_b={("AppStream", "ant"): frozenset({"i686"})}
    )
    msg = "\n".join(b)
    # Headline mentions the suppression count.
    assert "suppressed by known-drift allowlist" in msg, msg
    assert "1 suppressed" in msg, msg
    # The real failure (newpkg) is still surfaced; ant is not.
    assert "AppStream/newpkg" in msg, msg
    assert "AppStream/ant" not in msg, msg


def test_noarch_parity_known_drift_b_default_none_means_no_suppression():
    """The ``known_drift_b`` parameter defaults to ``None`` so test
    cases that don't care about the allowlist (the majority of
    meta-tests) keep working unchanged. If the default ever
    accidentally became ``load_noarch_parity_known_drift()``, the
    unit tests would silently inherit the YAML and a typo in the
    test data could land on a real allow-listed pair without anyone
    noticing.
    """
    from tests.release.test_noarch_parity import compute_noarch_parity_failures

    cells = {
        ("AppStream", "x86_64"): [_pkg("ant", "1.10.9", "15.el9", "a" * 64)],
        ("AppStream", "i686"):   [_pkg("ant", "1.10.9", "11.el9_5", "b" * 64)],
    }
    # No known_drift_b passed → default (None) → (B) must fire even on
    # the name AppStream/ant which is in the shipped YAML.
    _a, b = compute_noarch_parity_failures(cells)
    assert b, "default known_drift_b must be empty; (B) must fire on ant"
    assert "AppStream/ant" in "\n".join(b)


# ---- known-drift loader contract ----------------------------------------


def test_load_noarch_parity_known_drift_parses_yaml_into_pair_arch_map():
    """The shipped YAML loads into a non-empty
    ``dict[(repo, name), frozenset[arches]]`` and covers the four
    repos in scope (AppStream, BaseOS, CRB, extras). Pins the
    loader+schema together so an accidental rename of the top-level
    key or a switch in shape is caught here, not deep in a release
    run.
    """
    from post_check.config import load_noarch_parity_known_drift

    pairs = load_noarch_parity_known_drift()
    assert isinstance(pairs, dict)
    assert pairs, "shipped YAML is non-empty"
    # Every entry is a (repo, name) -> non-empty frozenset[str] mapping.
    for (repo, name), arches in pairs.items():
        assert isinstance(repo, str) and repo
        assert isinstance(name, str) and name
        assert isinstance(arches, frozenset) and arches, (
            f"{(repo, name)!r}: arches must be a non-empty frozenset, "
            f"got {arches!r}"
        )
        for a in arches:
            assert isinstance(a, str) and a
    # Spot-check the four repos we know to be present from the
    # user-reported drift list — and that the arch is i686 as
    # documented in the YAML.
    assert pairs[("AppStream", "ant")] == frozenset({"i686"})
    assert pairs[("BaseOS", "cockpit-doc")] == frozenset({"i686"})
    assert pairs[("CRB", "xmvn-core")] == frozenset({"i686"})
    assert pairs[("extras", "centos-release-messaging")] == frozenset({"i686"})


def test_load_noarch_parity_known_drift_rejects_bad_shape(tmp_path, monkeypatch):
    """A malformed YAML must raise at load time, not silently produce
    an empty dict. An empty dict would silently re-enable every
    previously-accepted drift on the next release run — the operator
    would see a wall of failures and might miss that the cause is
    "the YAML stopped parsing", not "60 new regressions".
    """
    import post_check.config as cfg

    monkeypatch.setattr(cfg, "ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "noarch_parity_known_drift.yaml").write_text(
        "known_drift: 'not a mapping at all'\n"
    )
    with pytest.raises(ValueError, match="must be a mapping"):
        cfg.load_noarch_parity_known_drift()


def test_load_noarch_parity_known_drift_rejects_old_list_shape(tmp_path, monkeypatch):
    """The previous shipped shape was ``repo: [name, name, ...]`` with
    no arches per entry. That shape is no longer supported — every
    entry must spell out the arches whose drift is accepted. The
    loader must reject the old list-of-strings form with a clear
    error rather than silently treating it as "any arch", which
    would re-introduce the very pre-arch-scoped bug this rewrite
    fixes.
    """
    import post_check.config as cfg

    monkeypatch.setattr(cfg, "ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "noarch_parity_known_drift.yaml").write_text(
        "known_drift:\n"
        "  AppStream:\n"
        "    - ant\n"
        "    - slf4j\n"
    )
    with pytest.raises(ValueError, match="must be a mapping"):
        cfg.load_noarch_parity_known_drift()


def test_load_noarch_parity_known_drift_rejects_empty_arches_list(tmp_path, monkeypatch):
    """An empty arches list (``name: []``) is a schema error, not
    "no arch allowed" — there's no useful semantic for it, and
    accepting it would let an operator add an entry that suppresses
    nothing while pretending to. Raising at load time forces them
    to either list the arch(es) or remove the entry.
    """
    import post_check.config as cfg

    monkeypatch.setattr(cfg, "ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "noarch_parity_known_drift.yaml").write_text(
        "known_drift:\n"
        "  AppStream:\n"
        "    ant: []\n"
    )
    with pytest.raises(ValueError, match="non-empty list of arches"):
        cfg.load_noarch_parity_known_drift()


def test_load_noarch_parity_known_drift_missing_file_returns_empty(tmp_path, monkeypatch):
    """When the YAML file is absent the loader returns an empty dict
    rather than raising — keeps the parity test functional in
    environments where no drift has been accepted yet (e.g. a fresh
    AL10 GA where the parity matrix is clean).
    """
    import post_check.config as cfg

    # Point ROOT at a directory that has no config/ subtree.
    monkeypatch.setattr(cfg, "ROOT", tmp_path)
    assert cfg.load_noarch_parity_known_drift() == {}
