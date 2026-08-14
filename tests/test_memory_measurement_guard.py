"""Unmeasured memory must FAIL, never silently pass.

Regression tests for a defect that made three decision gates report green on a
host that measured no memory at all. `benchmarks/memory_profiler.py` reads
/proc/self/statm and /proc/self/smaps_rollup with no platform guard; off Linux
every read failed and the metrics were recorded as zeros. Downstream:

    G4  100*(0 - baseline)/baseline, guarded to 0.0 when baseline==0  -> PASS
    G5  short-circuits to 0.0 when baseline_rss_bytes == 0            -> PASS
    G6  no usable scales -> "insufficient_scales" != "superlinear"    -> PASS

Cells carrying those zeros exist in this repo's results/raw. The fix has three
ends and each is tested here: the profiler refuses to run, the writers refuse to
record, and the analyser refuses to score.

These are pure-Python assertions on dicts and monkeypatched paths -- no
benchmark, no timing -- so they are valid on any host and belong in CI.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "benchmarks"))
sys.path.insert(0, ROOT)

import memory_profiler as mp  # noqa: E402


# --------------------------------------------------------------- platform probe

def test_support_probe_agrees_with_the_platform():
    """The probe reads the counter rather than trusting sys.platform alone."""
    ok, reason = mp.process_sampling_support()
    assert ok == os.path.exists(mp._STATM)
    if ok:
        assert reason == ""
    else:
        # An unavailable profiler must say what is missing and what to do.
        assert mp._STATM in reason
        assert "Linux" in reason


def test_unsupported_host_raises_instead_of_returning_zeros(monkeypatch):
    """The whole defect in one assertion: no silent zero."""
    monkeypatch.setattr(mp, "_STATM", "/nonexistent/proc/self/statm")
    ok, reason = mp.process_sampling_support()
    assert ok is False
    with pytest.raises(mp.MemoryProfilerUnavailable) as e:
        mp.require_process_sampling()
    assert "/nonexistent/proc/self/statm" in str(e.value)


def test_profile_memory_refuses_to_start_on_an_unsupported_host(monkeypatch):
    """It must fail at __enter__, before the operation burns a whole cell."""
    monkeypatch.setattr(mp, "_STATM", "/nonexistent/proc/self/statm")
    with pytest.raises(mp.MemoryProfilerUnavailable):
        with mp.profile_memory():
            pass


def test_rss_reader_returns_none_not_zero_on_failure(monkeypatch):
    """None and 0 must stay distinguishable: 0 is a legal RSS, None is 'unknown'."""
    monkeypatch.setattr(mp, "_STATM", "/nonexistent/proc/self/statm")
    assert mp._rss_now() is None


@pytest.mark.skipif(not os.path.exists(mp._STATM),
                    reason="process sampling is Linux-only; nothing to measure here")
def test_supported_host_actually_collects_samples():
    """On Linux the profiler must produce a real measurement, not an empty one."""
    with mp.profile_memory(interval_s=0.001) as pm:
        blob = bytearray(8 * 1024 * 1024)
        del blob
    m = pm.metrics
    assert m.sample_count > 0
    assert m.peak_rss_bytes > 0
    assert m.baseline_rss_bytes > 0
    assert mp.memory_block_problems(m.as_dict()) == []


# ------------------------------------------------- the shared "measured?" rule

def _good_block() -> dict:
    return {"sample_count": 12, "peak_rss_bytes": 700_000_000,
            "baseline_rss_bytes": 500_000_000, "post_run_rss_bytes": 510_000_000}


def test_good_block_has_no_problems():
    assert mp.memory_block_problems(_good_block()) == []


@pytest.mark.parametrize("field", ["sample_count", "peak_rss_bytes", "baseline_rss_bytes"])
def test_each_zeroed_field_is_reported(field):
    block = _good_block()
    block[field] = 0
    problems = mp.memory_block_problems(block)
    assert problems, f"zero {field} was accepted as a measurement"
    assert any(field in p for p in problems)


def test_missing_block_is_reported():
    assert mp.memory_block_problems(None)


def test_cell_check_covers_every_phase():
    """A cell is only measured if BOTH encode and decode were measured."""
    cell = {"memory_encode": _good_block(), "memory_decode": _good_block()}
    assert mp.cell_memory_problems(cell) == []
    cell["memory_encode"] = dict(_good_block(), sample_count=0)
    problems = mp.cell_memory_problems(cell)
    assert problems and all(p.startswith("memory_encode") for p in problems)


def test_the_exact_shape_recorded_on_a_windows_host_is_rejected():
    """The verbatim memory block a Windows host produced before this guard existed:
    tracemalloc still reports a plausible Python-heap figure while every
    process-level counter is zero. Cells in this shape passed G4/G5/G6."""
    windows_block = {
        "sample_count": 0, "peak_rss_bytes": 0, "baseline_rss_bytes": 0,
        "post_run_rss_bytes": 0, "peak_uss_bytes": 0, "mean_rss_bytes": 0,
        "tracemalloc_peak_bytes": 8249, "memory_pool_backend": "mimalloc",
    }
    assert mp.memory_block_problems(windows_block), (
        "the exact block that produced three false gate PASSes was accepted")
