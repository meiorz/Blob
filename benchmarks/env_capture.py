"""Environment + provenance capture. Every result row carries this."""
from __future__ import annotations
import hashlib, json, os, platform, subprocess, sys
from importlib.metadata import PackageNotFoundError, version
from typing import Callable, cast

# SKILL.md results must never be misread as hardware-validated. Set via
# COMPRESSION_BENCH_ENV; defaults to the most conservative label.
ENVIRONMENT_CLASS = os.environ.get("COMPRESSION_BENCH_ENV", "sandbox")


def _cpu_model() -> str:
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _physical_cores() -> int | None:
    try:
        ids = set()
        core_id = phys_id = None
        for line in open("/proc/cpuinfo"):
            if line.startswith("core id"):
                core_id = line.split(":")[1].strip()
            elif line.startswith("physical id"):
                phys_id = line.split(":")[1].strip()
            elif not line.strip() and core_id is not None:
                ids.add((phys_id, core_id)); core_id = phys_id = None
        if core_id is not None:
            ids.add((phys_id, core_id))
        return len(ids) or None
    except OSError:
        return None


def _mem_total_bytes() -> int:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def capture() -> dict:
    import pyarrow as pa
    sched_getaffinity = cast(Callable[[int], set[int]] | None, getattr(os, "sched_getaffinity", None))
    info = {
        "environment_class": ENVIRONMENT_CLASS,
        "hardware_validated": ENVIRONMENT_CLASS not in ("sandbox",),
        "hostname": platform.node(),
        "kernel": platform.release(),
        "os": platform.platform(),
        "cpu_model": _cpu_model(),
        "cpu_logical": os.cpu_count(),
        "cpu_physical_cores": _physical_cores(),
        "smt_active": (os.cpu_count() or 0) > (_physical_cores() or 0),
        "cpu_affinity": sorted(sched_getaffinity(0)) if sched_getaffinity is not None else None,
        "mem_total_bytes": _mem_total_bytes(),
        "python": sys.version.split()[0],
        "pyarrow": pa.__version__,
    }
    try:
        cramjam_version = _package_version("cramjam")
        if cramjam_version is not None:
            info["cramjam"] = cramjam_version
    except Exception:
        pass
    try:
        import zstandard
        zstandard_version = _package_version("zstandard")
        if zstandard_version is not None:
            info["zstandard_binding"] = zstandard_version
        info["libzstd"] = ".".join(str(x) for x in zstandard.ZSTD_VERSION)
    except Exception:
        pass
    # pyarrow bundles its own codec libraries; their versions are what actually
    # produced the Parquet bytes, so record them separately from the python bindings.
    try:
        info["pyarrow_cpp_build"] = {
            k: str(v) for k, v in pa.cpp_build_info._asdict().items()
        } if hasattr(pa, "cpp_build_info") else None
    except Exception:
        info["pyarrow_cpp_build"] = None
    info["parquet_codecs_available"] = {
        c: bool(pa.Codec.is_available(c)) for c in ("snappy", "zstd", "gzip", "lz4", "brotli")
    }
    # NOT MEASURED register: things this host structurally cannot validate.
    info["not_measured"] = []
    if info["smt_active"] or (info["cpu_physical_cores"] or 0) < 2:
        info["not_measured"].append(
            "concurrency_scaling: only %s physical core(s) with SMT; SKILL.md forbids "
            "comparing across concurrency levels on such a host"
            % info["cpu_physical_cores"]
        )
    if not os.access("/proc/sys/vm/drop_caches", os.W_OK):
        info["not_measured"].append(
            "cold_cache: /proc/sys/vm/drop_caches not writable (unprivileged); "
            "codec benchmarks run in-process on preloaded buffers to remove I/O from the hot path"
        )
    if info["mem_total_bytes"] < 16 * 1024**3:
        info["not_measured"].append(
            "absolute_per_worker_memory_budget: host has %.1f GiB total; the 4-8 GiB target / "
            "16 GiB hard limit cannot be exercised. Only scale-invariant memory metrics transfer."
            % (info["mem_total_bytes"] / 1024**3)
        )
    return info


def banner() -> None:
    """Announce the environment class before any numbers are emitted.

    Written to stderr, never stdout: analyze_results.py emits machine-readable JSON on
    stdout and a banner there would corrupt it.
    """
    hardware_validated = ENVIRONMENT_CLASS not in ("sandbox",)
    print(f"environment_class={ENVIRONMENT_CLASS} hardware_validated={str(hardware_validated).lower()}",
          file=sys.stderr)
    if not hardware_validated:
        print("  WARNING: sandbox run. Results are PROVISIONAL and cannot support a 'keep'\n"
              "  decision. Concurrency scaling, cold-cache behaviour and absolute per-worker\n"
              "  memory budgets are NOT MEASURED here. Set COMPRESSION_BENCH_ENV=cloud-<shape>\n"
              "  on real hardware. See docs/benchmark-methodology.md.",
              file=sys.stderr)


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


if __name__ == "__main__":
    print(json.dumps(capture(), indent=2))
