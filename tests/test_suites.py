"""pytest entry point for the two standalone suites.

The suites signal pass/fail by exit code, not by raising. This wrapper is the only
pytest-visible surface; see tests/conftest.py for why the suites themselves are excluded
from collection.

    pytest tests/                          # both suites
    COMPRESSION_BENCH_SKIP_HEAVY=1 pytest  # skip the 1 GiB decompression-bomb suite
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")

SKIP_HEAVY = os.environ.get("COMPRESSION_BENCH_SKIP_HEAVY") == "1"


def _run_suite(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, os.path.join(HERE, script)],
        capture_output=True, text=True, timeout=1800,
    )


def _assert_suite(script: str, artifact: str) -> None:
    proc = _run_suite(script)
    # Surface the suite's own output; a bare "exit 1" is not actionable.
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)

    path = os.path.join(RESULTS, artifact)
    detail = ""
    if os.path.exists(path):
        with open(path) as fh:
            data = json.load(fh)
        failed = [r["test"] for r in data.get("results", []) if not r.get("pass")]
        detail = f"; failed: {failed}" if failed else ""
        # The artifact and the exit code must agree. If they disagree the harness itself
        # is untrustworthy, which is a failure regardless of which one says "pass".
        assert data.get("all_pass") == (proc.returncode == 0), (
            f"{script}: {artifact} all_pass={data.get('all_pass')} disagrees with "
            f"exit code {proc.returncode}"
        )
    else:
        detail = f"; {artifact} was not written"

    assert proc.returncode == 0, f"{script} exited {proc.returncode}{detail}"


def test_correctness_suite() -> None:
    _assert_suite("test_correctness.py", "correctness_results.json")


@pytest.mark.skipif(
    SKIP_HEAVY,
    reason="COMPRESSION_BENCH_SKIP_HEAVY=1 — suite allocates a 1 GiB buffer to build the "
           "decompression bomb. Skipping means the security gate is NOT evaluated.",
)
def test_hostile_input_suite() -> None:
    _assert_suite("test_hostile_inputs.py", "security_results.json")
