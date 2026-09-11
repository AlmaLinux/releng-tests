"""Online tests: the installation media are actually in place under
``isos/<arch>/`` on ``repo.almalinux.org``. Stable and beta only.

Why this exists next to ``test_iso_checksums.py``: those tests start
from ``CHECKSUM`` and therefore go quiet in exactly the situation an
operator most needs to hear about — nothing published yet. A missing
ISO tree makes every CHECKSUM-driven test *skip*, and the release
report then reads "SKIPPED" where it should read "the ISOs are not
there". Here the directory index is the source of truth, so a missing
or half-uploaded ISO is a failure.

The location deliberately differs from ``test_iso_checksums`` for beta.
Beta yum repos are served only from ``vault.almalinux.org`` (that is
what the mirrorlist hands out, and vault keeps every past beta), which
makes vault the right home for the CHECKSUM trust chain. The ISOs are
additionally published to
``repo.almalinux.org/almalinux/<version>-beta/isos/<arch>/`` — the copy
the beta announcement points users at, and the copy the publish step
populates. See ``RepoURL.public_iso_dir``.

The ``runtime_config``, ``arch`` and ``report_detail`` fixtures are
defined in ``tests/conftest.py``.
"""

from __future__ import annotations

import pytest

from post_check.helpers import dir_listing, http, iso
from post_check.helpers.url_builder import RepoURL

pytestmark = [pytest.mark.online, pytest.mark.release]

# A boot ISO — the smallest flavour AlmaLinux ships — is around 1 GiB on
# every arch. This floor sits an order of magnitude below that on
# purpose: it exists to catch a zero-byte placeholder, an interrupted
# rsync or an error page served with 200, not to police ISO sizes.
_MIN_PLAUSIBLE_ISO_BYTES = 100 * 1024 * 1024


@pytest.fixture(autouse=True)
def _skip_sources_without_public_isos(runtime_config):
    """Only stable and beta have a public ISO tree to be "in place".

    pungi keeps its media inside the per-arch compose host (covered by
    ``test_iso_checksums`` against the compose URL), and pulp publishes
    no ISOs at all — its layered internal-beta is a yum repo only.
    """
    if runtime_config.source not in ("stable", "beta"):
        pytest.skip(
            f"source={runtime_config.source}: no ISOs are published on "
            f"repo.almalinux.org; only stable and beta ship a public "
            f"ISO tree"
        )


def _require_listing(runtime_config, arch):
    """``(session, url, [iso file names])`` for the public ``isos/<arch>/``.

    Every test in this module needs the listing, and a missing ISO tree
    is the whole point of the module — so it is asserted here rather
    than skipped, and each test then fails with the same actionable
    message instead of erroring out inside a fixture.

    The returned URL keeps its trailing slash so callers can append a
    file name directly.
    """
    u = RepoURL.from_config(
        source=runtime_config.source,
        version=runtime_config.version,
        arch=arch,
        # The ISO path has no repo component; the builder requires one.
        repo="BaseOS",
    )
    s = http.session()
    url = u.public_iso_dir() + "/"
    r = s.get(url)
    assert r.status_code == 200, (
        f"ISO directory {url} → HTTP {r.status_code}. For a release under "
        f"validation this means the ISO publish step has not run; for an "
        f"already-superseded minor the media have been pruned from "
        f"repo.almalinux.org (check the version under test)."
    )
    return s, url, dir_listing.files(r.text, suffix=".iso")


def test_iso_dir_is_published(runtime_config, arch, report_detail):
    """``isos/<arch>/`` is published on the public mirror and carries at
    least one ``.iso``.

    The "are the media there at all" gate for a stable/beta release. It
    fails rather than skips on purpose: every other ISO check in the
    suite is driven by ``CHECKSUM`` and reports a skip when the tree is
    absent, which reads as "not applicable" in the release report when
    it actually means the release is not shippable.
    """
    _, url, isos = _require_listing(runtime_config, arch)
    report_detail(f"GET {url} → 200, {len(isos)} ISO file(s) listed")
    for name in isos:
        report_detail(f"listed: `{name}`")
    assert isos, f"{url} is published but lists no .iso file at all"


def test_iso_dir_has_every_expected_flavour(runtime_config, arch, report_detail):
    """Every expected flavour (``dvd``/``boot``/``minimal``) is present
    as a version-stamped ISO for this arch.

    Matching on the name — ``AlmaLinux-<version>-<arch>-<kind>.iso``,
    or ``AlmaLinux-<version>-beta-<respin>-<arch>-<kind>.iso`` for beta
    — is what ties the media to the release under test: a leftover ISO
    from the previous minor would otherwise satisfy a plain "some dvd
    ISO exists" check.
    """
    _, url, isos = _require_listing(runtime_config, arch)
    beta = runtime_config.source == "beta"
    missing: list[str] = []
    for kind in iso.ISO_KINDS:
        pattern = iso.versioned_iso_re(
            version=runtime_config.version, arch=arch, kind=kind, beta=beta
        )
        matched = [n for n in isos if pattern.match(n)]
        if matched:
            for n in matched:
                report_detail(f"{kind}: `{n}`")
        else:
            missing.append(f"{kind} (expected a name matching {pattern.pattern})")
    assert not missing, (
        f"{url}: no ISO published for flavour(s):\n" + "\n".join(missing)
    )


def test_iso_dir_has_latest_aliases(runtime_config, arch, report_detail):
    """The version-independent ``AlmaLinux-<major>-latest…`` alias exists
    for every flavour.

    These are the names the website and downstream tooling link to, so
    a publish that uploads only the version-stamped ISOs leaves every
    "latest" download link 404ing while the release directory itself
    looks complete.
    """
    _, url, isos = _require_listing(runtime_config, arch)
    beta = runtime_config.source == "beta"
    major = runtime_config.version.split(".")[0]
    missing: list[str] = []
    for kind in iso.ISO_KINDS:
        alias = iso.latest_alias_name(
            major=major, arch=arch, kind=kind, beta=beta
        )
        if alias in isos:
            report_detail(f"alias present: `{alias}`")
        else:
            missing.append(alias)
    assert not missing, (
        f"{url}: missing 'latest' alias(es): {', '.join(missing)}"
    )


def test_published_isos_are_fully_uploaded(runtime_config, arch, report_detail):
    """Every ``.iso`` in ``isos/<arch>/`` is a complete download: HEAD
    returns 200 and the served ``Content-Length`` matches the size the
    release declares for that file in ``CHECKSUM``.

    A 200 on its own is not enough — a truncated or still-syncing ISO
    answers 200 with a short body, and a user only finds out when the
    install media fails to verify. The byte count CHECKSUM carries next
    to each hash is the release's own statement of the expected length,
    so the comparison costs one HEAD per file and no ISO download.

    The CHECKSUM body is read here without verifying its signature:
    the trust chain is ``test_iso_checksums``' job, and what this test
    needs is a cross-check of the mirror against itself, not a trust
    decision. Sizes not declared there still have to clear a 100 MiB
    floor, which is what catches an empty or error-page upload.
    """
    s, url, isos = _require_listing(runtime_config, arch)
    assert isos, f"{url} is published but lists no .iso file at all"

    checksum = s.get(url + "CHECKSUM")
    sizes = (
        iso.declared_sizes(checksum.text) if checksum.status_code == 200 else {}
    )
    if not sizes:
        report_detail(
            f"CHECKSUM declares no sizes (HTTP {checksum.status_code}) — "
            f"floor check only"
        )

    problems: list[str] = []
    for name in isos:
        head = s.head(url + name, allow_redirects=True, timeout=15)
        if head.status_code != 200:
            problems.append(f"{name}: HEAD → {head.status_code}")
            continue
        raw = head.headers.get("Content-Length")
        if not raw or not raw.isdigit():
            problems.append(f"{name}: no usable Content-Length ({raw!r})")
            continue
        served = int(raw)
        declared = sizes.get(name)
        verdict = (
            "size not declared in CHECKSUM"
            if declared is None
            else ("matches CHECKSUM" if declared == served else "MISMATCH")
        )
        report_detail(
            f"HEAD `{name}` → 200, {served / 1024**3:.2f} GiB ({verdict})"
        )
        # The floor applies even when the sizes agree: a CHECKSUM that
        # declares a truncated length would otherwise cross-check
        # happily against the truncated file.
        if served < _MIN_PLAUSIBLE_ISO_BYTES:
            problems.append(
                f"{name}: {served} B is below the "
                f"{_MIN_PLAUSIBLE_ISO_BYTES} B floor for a real ISO"
            )
        elif declared is not None and served != declared:
            problems.append(
                f"{name}: mirror serves {served} B but CHECKSUM declares "
                f"{declared} B ({served - declared:+d} B) — truncated or "
                f"still syncing"
            )
    assert not problems, (
        f"{url}: ISO files not fully published:\n" + "\n".join(problems)
    )
