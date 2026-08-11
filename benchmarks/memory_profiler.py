"""Memory instrumentation for compression benchmarks.

Implements the SKILL.md "Memory footprint profiling" requirements:
one process-level metric (smaps_rollup / getrusage) AND one runtime-level
metric (Arrow memory pool + tracemalloc), sampled frequently enough to
catch short-lived spikes rather than relying on start/end snapshots.

Design note: getrusage(ru_maxrss) is a process-lifetime high-water mark and
never decreases, so it is only meaningful when each benchmark cell runs in a
fresh subprocess. run_cell.py enforces that. The sampler thread is the
authoritative per-run peak; ru_maxrss is recorded as a cross-check.
"""
from __future__ import annotations

import gc
import os
import threading
import time
import tracemalloc
from dataclasses import dataclass, field, asdict

try:
    import resource
except ImportError:
    resource = None

_SMAPS = "/proc/self/smaps_rollup"
_STATUS = "/proc/self/status"


def _page_size() -> int:
    sysconf = getattr(os, "sysconf", None)
    if sysconf is None:
        return 4096
    try:
        return int(sysconf("SC_PAGE_SIZE"))
    except Exception:
        return 4096


def _read_smaps_rollup() -> dict[str, int]:
    """Return byte counts from smaps_rollup. USS = Private_Clean + Private_Dirty."""
    out: dict[str, int] = {}
    try:
        with open(_SMAPS, "rb") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[0].endswith(b":"):
                    key = parts[0][:-1].decode()
                    try:
                        out[key] = int(parts[1]) * 1024  # kB -> bytes
                    except ValueError:
                        continue
    except OSError:
        return {}
    out["Uss"] = out.get("Private_Clean", 0) + out.get("Private_Dirty", 0)
    return out


def _rss_now() -> int:
    """Cheap RSS read via statm (pages)."""
    try:
        with open("/proc/self/statm", "rb") as fh:
            return int(fh.read().split()[1]) * _page_size()
    except (OSError, IndexError, ValueError):
        return 0


@dataclass
class MemoryMetrics:
    # process level (bytes)
    peak_rss_bytes: int = 0
    mean_rss_bytes: int = 0
    baseline_rss_bytes: int = 0
    peak_uss_bytes: int = 0
    peak_pss_bytes: int = 0
    peak_anon_bytes: int = 0
    peak_file_backed_bytes: int = 0
    rusage_maxrss_bytes: int = 0
    post_run_rss_bytes: int = 0
    post_run_rss_before_release_bytes: int = 0
    post_run_retained_bytes: int = 0
    arena_retained_bytes: int = 0
    memory_pool_backend: str = ""
    # runtime level (bytes)
    arrow_pool_peak_bytes: int = 0
    arrow_pool_delta_bytes: int = 0
    tracemalloc_peak_bytes: int = 0
    # sampling provenance
    sample_count: int = 0
    sample_interval_ms: float = 0.0
    gc_collections_during_run: int = 0
    gc_ran: bool = False
    samples_rss_bytes: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("samples_rss_bytes", None)  # kept out of the row; series saved separately
        return d


class MemorySampler:
    """Background sampler. Default 5 ms: fast enough to catch transient
    scratch-buffer spikes that a start/end snapshot would miss entirely."""

    def __init__(self, interval_s: float = 0.005, keep_series: bool = True):
        self.interval_s = interval_s
        self.keep_series = keep_series
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peak_rss = 0
        self.sum_rss = 0
        self.n = 0
        self.peak_uss = 0
        self.peak_pss = 0
        self.peak_anon = 0
        self.peak_file = 0
        self.series: list[int] = []

    def _loop(self) -> None:
        # smaps_rollup is ~30x more expensive than statm, so sample RSS hot
        # and the rollup every 8th tick. Peaks are still caught by statm.
        i = 0
        while not self._stop.is_set():
            rss = _rss_now()
            if rss:
                self.peak_rss = max(self.peak_rss, rss)
                self.sum_rss += rss
                self.n += 1
                if self.keep_series and len(self.series) < 200_000:
                    self.series.append(rss)
            if i % 8 == 0:
                roll = _read_smaps_rollup()
                if roll:
                    self.peak_uss = max(self.peak_uss, roll.get("Uss", 0))
                    self.peak_pss = max(self.peak_pss, roll.get("Pss", 0))
                    self.peak_anon = max(self.peak_anon, roll.get("Anonymous", 0))
                    fb = roll.get("Rss", 0) - roll.get("Anonymous", 0)
                    self.peak_file = max(self.peak_file, max(fb, 0))
            i += 1
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


class profile_memory:
    """Context manager producing MemoryMetrics for one operation."""

    def __init__(self, interval_s: float = 0.005, trace_python_heap: bool = True,
                 keep_series: bool = False):
        self.interval_s = interval_s
        self.trace_python_heap = trace_python_heap
        self.keep_series = keep_series
        self.metrics = MemoryMetrics()

    def __enter__(self) -> "profile_memory":
        gc.collect()
        self._gc0 = sum(s["collections"] for s in gc.get_stats())
        self.metrics.baseline_rss_bytes = _rss_now()
        try:
            import pyarrow as pa
            self._pool = pa.default_memory_pool()
            self._arrow0 = self._pool.bytes_allocated()
            # max_memory is cumulative for the pool's lifetime; record the
            # pre-run value and report the delta so it is per-run meaningful.
            self._arrow_max0 = self._pool.max_memory()
        except Exception:
            self._pool = None
            self._arrow0 = self._arrow_max0 = 0
        if self.trace_python_heap:
            tracemalloc.start()
        self._sampler = MemorySampler(self.interval_s, keep_series=self.keep_series)
        self._sampler.start()
        return self

    def __exit__(self, *exc) -> bool:
        self._sampler.stop()
        m = self.metrics
        m.peak_rss_bytes = self._sampler.peak_rss
        m.mean_rss_bytes = int(self._sampler.sum_rss / self._sampler.n) if self._sampler.n else 0
        m.peak_uss_bytes = self._sampler.peak_uss
        m.peak_pss_bytes = self._sampler.peak_pss
        m.peak_anon_bytes = self._sampler.peak_anon
        m.peak_file_backed_bytes = self._sampler.peak_file
        m.sample_count = self._sampler.n
        m.sample_interval_ms = self.interval_s * 1000.0
        if self.keep_series:
            m.samples_rss_bytes = self._sampler.series
        if self.trace_python_heap:
            _, peak = tracemalloc.get_traced_memory()
            m.tracemalloc_peak_bytes = peak
            tracemalloc.stop()
        if self._pool is not None:
            m.arrow_pool_peak_bytes = max(self._pool.max_memory() - self._arrow_max0, 0)
            m.arrow_pool_delta_bytes = self._pool.bytes_allocated() - self._arrow0
        getrusage = getattr(resource, "getrusage", None) if resource is not None else None
        rusageself = getattr(resource, "RUSAGE_SELF", None) if resource is not None else None
        if getrusage is not None and rusageself is not None:
            m.rusage_maxrss_bytes = getrusage(rusageself).ru_maxrss * 1024
        m.gc_collections_during_run = sum(s["collections"] for s in gc.get_stats()) - self._gc0
        gc.collect()
        m.gc_ran = True
        time.sleep(0.05)
        # Arrow's default pool is an arena allocator (jemalloc/mimalloc) that
        # retains freed pages by design. Measuring retention without releasing
        # the arena conflates normal allocator behaviour with a genuine leak,
        # so record BOTH: pre-release (what the OS sees while the pool is warm)
        # and post-release (what is actually unreclaimable).
        m.post_run_rss_before_release_bytes = _rss_now()
        m.memory_pool_backend = ""
        if self._pool is not None:
            try:
                m.memory_pool_backend = self._pool.backend_name
            except Exception:
                pass
            try:
                self._pool.release_unused()
                time.sleep(0.05)
            except Exception:
                pass
        m.post_run_rss_bytes = _rss_now()
        m.arena_retained_bytes = max(
            m.post_run_rss_before_release_bytes - m.post_run_rss_bytes, 0)
        m.post_run_retained_bytes = m.post_run_rss_bytes - m.baseline_rss_bytes
        return False


def derived_memory_metrics(m: MemoryMetrics, original_bytes: int, compressed_bytes: int) -> dict:
    """SKILL.md 'Required memory metrics'.

    UNIT DISCREPANCY (surfaced, not silently corrected):
    SKILL.md defines

        memory_per_input_mb = peak_resident_set_bytes / original_input_bytes * 1048576

    peak/original is dimensionless, so multiplying by 1048576 yields
    *bytes of RSS per MiB of input*, not MiB. The `_mb` suffix is therefore a
    misnomer in the spec. We emit the spec formula verbatim under the spec's
    own name so results remain comparable to anything else built from SKILL.md,
    and additionally emit `*_ratio` fields (MiB RSS per MiB of data), which are
    what the memory regression gates actually compare across scales.
    Resolve the naming in SKILL.md before this becomes load-bearing.
    """
    MIB = 1048576
    peak = m.peak_rss_bytes
    denom = max(original_bytes, compressed_bytes)
    return {
        "peak_rss_mb": peak / MIB,
        "peak_uss_mb": m.peak_uss_bytes / MIB,
        "post_run_rss_mb": m.post_run_rss_bytes / MIB,
        # spec-verbatim (units: bytes of RSS per MiB of data)
        "memory_per_input_mb": (peak / original_bytes * MIB) if original_bytes else None,
        "memory_per_output_mb": (peak / compressed_bytes * MIB) if compressed_bytes else None,
        # dimensionless, scale-invariant -> these are the gate-bearing metrics
        "memory_per_input_ratio": (peak / original_bytes) if original_bytes else None,
        "memory_per_output_ratio": (peak / compressed_bytes) if compressed_bytes else None,
        "memory_amplification": (peak / denom) if denom else None,
    }
