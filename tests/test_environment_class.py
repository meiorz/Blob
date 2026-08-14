"""COMPRESSION_BENCH_ENV must fail closed, and the register must not shrink.

Two regressions, both of which made a run claim more than it measured.

1. `hardware_validated` was `ENVIRONMENT_CLASS not in ("sandbox",)`, so every value
   that was not exactly "sandbox" asserted hardware validation -- including an
   exported-but-empty variable, wrong case, and a typo. That is the strongest
   claim this project makes, reachable by a slip of the keyboard, and it lands in
   append-only recorded evidence.

2. The NOT-MEASURED register was conditional on host capability throughout, so on
   the 4-8 core node Milestone 1.2a specifies, the concurrency caveat disappeared
   while run_cell.py went on hardcoding pa.set_cpu_count(1). A register that
   shrinks on a bigger machine reads as "we measured more this time".
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "benchmarks"))

import env_capture as ec  # noqa: E402


# --------------------------------------------------------------- the allowlist

@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_unset_or_empty_means_sandbox(raw):
    """An operator who forgets the variable, or exports it empty, gets the
    conservative default -- never a validated-looking result."""
    assert ec.resolve_environment_class(raw) == "sandbox"


@pytest.mark.parametrize("raw", ["sandbox", "dev-local", "cloud-8x32",
                                 "cloud-m5.2xlarge", "cloud-c6i-4x16", "cloud-a"])
def test_valid_values_are_accepted(raw):
    assert ec.resolve_environment_class(raw) == raw


def test_surrounding_whitespace_is_tolerated():
    assert ec.resolve_environment_class("  cloud-8x32  ") == "cloud-8x32"


@pytest.mark.parametrize("raw", [
    "Sandbox", "SANDBOX", "sanbox", "sandox", "prod", "production",
    "Dev-Local", "devlocal", "dev_local",
    "cloud", "cloud-", "Cloud-8x32", "cloud-8X32", "cloud- 8x32", "cloud-!",
    "true", "1", "yes",
])
def test_every_unrecognised_value_raises(raw):
    """The old code turned each of these into hardware_validated=true."""
    with pytest.raises(ec.InvalidEnvironmentClass):
        ec.resolve_environment_class(raw)


def test_the_error_lists_the_valid_values():
    """An operator who mistypes must be told what to type instead."""
    with pytest.raises(ec.InvalidEnvironmentClass) as e:
        ec.resolve_environment_class("sanbox")
    msg = str(e.value)
    assert "sanbox" in msg                      # what they typed
    assert "'sandbox'" in msg and "'dev-local'" in msg and "cloud-" in msg
    assert "hardware_validated" in msg          # why it matters


def test_hardware_validated_is_derived_from_the_validated_class():
    assert ec.HARDWARE_VALIDATED == (ec.ENVIRONMENT_CLASS != "sandbox")
    assert ec.resolve_environment_class(None) == ec.ENVIRONMENT_CLASS_SANDBOX


def test_no_unrecognised_value_can_reach_a_cell():
    """The property that matters: there is no input for which resolution
    succeeds AND the value is outside the allowlist."""
    for raw in ["Sandbox", "sanbox", "prod", "cloud-", "", None, "cloud-8x32"]:
        try:
            resolved = ec.resolve_environment_class(raw)
        except ec.InvalidEnvironmentClass:
            continue
        assert (resolved in ec.ENVIRONMENT_CLASS_LITERALS
                or ec.CLOUD_CLASS_RE.match(resolved)), resolved


# ------------------------------------------------------- the register cannot shrink

def _register(capture: dict) -> dict:
    return {e["id"]: e for e in capture["not_measured"]}


HARNESS_DESIGN_ENTRIES = ("concurrency_scaling", "cold_cache")


def test_harness_design_entries_are_present_on_this_host():
    reg = _register(ec.capture())
    for entry_id in HARNESS_DESIGN_ENTRIES:
        assert entry_id in reg, f"{entry_id} missing from the register"
        assert reg[entry_id]["cause"] == "harness design"
        assert reg[entry_id]["retires_on_better_hardware"] is False


def test_harness_design_entries_survive_a_16_core_root_capable_host(monkeypatch):
    """The regression. Simulate the node Milestone 1.2a specifies: many physical
    cores, no SMT, plenty of RAM, and writable drop_caches. Both entries must
    still be there, because run_cell.py still hardcodes pa.set_cpu_count(1) and
    codec benchmarks still run on in-memory buffers."""
    monkeypatch.setattr(ec, "_physical_cores", lambda: 16)
    monkeypatch.setattr(ec, "_mem_total_bytes", lambda: 64 * 1024 ** 3)
    monkeypatch.setattr(ec.os, "cpu_count", lambda: 16)          # no SMT
    monkeypatch.setattr(ec.os, "access", lambda *a, **k: True)   # root: drop_caches writable

    reg = _register(ec.capture())

    for entry_id in HARNESS_DESIGN_ENTRIES:
        assert entry_id in reg, (
            f"{entry_id} vanished on a large root-capable host. The harness still "
            "does not vary it; a bigger machine must not retire this entry.")
        assert reg[entry_id]["retires_on_better_hardware"] is False

    # And the host-capability entries correctly DO retire on that host.
    assert "absolute_per_worker_memory_budget" not in reg
    assert "concurrency_scaling_host_constraint" not in reg


def test_host_capability_entries_appear_on_a_small_host(monkeypatch):
    """The other direction: host capability may still ADD entries."""
    monkeypatch.setattr(ec, "_physical_cores", lambda: 1)
    monkeypatch.setattr(ec, "_mem_total_bytes", lambda: 3 * 1024 ** 3)
    monkeypatch.setattr(ec.os, "cpu_count", lambda: 2)           # SMT
    reg = _register(ec.capture())
    assert reg["absolute_per_worker_memory_budget"]["cause"] == "host capability"
    assert reg["absolute_per_worker_memory_budget"]["retires_on_better_hardware"] is True
    assert reg["concurrency_scaling_host_constraint"]["cause"] == "host capability"
    # ...without displacing the unconditional ones.
    for entry_id in HARNESS_DESIGN_ENTRIES:
        assert entry_id in reg


def test_every_entry_declares_a_cause_and_retirement():
    for entry in ec.capture()["not_measured"]:
        assert entry["cause"] in ("harness design", "host capability"), entry
        assert isinstance(entry["retires_on_better_hardware"], bool), entry
        assert entry["detail"].strip(), entry


def test_only_two_entries_ever_claim_to_retire(monkeypatch):
    """Exactly absolute_per_worker_memory_budget and process_memory_profiling
    describe a limit a better machine removes; concurrency_scaling_host_constraint
    narrows a harness-design entry rather than replacing it."""
    monkeypatch.setattr(ec, "_physical_cores", lambda: 1)
    monkeypatch.setattr(ec, "_mem_total_bytes", lambda: 3 * 1024 ** 3)
    retiring = {e["id"] for e in ec.capture()["not_measured"]
                if e["retires_on_better_hardware"]}
    assert retiring <= {"absolute_per_worker_memory_budget",
                        "process_memory_profiling",
                        "concurrency_scaling_host_constraint"}
    assert not (retiring & set(HARNESS_DESIGN_ENTRIES))
