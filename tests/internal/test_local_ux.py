import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
RUN_LOCAL = ROOT / "scripts" / "run-local.sh"


def test_run_local_sh_fails_when_alma_version_unset():
    env = {"PATH": os.environ["PATH"]}
    res = subprocess.run([str(RUN_LOCAL)], env=env, capture_output=True, text=True)
    assert res.returncode != 0
    assert "Usage" in res.stderr


def test_run_local_sh_passes_through_pytest_args(tmp_path):
    """Stub pytest with echo to verify argument pass-through.

    We explicitly clear ``PYTEST`` from the subprocess env so that the
    stub on ``PATH`` is what gets executed. The Makefile sets
    ``PYTEST=.venv/bin/pytest`` (absolute path) and exports it; without
    clearing here, run-local.sh would exec the venv pytest directly and
    bypass the stub.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pytest_stub = fake_bin / "pytest"
    pytest_stub.write_text("#!/bin/sh\necho ARGS: \"$@\"")
    pytest_stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
           "ALMA_SOURCE": "stable", "ALMA_VERSION": "10.1", "ALMA_ARCHES": "x86_64",
           "PYTEST": "pytest"}
    res = subprocess.run([str(RUN_LOCAL), "-k", "foo", "-vv"], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "-k foo" in res.stdout
    assert "-vv" in res.stdout


def test_makefile_targets_exist():
    res = subprocess.run(
        ["make", "-n", "-C", str(ROOT), "test-release-host"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0


def test_makefile_test_release_arch_unknown_arch_fails_gracefully():
    # Set ALMA_SOURCE/VERSION so the Makefile's ``_require-vars`` guard
    # doesn't bail out before reaching the arch validation. We're testing
    # the *arch* error path, not the env-var guard.
    env = {**os.environ, "ALMA_SOURCE": "stable", "ALMA_VERSION": "10.1"}
    res = subprocess.run(
        ["make", "-C", str(ROOT), "test-release-arch-foo"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "Unknown arch" in res.stderr or "Unknown arch" in res.stdout
