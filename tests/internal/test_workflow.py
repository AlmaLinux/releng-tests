import yaml
from pathlib import Path

WF = Path(".github/workflows/release-tests.yml")


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


def _load():
    return yaml.load(WF.read_text(), Loader=_NoBoolLoader)


def test_workflow_yaml_is_valid_yaml():
    _load()

def test_workflow_inputs_match_documented_set():
    data = _load()
    inputs = data["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"source", "version", "arches"}

def test_workflow_required_inputs_have_defaults_or_required_true():
    data = _load()
    inputs = data["on"]["workflow_dispatch"]["inputs"]
    assert inputs["version"]["required"] is True

def test_workflow_uses_pinned_action_versions():
    text = WF.read_text()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("uses:") and "@" in line:
            ref = line.split("@", 1)[1].strip()
            assert ref and ref != "main", f"Not pinned: {line}"

def test_workflow_fails_fast_disabled_for_matrix_so_one_arch_failure_doesnt_abort_others():
    data = _load()
    assert data["jobs"]["container-tests"]["strategy"]["fail-fast"] is False

def test_workflow_summary_job_runs_even_if_tests_fail():
    data = _load()
    assert data["jobs"]["summary"]["if"] == "always()"

def test_workflow_has_no_pull_request_target_trigger():
    data = _load()
    assert "pull_request_target" not in data["on"]
