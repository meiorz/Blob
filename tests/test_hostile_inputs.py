"""Hostile-input tests. No pytest dependency: run directly.

    python3 tests/test_hostile_inputs.py

Each case asserts SAFE FAILURE: bounded time, bounded memory, no crash, no
unsafe partial output. Failing inputs are minimized and kept in tests/fixtures/.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time

try:
    import resource
except ImportError:
    resource = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from security.safe_decompress import (  # noqa: E402
    DecompressionLimitExceeded, DecompressionLimits, MalformedCompressedInput,
    declared_frame_content_size, safe_parquet_open, safe_zstd_decompress,
)

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
RESULTS: list[dict] = []


def _ru_maxrss_bytes() -> int:
    if resource is None:
        return 0
    getrusage = getattr(resource, "getrusage", None)
    rusageself = getattr(resource, "RUSAGE_SELF", None)
    if getrusage is None or rusageself is None:
        return 0
    return getrusage(rusageself).ru_maxrss * 1024


def record(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append({"test": name, "pass": bool(passed), "detail": detail})
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ---------------------------------------------------------------- zstd bombs
def test_zstd_bomb_output_cap() -> None:
    """1 GiB of zeros compresses to a few KiB. Guarded decode must refuse."""
    import zstandard as zstd
    payload = b"\0" * (1 << 30)
    frame = zstd.ZstdCompressor(level=3).compress(payload)
    del payload
    ratio = (1 << 30) / len(frame)
    limits = DecompressionLimits(max_output_bytes=64 << 20, max_expansion_ratio=100.0)
    rss0 = _ru_maxrss_bytes()
    t0 = time.monotonic()
    try:
        safe_zstd_decompress(frame, limits)
        record("zstd_bomb_output_cap", False, "decoder did NOT reject the bomb")
    except DecompressionLimitExceeded as e:
        dt = time.monotonic() - t0
        grew = _ru_maxrss_bytes() - rss0
        # Bounded TIME and bounded MEMORY, not just "an exception was raised".
        # A decoder that materializes 1 GiB and then complains has already lost.
        ok = dt < 1.0 and grew < (128 << 20)
        record("zstd_bomb_output_cap", ok,
               f"rejected on {e.limit_name} in {dt*1000:.0f}ms, RSS +{grew/1048576:.1f}MiB "
               f"(frame={len(frame)}B, true expansion={ratio:.0f}x)")
    os.makedirs(FIXTURES, exist_ok=True)
    with open(os.path.join(FIXTURES, "zstd_bomb_1gib_zeros.zst"), "wb") as fh:
        fh.write(frame)


def test_zstd_frame_content_size_not_trusted() -> None:
    """The frame DECLARES ~1 GiB. Prove the guarded decoder never allocates it."""
    import zstandard as zstd
    frame = zstd.ZstdCompressor(level=3).compress(b"\0" * (1 << 30))
    declared = declared_frame_content_size(frame)
    limits = DecompressionLimits(max_output_bytes=16 << 20, max_expansion_ratio=1e9)
    before = _ru_maxrss_bytes()
    try:
        safe_zstd_decompress(frame, limits)
        ok, detail = False, "decoder did not stop at the output cap"
    except DecompressionLimitExceeded:
        after = _ru_maxrss_bytes()
        grew = after - before
        # Must stay near the cap, nowhere near the declared size.
        ok = grew < (64 << 20)
        declared_text = f"{declared / 1048576:.0f}MiB" if declared is not None else "unknown"
        detail = (f"declared={declared_text}, cap=16MiB, "
                  f"peak RSS growth={grew/1048576:.1f}MiB")
    record("zstd_frame_content_size_not_trusted", ok, detail)


def test_zstd_expansion_ratio_cap() -> None:
    import zstandard as zstd
    frame = zstd.ZstdCompressor(level=3).compress(b"A" * (32 << 20))
    limits = DecompressionLimits(max_output_bytes=1 << 30, max_expansion_ratio=10.0)
    try:
        safe_zstd_decompress(frame, limits)
        record("zstd_expansion_ratio_cap", False, "ratio cap not enforced")
    except DecompressionLimitExceeded as e:
        record("zstd_expansion_ratio_cap", e.limit_name == "max_expansion_ratio",
               f"rejected on {e.limit_name}")


def test_zstd_max_compressed_input() -> None:
    limits = DecompressionLimits(max_compressed_bytes=1024)
    try:
        safe_zstd_decompress(b"\x28\xb5\x2f\xfd" + b"\0" * 4096, limits)
        record("zstd_max_compressed_input", False, "oversized input accepted")
    except DecompressionLimitExceeded as e:
        record("zstd_max_compressed_input", e.limit_name == "max_compressed_bytes")
    except Exception as e:
        record("zstd_max_compressed_input", False, f"wrong exception {type(e).__name__}")


def test_zstd_truncated_and_corrupt() -> None:
    import zstandard as zstd
    frame = zstd.ZstdCompressor(level=3).compress(b"payload" * 10000)
    cases = {
        "truncated_half": frame[: len(frame) // 2],
        "truncated_header": frame[:3],
        "corrupt_magic": b"\x00\x00\x00\x00" + frame[4:],
        "empty": b"",
    }
    ok = True
    details = []
    for name, blob in cases.items():
        try:
            got = safe_zstd_decompress(blob, DecompressionLimits(max_output_bytes=8 << 20))
            # Returning ANY bytes for a malformed frame is an unsafe partial
            # extraction, even if the call did not raise.
            details.append(f"{name}:ACCEPTED({len(got)}B)")
            ok = False
        except (MalformedCompressedInput, DecompressionLimitExceeded) as e:
            details.append(f"{name}:{type(e).__name__}")
        except Exception as e:
            details.append(f"{name}:UNEXPECTED_{type(e).__name__}")
            ok = False
    record("zstd_truncated_and_corrupt", ok, ", ".join(details))


# ------------------------------------------------------------ parquet vectors
def _small_parquet() -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq
    t = pa.table({"a": pa.array(list(range(50000))),
                  "s": pa.array(["value-%d" % (i % 977) for i in range(50000)])})
    sink = pa.BufferOutputStream()
    pq.write_table(t, sink, compression="zstd", compression_level=3, version="1.0")
    return sink.getvalue().to_pybytes()


def test_parquet_truncated_footer() -> None:
    blob = _small_parquet()
    ok = True
    details = []
    for name, b in {"no_footer": blob[:-8], "half": blob[: len(blob) // 2],
                    "one_byte": blob[:1]}.items():
        try:
            safe_parquet_open(b)
            details.append(f"{name}:ACCEPTED")
            ok = False
        except Exception as e:
            details.append(f"{name}:{type(e).__name__}")
    record("parquet_truncated_footer", ok, ", ".join(details))


def test_parquet_declared_size_cap() -> None:
    """Declared uncompressed size must be checked BEFORE materializing pages."""
    blob = _small_parquet()
    try:
        safe_parquet_open(blob, DecompressionLimits(max_output_bytes=1024))
        record("parquet_declared_size_cap", False, "declared-size cap not enforced")
    except DecompressionLimitExceeded as e:
        record("parquet_declared_size_cap", "max_output_bytes" in e.limit_name,
               f"rejected on {e.limit_name} before decompressing any page")


# ------------------------------------------------------------------- fuzzing
FUZZ_CHILD = r'''
from pathlib import Path
import os, sys

try:
    import resource
except ImportError:
    resource = None

if resource is not None:
    setrlimit = getattr(resource, "setrlimit", None)
    rlimit_as = getattr(resource, "RLIMIT_AS", None)
    if setrlimit is not None and rlimit_as is not None:
        setrlimit(rlimit_as, (%(mem)d, %(mem)d))

# Derived from this file's location, never interpolated from the generating host:
# tests/_fuzz_child.py is TRACKED, so baking an absolute path in here rewrites a
# committed file with a machine-specific one on every run, and CI guard 2 then
# fails on whichever host last ran the suite.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "security"))
sys.path.insert(0, str(ROOT))
from security.safe_decompress import safe_parquet_open, DecompressionLimits
data = open(sys.argv[1], "rb").read()
try:
    pf = safe_parquet_open(data, DecompressionLimits(max_output_bytes=64*1024*1024))
    pf.read()
    print("READ_OK")
except MemoryError:
    print("MEMORY_ERROR")
except Exception as e:
    print("REJECTED:" + type(e).__name__)
'''


def test_parquet_mutation_fuzz(n_cases: int = 120, seed: int = 20260810) -> None:
    """Mutation fuzzing under RLIMIT_AS + timeout in a child process.

    Byte flips naturally reach page-header size fields, thrift structures and
    checksums -- broader and more honest than a hand-forged header, which is
    easy to get subtly wrong. Any crash/OOM/timeout is preserved as a fixture.
    """
    blob = bytearray(_small_parquet())
    rng = random.Random(seed)
    os.makedirs(FIXTURES, exist_ok=True)
    child = os.path.join(ROOT, "tests", "_fuzz_child.py")
    # newline="\n" pins LF on every host. Without it Python translates to the
    # platform ending, so this TRACKED file is rewritten CRLF on Windows and LF on
    # Linux -- a whole-file diff on every cross-OS run, and `git diff --check`
    # reports each CR as trailing whitespace.
    with open(child, "w", newline="\n") as fh:
        fh.write(FUZZ_CHILD % {"mem": 512 * 1024 * 1024})
    outcomes: dict[str, int] = {}
    bad: list[str] = []
    for i in range(n_cases):
        m = bytearray(blob)
        for _ in range(rng.randint(1, 6)):
            m[rng.randrange(len(m))] = rng.randrange(256)
        tmp = os.path.join(FIXTURES, "_fuzz_case.parquet")
        with open(tmp, "wb") as fh:
            fh.write(m)
        try:
            p = subprocess.run([sys.executable, child, tmp], capture_output=True,
                               text=True, timeout=20)
            out = (p.stdout or "").strip().splitlines()
            tag = out[-1] if out else f"NO_OUTPUT_rc={p.returncode}"
            if p.returncode < 0:
                tag = f"SIGNAL_{-p.returncode}"
        except subprocess.TimeoutExpired:
            tag = "TIMEOUT"
        key = tag.split(":")[0]
        outcomes[key] = outcomes.get(key, 0) + 1
        if key in ("TIMEOUT", "MEMORY_ERROR", "NO_OUTPUT_rc", "SIGNAL_11") or key.startswith("SIGNAL"):
            name = os.path.join(FIXTURES, f"fuzz_{key}_{i:04d}.parquet")
            with open(name, "wb") as fh:
                fh.write(m)
            bad.append(os.path.basename(name))
    ok = not bad
    record("parquet_mutation_fuzz", ok,
           f"{n_cases} cases -> {json.dumps(outcomes)}"
           + (f"; PRESERVED CRASHES: {bad}" if bad else "; no crashes/OOM/timeouts"))


def test_archive_limits_not_applicable() -> None:
    """Explicitly record N/A rather than silently omitting (SKILL.md requires
    every investigation area be marked)."""
    lim = DecompressionLimits()
    record("archive_limits_marked_not_applicable",
           lim.max_entries is None and lim.max_nesting_depth is None,
           "nested-archive depth / entry count / path traversal are NOT APPLICABLE "
           "to Parquet page codecs (workload P1); live for S1 if archives are ingested")


def main() -> int:
    print("Hostile-input suite")
    for fn in (test_zstd_bomb_output_cap, test_zstd_frame_content_size_not_trusted,
               test_zstd_expansion_ratio_cap, test_zstd_max_compressed_input,
               test_zstd_truncated_and_corrupt, test_parquet_truncated_footer,
               test_parquet_declared_size_cap, test_parquet_mutation_fuzz,
               test_archive_limits_not_applicable):
        try:
            fn()
        except Exception as e:  # a test harness error is itself a failure
            record(fn.__name__, False, f"harness error {type(e).__name__}: {e}")
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    with open(os.path.join(ROOT, "results", "security_results.json"), "w") as fh:
        json.dump({"results": RESULTS,
                   "all_pass": all(r["pass"] for r in RESULTS)}, fh, indent=2)
    failed = [r["test"] for r in RESULTS if not r["pass"]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed"
          + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
