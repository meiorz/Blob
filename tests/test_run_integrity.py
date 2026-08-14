"""scripts/verify_run_integrity.py — the pre-analysis coherence check.

`analyze_results.py` asks "what do these cells say?". This asks the prior
question: "are these cells a valid population to say anything about?"

The digest logic is tested closely because it is the one check that must
tolerate a documented historical quirk without going blind to a real conflict:
run 20260811T072000Z-g5fix recorded a 32-char PREFIX of dataset_sha256, and
docs/benchmark-methodology.md confirms each prefix matches the full digest.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import verify_run_integrity as vri  # noqa: E402

FULL = "fa134fe101e68324e0de851146fda69624f5cbb707d387141d1c2a88a219a16d"
PREFIX = FULL[:32]
OTHER = "0" * 64


def test_single_digest_is_fine():
    assert vri.digest_conflict({FULL}) == (None, None)


def test_empty_is_fine():
    assert vri.digest_conflict(set()) == (None, None)
    assert vri.digest_conflict({None}) == (None, None)


def test_matching_prefix_is_not_a_conflict():
    """The pre-correction case. Failing here would make the checker unusable on
    every run that touches an Iteration 1 cell."""
    conflict, note = vri.digest_conflict({FULL, PREFIX})
    assert conflict is None
    assert note is not None and "pre-correction" in note


def test_non_matching_digest_is_a_conflict():
    """The case the prefix tolerance must not swallow: genuinely different bytes
    recorded under one dataset_id."""
    conflict, note = vri.digest_conflict({FULL, OTHER})
    assert conflict is not None
    assert "different datasets" in conflict


def test_non_matching_short_digest_is_still_a_conflict():
    """A 32-char value that is NOT a prefix of the full digest is a real
    conflict, not a pre-correction artifact."""
    conflict, _ = vri.digest_conflict({FULL, "b" * 32})
    assert conflict is not None


def test_display_tolerates_paths_outside_the_repo():
    """Regression: relative_to() raised ValueError on a relative --manifest."""
    from pathlib import Path
    assert vri.display(Path("results/latest_manifest.json"))
    assert vri.display(Path(r"C:\somewhere\else\manifest.json"))


def test_it_uses_the_shared_guards_rather_than_its_own_copies():
    """The host and memory rules must be the SAME objects the analyser and the
    writers use; a second copy would drift."""
    from benchmarks.env_capture import mixed_host_report
    from benchmarks.memory_profiler import cell_memory_problems
    assert vri.mixed_host_report is mixed_host_report
    assert vri.cell_memory_problems is cell_memory_problems
