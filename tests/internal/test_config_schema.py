import yaml
from pathlib import Path

import pytest

from post_check.config import compute_prev_version, load_sources

CONFIG = Path(__file__).parent.parent.parent / "config"


def test_sources_registry_has_known_shape():
    """``load_sources()`` returns the hardcoded source registry.

    The registry lives directly in ``post_check/config.py`` (not in
    YAML) because nothing in it varies per-deployment: source names,
    the common repo set and the pungi-specific ``result_dir_*`` are
    facts about the AlmaLinux release process, not settings.

    Mirrorlist URLs are intentionally **not** part of this contract:
    they're authoritative only inside the ``almalinux-repos``
    package's ``.repo`` files (see ``test_mirrorlist``), and copying
    them into a Python constant would just let the two drift.
    """
    sources = load_sources()
    assert {"stable", "beta", "pungi", "pulp"} <= set(sources.keys())
    for name, cfg in sources.items():
        assert isinstance(cfg["repos"], list) and cfg["repos"], (
            f"source={name}: empty repos list"
        )
        assert "mirrorlist" not in cfg, (
            f"source={name}: mirrorlist must not live in the registry "
            "— it comes from the almalinux-repos package's .repo files"
        )
    # pungi-specific fields are present and carry the expected values.
    assert sources["pungi"]["result_dir_default"] == "latest_result_almalinux"
    assert sources["pungi"]["result_dir_kitten"] == "latest_result_almalinux-kitten"
    # pulp-specific: per-major URL templates for the unsigned internal beta.
    pulp_urls = sources["pulp"]["internal_beta_urls"]
    assert {"9", "10"} <= set(pulp_urls.keys()), (
        "pulp must define internal_beta_urls for both AL9 and AL10"
    )
    for major, urls in pulp_urls.items():
        assert {"main", "debug", "source"} <= set(urls.keys()), (
            f"pulp/{major}: must define main+debug+source URL templates"
        )
        assert urls["main"].startswith("https://build.almalinux.org/pulp/content/"), (
            f"pulp/{major}: main URL must point at build.almalinux.org/pulp/content"
        )


def test_sources_registry_repos_include_baseos_and_appstream():
    """The default repo set for every source contains at least
    ``BaseOS`` and ``AppStream`` — without those, testing is pointless.
    Smoke check against accidentally trimming ``_DEFAULT_REPOS``.
    """
    sources = load_sources()
    for name, cfg in sources.items():
        assert "BaseOS" in cfg["repos"], f"{name}: BaseOS missing"
        assert "AppStream" in cfg["repos"], f"{name}: AppStream missing"


def test_sources_registry_returns_independent_lists():
    """Every call to ``load_sources()`` returns its own list objects —
    mutating one source's ``cfg["repos"]`` must not leak into another
    source (or into a subsequent call).
    """
    a = load_sources()
    b = load_sources()
    a["stable"]["repos"].append("INJECTED")
    assert "INJECTED" not in b["stable"]["repos"]
    assert "INJECTED" not in a["beta"]["repos"]


@pytest.mark.parametrize(
    "version, expected",
    [
        ("10.1", "10.0"),
        ("10.2", "10.1"),
        ("10.5", "10.4"),
        ("9.6", "9.5"),
    ],
)
def test_compute_prev_version_decrements_minor(version, expected):
    assert compute_prev_version(version) == expected


@pytest.mark.parametrize(
    "version",
    [
        "10.0",   # first minor — no in-major predecessor
        "9.0",    # ditto for 9
        "10",     # major-only (pungi) — no minor to decrement
        "",       # garbage
        "10.x",   # non-int minor
        "10.1.2", # too many parts
    ],
)
def test_compute_prev_version_returns_none_when_no_predecessor(version):
    assert compute_prev_version(version) is None


def test_pulp_internal_beta_yaml_schema():
    """``config/pulp_internal_beta.yaml`` carries the full per-major
    URL set for the ``pulp`` source's unsigned internal-beta layer.

    Schema invariants (mirror what ``render_pulp_internal_beta_repo_file``
    relies on):

    * AL9 and AL10 are both present (the only majors post-check supports);
    * each major defines ``main``/``debug``/``source``;
    * ``main``/``debug`` carry the ``{arch}`` placeholder so the
      renderer can substitute per-run arch;
    * ``source`` does NOT carry ``{arch}`` (sources are arch-less by
      design — same SRPM serves every arch);
    * every URL points at ``build.almalinux.org/pulp/content/`` —
      this is the contract for "what the pulp source is".

    Locks the YAML's shape against accidental edits that would break
    the renderer at runtime instead of at config-load time.
    """
    data = yaml.safe_load((CONFIG / "pulp_internal_beta.yaml").read_text())
    assert {"9", "10"} <= set(data.keys()), (
        "pulp_internal_beta.yaml must define both AL9 and AL10"
    )
    for major, urls in data.items():
        assert {"main", "debug", "source"} <= set(urls.keys()), (
            f"pulp/{major}: must define main+debug+source"
        )
        for kind in ("main", "debug"):
            assert "{arch}" in urls[kind], (
                f"pulp/{major}/{kind}: URL must carry the {{arch}} placeholder"
            )
        assert "{arch}" not in urls["source"], (
            f"pulp/{major}/source: URL must NOT carry {{arch}} "
            f"(sources are arch-less; one SRPM serves every arch)"
        )
        for kind, url in urls.items():
            assert url.startswith("https://build.almalinux.org/pulp/content/"), (
                f"pulp/{major}/{kind}: URL must point at "
                f"build.almalinux.org/pulp/content/, got: {url}"
            )


def test_architectures_yaml_schema():
    data = yaml.safe_load((CONFIG / "architectures.yaml").read_text())
    assert set(data.keys()) >= {"x86_64", "aarch64", "s390x", "ppc64le"}
    for arch, cfg in data.items():
        assert cfg["docker_platform"].startswith("linux/")
    # pungi_host is set only where the hostname differs from the arch name.
    assert data["x86_64"]["pungi_host"] == "x86-64"
    for arch in ("aarch64", "s390x", "ppc64le"):
        assert "pungi_host" not in data[arch], (
            f"{arch}: pungi_host should not be set — hostname matches the arch name"
        )


def test_architectures_yaml_optional_extras():
    """The optional ``i686`` and ``x86_64_v2`` arches are wired up
    correctly:

    * ``i686`` ships AL9+AL10, has no container/iso/upgrade tests
      (skip_categories) and a vault-only mirrorlist.
    * ``x86_64_v2`` is AL10-only and overrides the container image
      to point at quay.io (the only place v2 images live).

    Locks in the contract that the rest of the suite — the autouse
    skip fixture, ``image_for``, ``test_mirrorlist`` — depends on.
    """
    data = yaml.safe_load((CONFIG / "architectures.yaml").read_text())

    # i686
    i686 = data["i686"]
    assert "9" in i686["supported_majors"] and "10" in i686["supported_majors"]
    assert i686["vault_only_mirrorlist"] is True
    assert i686["stable_repo_base_url"] == "https://vault.almalinux.org", (
        "i686 stable repos are never on repo.almalinux.org — they ship "
        "to vault.almalinux.org with a flatter path layout (no "
        "/almalinux/ prefix). Without this override the URL builder "
        "would 404 every i686 stable URL and parity tests would silently "
        "skip the drift."
    )
    cats = set(i686["skip_categories"])
    assert {"container", "iso"} <= cats, (
        "i686 must skip container and iso tests — no AlmaLinux i686 "
        "container image and no i686 ISOs are published"
    )
    # parity is intentionally NOT skipped: an i686 rebuild that drifts
    # from the upstream matrix (e.g. 49.0-3.el10 on i686 vs 49.0-2.el10
    # everywhere else) is a real release bug we want surfaced.
    assert "parity" not in cats

    # x86_64_v2
    v2 = data["x86_64_v2"]
    assert v2["supported_majors"] == ["10"], (
        "x86_64_v2 was introduced in AL10 — must not be 'supported' on AL9"
    )
    assert v2["docker_platform"] == "linux/amd64/v2"
    assert "{major}" in v2["docker_image"], (
        "docker_image must be a template with a {major} placeholder so "
        "the same field works for any future AL major"
    )
