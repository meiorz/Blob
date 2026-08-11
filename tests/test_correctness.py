"""Lossless correctness + edge cases. No pytest dependency.

    python3 tests/test_correctness.py

Parquet re-encoding is NOT byte-identical, so file-hash equality is the wrong
assertion. The correct one is full Arrow value + schema + null equality after a
round trip, which is what tables_equal() checks.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pyarrow as pa            # noqa: E402
import pyarrow.parquet as pq    # noqa: E402
from benchmarks.parquet_bench import CODEC_ARMS, tables_equal, write_parquet_buffer  # noqa: E402

RESULTS: list[dict] = []


def record(name, ok, detail=""):
    RESULTS.append({"test": name, "pass": bool(ok), "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def roundtrip(t: pa.Table, arm: str) -> bool:
    buf = write_parquet_buffer(t, arm)
    return tables_equal(t, pq.read_table(pa.BufferReader(buf)))


EDGE_CASES = {
    "empty_table":      lambda: pa.table({"a": pa.array([], pa.int64())}),
    "single_row":       lambda: pa.table({"a": pa.array([1]), "s": pa.array(["x"])}),
    "all_null_column":  lambda: pa.table({"a": pa.array([None] * 1000, pa.int64()),
                                          "b": pa.array(list(range(1000)))}),
    "repeated_values":  lambda: pa.table({"a": pa.array([7] * 100000)}),
    "incompressible":   lambda: pa.table({"b": pa.array([os.urandom(64) for _ in range(20000)],
                                                        pa.binary())}),
    "unicode_4byte":    lambda: pa.table({"s": pa.array(["\U0001F600\U0001F1EF\U0001F1F5" * 3,
                                                         "écafé", "中文",
                                                         " embedded"] * 5000)}),
    "huge_single_string": lambda: pa.table({"s": pa.array(["z" * (4 << 20)])}),
    "wide_row":         lambda: pa.table({f"c{i}": pa.array([i] * 500) for i in range(512)}),
    "mixed_nulls":      lambda: pa.table({"a": pa.array([1, None, 3, None] * 5000),
                                          "s": pa.array(["a", None, "", "d"] * 5000),
                                          "f": pa.array([1.5, None, float("inf"), -0.0] * 5000)}),
}


def test_edge_cases():
    for arm in CODEC_ARMS:
        bad = []
        for name, make in EDGE_CASES.items():
            try:
                if not roundtrip(make(), arm):
                    bad.append(name)
            except Exception as e:
                bad.append(f"{name}({type(e).__name__})")
        record(f"edge_cases[{arm}]", not bad,
               f"{len(EDGE_CASES)} cases" + (f"; FAILED {bad}" if bad else " all lossless"))


def test_determinism():
    """SKILL.md: test whether repeated compression with identical settings gives
    identical output; if not, document why."""
    t = pa.table({"a": pa.array(range(50000)),
                  "s": pa.array([f"v{i % 997}" for i in range(50000)])})
    for arm in CODEC_ARMS:
        a = write_parquet_buffer(t, arm).to_pybytes()
        b = write_parquet_buffer(t, arm).to_pybytes()
        record(f"determinism[{arm}]", a == b,
               "byte-identical across repeated writes" if a == b
               else f"NOT byte-identical ({len(a)} vs {len(b)} bytes) -- investigate before "
                    "relying on content-addressed storage or dedup")


def test_cross_codec_value_identity():
    """Every arm must decode to the SAME values. A codec that changes data is a
    correctness failure, not a compression trade-off."""
    t = pa.table({"a": pa.array(range(20000)),
                  "s": pa.array([f"row-{i % 577}" for i in range(20000)]),
                  "f": pa.array([i * 0.25 for i in range(20000)])})
    ref = pq.read_table(pa.BufferReader(write_parquet_buffer(t, "none")))
    bad = [arm for arm in CODEC_ARMS
           if not tables_equal(ref, pq.read_table(pa.BufferReader(write_parquet_buffer(t, arm))))]
    record("cross_codec_value_identity", not bad,
           "all arms decode identically" if not bad else f"DIVERGED: {bad}")


def test_already_compressed_control():
    """SKILL.md-mandated do-not-recompress control: high-entropy input must not
    shrink, and paying CPU for it must be visible as a negative result."""
    import gzip
    blobs = [gzip.compress(os.urandom(4096)) for _ in range(4000)]
    t = pa.table({"blob": pa.array(blobs, pa.binary())})
    sizes = {arm: write_parquet_buffer(t, arm).size for arm in CODEC_ARMS}
    base = sizes["none"]
    worst = max(sizes[a] / base for a in sizes if a != "none")
    record("already_compressed_control", worst < 1.05,
           "; ".join(f"{a}={sizes[a] / base:.4f}x vs uncompressed" for a in sorted(sizes))
           + " -- ratios ~1.0 confirm no gain is available on pre-compressed bytes")


def main() -> int:
    print("Correctness suite")
    for fn in (test_edge_cases, test_determinism, test_cross_codec_value_identity,
               test_already_compressed_control):
        try:
            fn()
        except Exception as e:
            record(fn.__name__, False, f"harness error {type(e).__name__}: {e}")
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    with open(os.path.join(ROOT, "results", "correctness_results.json"), "w") as fh:
        json.dump({"results": RESULTS, "all_pass": all(r["pass"] for r in RESULTS)}, fh, indent=2)
    failed = [r["test"] for r in RESULTS if not r["pass"]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed"
          + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
