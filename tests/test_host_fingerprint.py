"""Cells from different machines must not be analysed together.

`environment_class` is a LABEL, not a machine identity: `env_capture.py` defaults
it to "sandbox" whenever COMPRESSION_BENCH_ENV is unset, so a Windows workstation
and the Linux benchmark container both record "sandbox" and a guard keyed on that
label alone passes the mix through.

That is not hypothetical. This repo's results/raw currently holds cells measured
on Linux-6.8 and on Windows-10 that both say environment_class="sandbox", whose
encode medians differ by more than 120%. SKILL.md: "Never compare results
collected with different datasets, different machines, materially different
concurrency, or inconsistent cache states."
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "benchmarks"))

import env_capture as ec  # noqa: E402

LINUX = {"environment_class": "sandbox",
         "os": "Linux-6.8.0-124-generic-x86_64-with-glibc2.35",
         "cpu_model": "AMD EPYC 7B13", "mem_total_bytes": 4_045_000_000}
WINDOWS = {"environment_class": "sandbox",
           "os": "Windows-10-10.0.26200-SP0",
           "cpu_model": "Intel64 Family 6", "mem_total_bytes": 0}


def test_same_host_is_accepted():
    envs = {"D1|S|snappy": dict(LINUX), "D1|S|zstd-3": dict(LINUX)}
    assert ec.mixed_host_report(envs) is None


def test_empty_and_single_cell_are_accepted():
    assert ec.mixed_host_report({}) is None
    assert ec.mixed_host_report({"D1|S|snappy": dict(LINUX)}) is None


def test_different_hosts_are_refused_despite_identical_environment_class():
    """The regression: the old guard compared only environment_class."""
    assert LINUX["environment_class"] == WINDOWS["environment_class"] == "sandbox"
    envs = {"D1|S|snappy": dict(LINUX), "D1|S|zstd-3": dict(WINDOWS)}
    report = ec.mixed_host_report(envs)
    assert report is not None, "a Linux/Windows mix passed the guard"
    assert "2 DIFFERENT HOSTS" in report
    assert "Linux-6.8.0-124-generic-x86_64-with-glibc2.35" in report
    assert "Windows-10-10.0.26200-SP0" in report
    # It must name the offending cells, not just say "mixed".
    assert "D1|S|snappy" in report and "D1|S|zstd-3" in report


def test_each_fingerprint_field_is_load_bearing():
    """os, cpu_model and mem_total_bytes each independently identify the machine:
    a RAM upgrade or a CPU swap is still a different machine for benchmarking."""
    for field, changed in (("os", "Linux-6.9.0-generic"),
                           ("cpu_model", "Intel Xeon Platinum 8375C"),
                           ("mem_total_bytes", 8_090_000_000)):
        other = dict(LINUX)
        other[field] = changed
        envs = {"a": dict(LINUX), "b": other}
        assert ec.mixed_host_report(envs) is not None, (
            f"differing {field} was not treated as a different host")


def test_fingerprint_ignores_fields_that_vary_within_one_host():
    """hostname and cpu_affinity legitimately differ between cells on one machine
    (container restarts, per-cell pinning); they must not trip the guard."""
    a = dict(LINUX, hostname="runner-1", cpu_affinity=[0])
    b = dict(LINUX, hostname="runner-2", cpu_affinity=[1])
    assert ec.mixed_host_report({"a": a, "b": b}) is None


def test_missing_env_block_is_its_own_fingerprint():
    """A cell with no env cannot be assumed to share a host with one that has it."""
    assert ec.mixed_host_report({"a": dict(LINUX), "b": None}) is not None


def test_report_explains_the_rule():
    report = ec.mixed_host_report({"a": dict(LINUX), "b": dict(WINDOWS)})
    assert "different machines" in report
    assert "environment_class" in report
