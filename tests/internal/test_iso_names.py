"""Unit tests for the ISO file-name contract in ``post_check.helpers.iso``.

Offline, no GPG. These pin the names ``test_iso_presence`` demands from
a release: if the naming helpers drift from what AlmaLinux actually
publishes, that test would either fail on a healthy release or (worse)
accept a directory that is missing media.
"""

from __future__ import annotations

from post_check.helpers.iso import ISO_KINDS, latest_alias_name, versioned_iso_re


def test_iso_kinds_are_the_three_published_flavours():
    """The flavour set is a release contract, not a preference — pin it
    so a silent edit of the helper cannot shrink what a release is
    required to publish.
    """
    assert ISO_KINDS == ("dvd", "boot", "minimal")


def test_latest_alias_name_for_stable():
    assert (
        latest_alias_name(major="10", arch="aarch64", kind="dvd", beta=False)
        == "AlmaLinux-10-latest-aarch64-dvd.iso"
    )


def test_latest_alias_name_for_beta_carries_the_beta_segment():
    """The beta alias is a different file name, not the stable one in a
    ``-beta`` directory — ``AlmaLinux-10-latest-beta-…``.
    """
    assert (
        latest_alias_name(major="10", arch="aarch64", kind="dvd", beta=True)
        == "AlmaLinux-10-latest-beta-aarch64-dvd.iso"
    )


def test_versioned_iso_re_matches_stable_name():
    pattern = versioned_iso_re(
        version="10.2", arch="x86_64_v2", kind="minimal", beta=False
    )
    assert pattern.match("AlmaLinux-10.2-x86_64_v2-minimal.iso")


def test_versioned_iso_re_matches_any_beta_respin():
    """The respin counter is unknowable from ``ALMA_VERSION``: a
    re-rolled beta publishes ``-beta-2``. Both must match, or the
    presence test would fail on a legitimately re-rolled beta.
    """
    pattern = versioned_iso_re(
        version="10.3", arch="aarch64", kind="dvd", beta=True
    )
    assert pattern.match("AlmaLinux-10.3-beta-1-aarch64-dvd.iso")
    assert pattern.match("AlmaLinux-10.3-beta-2-aarch64-dvd.iso")


def test_versioned_iso_re_requires_the_respin_counter_on_beta():
    """A beta ISO without the counter is not the file the release notes
    link to — it must not satisfy the flavour check."""
    pattern = versioned_iso_re(
        version="10.3", arch="aarch64", kind="dvd", beta=True
    )
    assert not pattern.match("AlmaLinux-10.3-beta-aarch64-dvd.iso")


def test_versioned_iso_re_rejects_other_versions_and_arches():
    """Guards the case the pattern exists for: a leftover ISO from the
    previous minor (or another arch) sitting in the directory must not
    read as "this release's media are in place".
    """
    pattern = versioned_iso_re(
        version="10.2", arch="aarch64", kind="dvd", beta=False
    )
    assert not pattern.match("AlmaLinux-10.1-aarch64-dvd.iso")
    assert not pattern.match("AlmaLinux-10.2-x86_64-dvd.iso")
    assert not pattern.match("AlmaLinux-10.2-aarch64-boot.iso")
    # The dot in the version is a literal, not a wildcard.
    assert not pattern.match("AlmaLinux-10-2-aarch64-dvd.iso")


def test_versioned_iso_re_rejects_stable_name_under_beta_and_vice_versa():
    """Beta and stable names are not interchangeable: a beta directory
    holding stable-named ISOs (or the reverse) is a publish bug.
    """
    stable = versioned_iso_re(
        version="10.3", arch="aarch64", kind="dvd", beta=False
    )
    beta = versioned_iso_re(
        version="10.3", arch="aarch64", kind="dvd", beta=True
    )
    assert not stable.match("AlmaLinux-10.3-beta-1-aarch64-dvd.iso")
    assert not beta.match("AlmaLinux-10.3-aarch64-dvd.iso")
