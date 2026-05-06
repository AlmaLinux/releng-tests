"""Meta-tests for ``scripts/run-local.sh`` — the shell entry point.

The script is what ``make test`` and CI (and humans) call, so its
contract matters at least as much as the Python tests' contract:

* without ``ALMA_SOURCE`` it must exit non-zero with a usage message
  (otherwise CI silently runs with bogus defaults);
* without ``ALMA_VERSION`` likewise;
* ``ALMA_ARCHES`` with whitespace (``"x86_64, aarch64"``) is normalized
  before being passed to pytest.

We invoke the script in a subprocess with ``-c true`` (the ``[pytest args]``
slot) so it never actually runs pytest — we only care about argument
parsing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "run-local.sh"


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    """Run the script with a clean env (only the keys provided).

    We do NOT pass through the parent environment — otherwise the
    operator's ALMA_* vars would leak into the subprocess and we'd be
    testing whatever happens to be set, not the script's contract.
    """
    full_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        **env,
    }
    return subprocess.run(
        [str(SCRIPT), *args],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _bash_available() -> bool:
    return shutil.which("bash") is not None


@pytest.fixture(autouse=True)
def _require_bash():
    if not _bash_available():
        pytest.skip("bash not available on this host")
    if not SCRIPT.exists():
        pytest.skip(f"{SCRIPT} not found")


def test_run_local_sh_without_alma_source_exits_nonzero_and_prints_usage():
    proc = _run(env={"ALMA_VERSION": "10.1"})
    assert proc.returncode != 0, f"expected non-zero exit, got {proc.returncode}"
    # Usage on stderr — the script does ``cat >&2 <<EOF Usage: ... EOF``.
    assert "ALMA_SOURCE" in proc.stderr, (
        f"expected usage on stderr, got: {proc.stderr!r}"
    )


def test_run_local_sh_without_alma_version_exits_nonzero_and_prints_usage():
    proc = _run(env={"ALMA_SOURCE": "stable"})
    assert proc.returncode != 0
    assert "ALMA_VERSION" in proc.stderr or "Usage" in proc.stderr


def test_run_local_sh_normalizes_whitespace_in_alma_arches(tmp_path):
    """Replace ``pytest`` with a stub that records argv and the env, run
    the script with whitespace-padded ALMA_ARCHES, then read what was
    seen by the (fake) pytest.

    Implementation: we put a wrapper directory at the front of PATH that
    contains a ``pytest`` shell stub. ``run-local.sh`` ends with
    ``exec pytest "$@"`` — so the stub will be the one running.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "record.txt"
    stub = stub_dir / "pytest"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "ALMA_ARCHES=$ALMA_ARCHES" > "{record}"\n'
        f'echo "argv: $@" >> "{record}"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)

    env = {
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "HOME": os.environ.get("HOME", ""),
        "ALMA_SOURCE": "stable",
        "ALMA_VERSION": "10.1",
        "ALMA_ARCHES": " x86_64 , aarch64 ",
    }
    proc = subprocess.run(
        [str(SCRIPT), "-m", "not requires_docker"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stub pytest failed: {proc.stderr}"
    seen = record.read_text()
    # Spaces around commas must be stripped by ``tr -d ' '``.
    assert "ALMA_ARCHES=x86_64,aarch64" in seen, seen


def test_run_local_sh_passes_through_pytest_args(tmp_path):
    """Ensure positional args (e.g. ``-m "not requires_docker"`` or a test
    path) reach pytest unchanged. If the script accidentally swallowed
    args, the operator's ``-m`` filter would silently turn into a no-op.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "record.txt"
    stub = stub_dir / "pytest"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" > "{record}"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)

    env = {
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "HOME": os.environ.get("HOME", ""),
        "ALMA_SOURCE": "stable",
        "ALMA_VERSION": "10.1",
        "ALMA_ARCHES": "x86_64",
    }
    proc = subprocess.run(
        [str(SCRIPT), "-m", "not requires_docker", "tests/internal/test_url_builder.py"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    lines = record.read_text().splitlines()
    assert lines == ["-m", "not requires_docker", "tests/internal/test_url_builder.py"], lines
