"""Environment + provenance capture. Every result row carries this."""
from __future__ import annotations
import hashlib, json, os, platform, re, subprocess, sys
from importlib.metadata import PackageNotFoundError, version
from typing import Callable, cast

# SKILL.md results must never be misread as hardware-validated.
#
# This is an ALLOWLIST, not a comparison against "sandbox". The previous form,
#
#     ENVIRONMENT_CLASS = os.environ.get("COMPRESSION_BENCH_ENV", "sandbox")
#     hardware_validated = ENVIRONMENT_CLASS not in ("sandbox",)
#
# failed OPEN: every value that was not exactly "sandbox" claimed hardware
# validation, so `Sandbox`, `sanbox`, `SANDBOX` and an exported-but-empty
# variable all produced cells asserting the strongest claim the project makes,
# from a typo. The safe default has to be unreachable by accident, so an
# unrecognised value is refused outright rather than resolved to something.
ENVIRONMENT_CLASS_SANDBOX = "sandbox"
ENVIRONMENT_CLASS_LITERALS = (ENVIRONMENT_CLASS_SANDBOX, "dev-local")

# cloud-<shape>: lowercase, starts alphanumeric, then alphanumerics . _ -
# e.g. cloud-8x32, cloud-m5.2xlarge, cloud-c6i-4x16
CLOUD_CLASS_RE = re.compile(r"^cloud-[a-z0-9][a-z0-9._-]*$")

VALID_ENVIRONMENT_CLASS_HELP = (
    f"valid values: {', '.join(repr(v) for v in ENVIRONMENT_CLASS_LITERALS)}, "
    "or 'cloud-<shape>' matching " + CLOUD_CLASS_RE.pattern + " "
    "(e.g. 'cloud-8x32', 'cloud-m5.2xlarge'). "
    "Unset or empty means 'sandbox'. Matching is exact and case-sensitive."
)


class InvalidEnvironmentClass(ValueError):
    """COMPRESSION_BENCH_ENV is not a recognised environment class.

    Raised at import so an invalid value cannot reach a benchmark cell. Failing
    here costs a command; failing open costs a false hardware-validation claim
    inside recorded, append-only evidence.
    """


def resolve_environment_class(raw: str | None) -> str:
    """Environment class for a raw COMPRESSION_BENCH_ENV value.

    Unset or empty/whitespace -> 'sandbox' (the conservative default).
    Anything else must match the allowlist exactly, or this raises.
    """
    if raw is None or not raw.strip():
        return ENVIRONMENT_CLASS_SANDBOX
    value = raw.strip()
    if value in ENVIRONMENT_CLASS_LITERALS or CLOUD_CLASS_RE.match(value):
        return value
    raise InvalidEnvironmentClass(
        f"COMPRESSION_BENCH_ENV={raw!r} is not a recognised environment class. "
        + VALID_ENVIRONMENT_CLASS_HELP
        + " Refusing to continue: an unrecognised value used to be treated as "
          "'not sandbox', which set hardware_validated=true on every cell."
    )


ENVIRONMENT_CLASS = resolve_environment_class(os.environ.get("COMPRESSION_BENCH_ENV"))

# Derived once, from the validated class. Never recompute this by comparing a
# string to "sandbox" -- that is the pattern that failed open.
HARDWARE_VALIDATED = ENVIRONMENT_CLASS != ENVIRONMENT_CLASS_SANDBOX


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
        "hardware_validated": HARDWARE_VALIDATED,
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
    # ---- NOT MEASURED register ------------------------------------------------
    #
    # An entry's CAUSE decides whether hardware can retire it, and the register
    # used to confuse the two. Every entry was conditional on host capability, so
    # on a 4-8 core node the concurrency caveat vanished -- while run_cell.py went
    # on calling pa.set_cpu_count(1) and measuring exactly one thread. A register
    # that shrinks on a bigger machine reads as "we measured more this time".
    #
    # So: entries whose cause is in the CODE are unconditional. Host capability
    # may ADD an entry; it may never remove one the code guarantees.
    info["not_measured"] = []

    def register(entry_id: str, cause: str, retires_on_better_hardware: bool,
                 detail: str) -> None:
        info["not_measured"].append({
            "id": entry_id,
            "cause": cause,                                   # harness design | host capability
            "retires_on_better_hardware": retires_on_better_hardware,
            "detail": detail,
        })

    # --- harness design: true regardless of the machine -------------------------
    # While run_cell.py hardcodes pa.set_cpu_count(1) and pins to one CPU, no run
    # varies concurrency, so no run can measure its effect. A 64-core node changes
    # nothing about that.
    register(
        "concurrency_scaling", "harness design", False,
        "benchmarks/run_cell.py pins to a single CPU and hardcodes "
        "pa.set_cpu_count(1) / set_io_thread_count(1), so every cell is "
        "single-threaded by construction and no run varies concurrency. SKILL.md "
        "forbids comparing across concurrency levels; here there is nothing to "
        "compare. Retiring this needs a harness change, not a bigger host.",
    )
    # While codec benchmarks build and read in-memory Arrow buffers, no disk read
    # happens on the hot path, so cache state is not a variable being measured.
    register(
        "cold_cache", "harness design", False,
        "codec benchmarks run entirely on preloaded in-memory Arrow buffers with "
        "no disk on the hot path (SKILL.md I/O isolation), so neither a cold nor a "
        "warm cache is exercised. Root access to /proc/sys/vm/drop_caches would "
        "not change this. End-to-end object-store cost is modelled analytically "
        "by benchmarks/model_crossover.py instead.",
    )

    # --- host capability: a better machine genuinely retires these --------------
    if info["mem_total_bytes"] < 16 * 1024**3:
        register(
            "absolute_per_worker_memory_budget", "host capability", True,
            "host has %.1f GiB total; the 4-8 GiB per-worker target and 16 GiB hard "
            "limit cannot be exercised. Only scale-invariant memory metrics "
            "(memory_per_input_ratio, memory_amplification, growth class) transfer."
            % (info["mem_total_bytes"] / 1024**3),
        )
    # Where this fires, no benchmark cell can be produced at all: profile_memory
    # raises and run_cell/orchestrate refuse to record. Listed so env_capture.py
    # run standalone says so before a sweep is attempted.
    try:
        from memory_profiler import process_sampling_support
    except ImportError:  # pragma: no cover - package-qualified import path
        from benchmarks.memory_profiler import process_sampling_support
    sampling_ok, sampling_reason = process_sampling_support()
    info["process_memory_sampling_available"] = sampling_ok
    if not sampling_ok:
        register(
            "process_memory_profiling", "host capability", True,
            sampling_reason + " G4/G5/G6 are UNEVALUABLE on this host and benchmark "
            "cells cannot be recorded.",
        )

    # Extra host-capability detail on top of the unconditional concurrency entry.
    # It narrows WHY a comparison would be invalid here; it does not make the
    # entry conditional.
    if info["smt_active"] or (info["cpu_physical_cores"] or 0) < 2:
        register(
            "concurrency_scaling_host_constraint", "host capability", True,
            "additionally, this host reports %s physical core(s)%s, so even a "
            "harness that varied concurrency could not produce a valid comparison "
            "here." % (info["cpu_physical_cores"],
                       " with SMT active" if info["smt_active"] else ""),
        )
    return info


# --------------------------------------------------------------- host identity
#
# environment_class is a LABEL, not a machine identity. It defaults to "sandbox"
# whenever COMPRESSION_BENCH_ENV is unset, so a Windows workstation and the Linux
# benchmark container both record "sandbox" and any guard keyed on that label
# alone waves the mix through. That is not hypothetical: cells measured on
# Linux-6.8 and on Windows-10 both carry environment_class="sandbox" in this
# repo's results/raw today, and their encode medians differ by >120%.
#
# SKILL.md: "Never compare results collected with different datasets, different
# machines, materially different concurrency, or inconsistent cache states."
# These three fields are what distinguishes the machine.
HOST_FINGERPRINT_FIELDS = ("os", "cpu_model", "mem_total_bytes")


def host_fingerprint(env: dict) -> tuple:
    """Machine identity of one recorded cell's `env` block."""
    env = env or {}
    return tuple(env.get(f) for f in HOST_FINGERPRINT_FIELDS)


def describe_fingerprint(fp: tuple) -> str:
    parts = []
    for name, value in zip(HOST_FINGERPRINT_FIELDS, fp):
        if name == "mem_total_bytes" and isinstance(value, int) and value:
            value = f"{value / 1024 ** 3:.1f} GiB"
        parts.append(f"{name}={value!r}")
    return ", ".join(parts)


def fingerprint_groups(labelled_envs: dict) -> dict:
    """Group {cell_label: env} by machine identity. >1 group == mixed hosts."""
    groups: dict = {}
    for label, env in labelled_envs.items():
        groups.setdefault(host_fingerprint(env), []).append(label)
    return {fp: sorted(labels) for fp, labels in groups.items()}


def mixed_host_report(labelled_envs: dict) -> str | None:
    """None when every cell came from one machine; otherwise an explanation.

    Shared by scripts/analyze_results.py and scripts/verify_run_integrity.py so
    the two cannot disagree about what counts as one host.
    """
    groups = fingerprint_groups(labelled_envs)
    if len(groups) <= 1:
        return None
    lines = [f"cells span {len(groups)} DIFFERENT HOSTS (environment_class does not "
             "distinguish them; it defaults to 'sandbox' everywhere):"]
    for fp, labels in sorted(groups.items(), key=lambda kv: str(kv[0])):
        lines.append(f"  host: {describe_fingerprint(fp)}")
        for lbl in labels:
            lines.append(f"    - {lbl}")
    lines.append("SKILL.md forbids comparing results collected on different machines. "
                 "Re-run the affected cells on one host, or analyse them separately.")
    return "\n".join(lines)


def banner() -> None:
    """Announce the environment class before any numbers are emitted.

    Written to stderr, never stdout: analyze_results.py emits machine-readable JSON on
    stdout and a banner there would corrupt it.
    """
    print(f"environment_class={ENVIRONMENT_CLASS} "
          f"hardware_validated={str(HARDWARE_VALIDATED).lower()}",
          file=sys.stderr)
    if not HARDWARE_VALIDATED:
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
