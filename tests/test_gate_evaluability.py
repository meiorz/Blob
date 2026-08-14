"""G4/G5/G6 must report UNEVALUABLE on unmeasured memory, never PASS.

The companion to tests/test_memory_measurement_guard.py: that file proves the
writer refuses to produce a zeroed cell, this one proves the reader refuses to
score one if it ever sees it. Both ends are needed, because results/raw is
append-only -- cells recorded before the fix are still on disk and will still be
handed to the analyser.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import analyze_results as ar  # noqa: E402

MEASURED = {"sample_count": 20, "peak_rss_bytes": 700_000_000,
            "baseline_rss_bytes": 500_000_000, "post_run_rss_bytes": 505_000_000}
UNMEASURED = {"sample_count": 0, "peak_rss_bytes": 0,
              "baseline_rss_bytes": 0, "post_run_rss_bytes": 0}

MEMORY_GATES = ("G4_peak_rss", "G5_post_run_retention", "G6_growth_class")


def _row(arm: str, mem: dict, *, proj_bytes: int, decode_ms: float) -> dict:
    return {
        "arm": arm,
        "projected_compressed_bytes": proj_bytes,
        "decode_projected_ms": {"median": decode_ms, "cov": 0.02},
        "_raw_decode_projected_ms": [decode_ms] * 30,
        "mem_decode": dict(mem),
        "integrity_lossless": True,
    }


def _gates(mem: dict, growth: dict | None = None) -> dict:
    base = _row("snappy", mem, proj_bytes=4_000_000, decode_ms=16.0)
    cand = _row("zstd-3", mem, proj_bytes=2_500_000, decode_ms=15.5)
    growth = growth or {"slope": 1.02, "class": "linear", "points": 3, "dropped": []}
    return ar.apply_gates(base, cand, growth)


# ------------------------------------------------------------ the regression

@pytest.mark.parametrize("gate", MEMORY_GATES)
def test_memory_gates_do_not_pass_on_unmeasured_memory(gate):
    g = _gates(UNMEASURED)
    assert g[gate]["pass"] is False, f"{gate} PASSED on unmeasured memory"
    assert g[gate].get("unevaluable") is True


def test_unmeasured_memory_blocks_all_pass():
    g = _gates(UNMEASURED)
    assert g["ALL_PASS"] is False
    assert set(g["UNEVALUABLE"]) == set(MEMORY_GATES)


def test_unevaluable_reason_names_the_offending_fields():
    """A reader must be able to tell WHY, not just that it failed."""
    g = _gates(UNMEASURED)
    reason = g["G4_peak_rss"]["reason"]
    assert "sample_count" in reason and "peak_rss_bytes" in reason
    assert "snappy" in reason and "zstd-3" in reason


def test_unevaluable_is_distinguished_from_failure():
    """UNEVALUABLE and FAIL both block, but they are not the same finding: one is
    fixed by changing the codec, the other by changing the host."""
    g = _gates(UNMEASURED)
    assert g["UNEVALUABLE"], "unevaluable gates were not reported separately"
    # G1 is a real, evaluated comparison and must be unaffected.
    assert "unevaluable" not in g["G1_footprint"]


# ------------------------------------------- the gates still work when measured

@pytest.mark.parametrize("gate", MEMORY_GATES)
def test_memory_gates_evaluate_normally_when_measured(gate):
    g = _gates(MEASURED)
    assert g[gate].get("unevaluable") is not True
    assert g[gate]["pass"] is True


def test_measured_but_regressed_still_fails():
    """Guard against over-correcting into 'unevaluable' for genuine failures."""
    base = _row("snappy", MEASURED, proj_bytes=4_000_000, decode_ms=16.0)
    blown = dict(MEASURED, peak_rss_bytes=900_000_000)      # +28.6% vs baseline
    cand = _row("zstd-3", blown, proj_bytes=2_500_000, decode_ms=15.5)
    g = ar.apply_gates(base, cand, {"slope": 1.02, "class": "linear",
                                    "points": 3, "dropped": []})
    assert g["G4_peak_rss"]["pass"] is False
    assert g["G4_peak_rss"].get("unevaluable") is not True


# ------------------------------------------------ G6 specifically: unfitted != OK

@pytest.mark.parametrize("cls", ["insufficient_scales", "undetermined", None])
def test_growth_class_that_could_not_be_fitted_is_unevaluable(cls):
    """'not superlinear' must mean measured-and-not-superlinear, not 'unknown'."""
    g = _gates(MEASURED, growth={"slope": None, "class": cls, "points": 1, "dropped": []})
    assert g["G6_growth_class"]["pass"] is False
    assert g["G6_growth_class"]["unevaluable"] is True


def test_superlinear_growth_still_fails_as_a_measurement():
    g = _gates(MEASURED, growth={"slope": 1.4, "class": "superlinear",
                                 "points": 3, "dropped": []})
    assert g["G6_growth_class"]["pass"] is False
    assert g["G6_growth_class"].get("unevaluable") is not True
