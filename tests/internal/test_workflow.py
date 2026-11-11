import yaml
from pathlib import Path

WF = Path(".github/workflows/release-tests.yml")
CI = Path(".github/workflows/ci.yml")

# Contracts that hold for every workflow in the repo, not just the
# release one. Kept as a list so a new workflow is covered the moment
# it is added rather than whenever someone remembers to extend this
# file — a missed `pull_request_target` or an unpinned action is just
# as dangerous in a lint job as in a release job.
ALL_WORKFLOWS = sorted(Path(".github/workflows").glob("*.yml"))


class _NoBoolLoader(yaml.SafeLoader):
    """SafeLoader that doesn't turn on/off/yes/no into bool — otherwise
    the `on:` key in GitHub Actions YAML gets parsed as True."""


# Strip YAML 1.1 bool resolvers for specific strings.
_NoBoolLoader.yaml_implicit_resolvers = {
    k: [(tag, regex) for tag, regex in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
# Keep only true/false as bool — re-register.
_NoBoolLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    yaml.resolver.Resolver.DEFAULT_SCALAR_TAG and __import__("re").compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _load(path=WF):
    return yaml.load(path.read_text(), Loader=_NoBoolLoader)


def test_workflow_yaml_is_valid_yaml():
    for path in ALL_WORKFLOWS:
        assert _load(path), f"{path} parsed to nothing"

def test_workflow_inputs_match_documented_set():
    data = _load()
    inputs = data["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"source", "version", "arches"}

def test_workflow_required_inputs_have_defaults_or_required_true():
    data = _load()
    inputs = data["on"]["workflow_dispatch"]["inputs"]
    assert inputs["version"]["required"] is True

def test_workflow_uses_pinned_action_versions():
    for path in ALL_WORKFLOWS:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("uses:") and "@" in line:
                ref = line.split("@", 1)[1].strip()
                assert ref and ref != "main", f"Not pinned in {path}: {line}"

def test_workflow_fails_fast_disabled_for_matrix_so_one_arch_failure_doesnt_abort_others():
    data = _load()
    assert data["jobs"]["container-tests"]["strategy"]["fail-fast"] is False

def test_workflow_summary_job_runs_even_if_tests_fail():
    data = _load()
    assert data["jobs"]["summary"]["if"] == "always()"

def test_workflow_has_no_pull_request_target_trigger():
    # ``pull_request_target`` runs with the base repo's secrets against
    # the fork's code. Nothing here needs it, and ci.yml runs on
    # ``pull_request`` precisely so a fork's code never gets that.
    for path in ALL_WORKFLOWS:
        assert "pull_request_target" not in _load(path)["on"], path


def test_ci_workflow_gates_on_both_lint_and_internal_tests():
    """CI must actually run the two offline gates, not just exist.

    A workflow that drops one of these silently stops catching the
    class of bug it was added for — an unlinted mypy error reaching
    main is what prompted ci.yml in the first place.
    """
    jobs = _load(CI)["jobs"]
    assert set(jobs) == {"lint", "internal-tests"}
    runs = [
        step.get("run", "")
        for job in jobs.values()
        for step in job["steps"]
    ]
    assert any("make lint" in r for r in runs), "CI does not run `make lint`"
    assert any("make test-internal" in r for r in runs), (
        "CI does not run `make test-internal`"
    )


def test_ci_workflow_stays_offline():
    """CI must not grow an ``ALMA_*`` dependency.

    tests/internal is contractually offline (see CLAUDE.md); wiring a
    release variable into this workflow would be the first step to
    breaking that, and would make the repo's own gate depend on the
    state of a published AlmaLinux release.

    Checks what the workflow *does* — its ``env`` blocks and the
    commands it runs — rather than grepping the raw file, so that a
    comment explaining this very contract doesn't fail the gate.
    """
    data = _load(CI)
    offenders: list[str] = []

    def _check_env(env, where):
        offenders.extend(
            f"{where}: {k}" for k in (env or {}) if k.startswith("ALMA_")
        )

    _check_env(data.get("env"), "workflow env")
    for job_name, job in data["jobs"].items():
        _check_env(job.get("env"), f"job {job_name} env")
        for step in job["steps"]:
            _check_env(step.get("env"), f"job {job_name} step env")
            if "ALMA_" in step.get("run", ""):
                offenders.append(f"job {job_name} run: {step['run'].strip()}")

    assert not offenders, "CI workflow references release variables: " + "; ".join(
        offenders
    )
