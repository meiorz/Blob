"""Iteration 1 benchmark cell: Parquet page codec is the ONLY variable.

Everything else (row group size, data page size, writer version, dictionary
usage, column order, row order, statistics) is held constant by WRITER_FIXED.

Methodology notes:
  * Codec-only path: Parquet bytes are produced into and read from an in-memory
    Arrow buffer. No disk on the hot path (SKILL.md I/O isolation), which also
    removes the cold/warm cache confound this host cannot control.
  * Timing trials run WITHOUT the memory sampler attached; a separate profiled
    run collects memory. Sampler overhead must not contaminate latency numbers.
  * "bytes scanned per query" is computed exactly from Parquet column-chunk
    metadata (total_compressed_size), not estimated.
"""
from __future__ import annotations

import gc
import time
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.parquet as pq

# Dual-path import: this module is reached two ways and must work from both.
#   * `from benchmarks.parquet_bench import ...` (tests/scripts, package-qualified)
#   * `import parquet_bench` after run_cell.py puts benchmarks/ on sys.path
#     (each benchmark cell runs as a standalone script in a fresh process)
# A flat-only import breaks the first; a package-only import breaks the second.
try:
    from benchmarks.memory_profiler import derived_memory_metrics, profile_memory
except ImportError:  # pragma: no cover - script-mode fallback
    from memory_profiler import derived_memory_metrics, profile_memory

MIB = 1048576

# ---- held constant across every arm -----------------------------------------
WRITER_VERSION: str = "1.0"      # V1 data pages: conservative given unenumerated readers
WRITER_DATA_PAGE_SIZE: int = 1 * MIB
WRITER_WRITE_STATISTICS: bool = True
WRITER_USE_DICTIONARY: bool = True
WRITER_WRITE_PAGE_INDEX: bool = False
ROW_GROUP_BYTES_TARGET = 128 * MIB
# -----------------------------------------------------------------------------

CODEC_ARMS = {
    "none":    dict(compression="none",   compression_level=None),
    "snappy":  dict(compression="snappy", compression_level=None),   # BASELINE
    "zstd-3":  dict(compression="zstd",   compression_level=3),
    "gzip-6":  dict(compression="gzip",   compression_level=6),
}
BASELINE_ARM = "snappy"


def now_ns() -> int:
    return time.monotonic_ns()


def cpu_ns() -> int:
    return time.process_time_ns()


@dataclass
class Timing:
    wall_ms: float
    cpu_ms: float


def _time_once(fn) -> tuple[Timing, object]:
    w0, c0 = now_ns(), cpu_ns()
    out = fn()
    w1, c1 = now_ns(), cpu_ns()
    return Timing((w1 - w0) / 1e6, (c1 - c0) / 1e6), out


def slice_to_bytes(table: pa.Table, target_bytes: int) -> pa.Table:
    """Slice to approximately target uncompressed Arrow bytes (whole rows).

    Returns a zero-copy VIEW and deliberately does NOT combine_chunks(). The
    caller must combine after dropping its reference to the source table --
    combining first materializes a full copy alongside the original, which
    inflates baseline RSS and corrupts every memory metric derived from it.
    See load_scaled_table().
    """
    if table.nbytes <= target_bytes or table.num_rows == 0:
        return table
    frac = target_bytes / table.nbytes
    n = max(1, int(table.num_rows * frac))
    return table.slice(0, n)


def load_scaled_table(source_path: str, target_bytes: int,
                      columns: list[str] | None = None) -> pa.Table:
    """Load ~target_bytes of Arrow data holding NOTHING else alive afterwards.

    Memory-measurement correctness depends on this function. The earlier version
    read the entire Parquet file, then sliced, and kept the full table reachable
    for the whole cell -- so `baseline_rss` already contained the full dataset
    and every derived metric (memory_per_input_ratio, amplification, growth
    class) was an artifact of the harness rather than of the codec.

    Three rules, in order:
      1. Read only as many row groups as needed to reach the target.
      2. Slice (zero-copy) BEFORE combining.
      3. Drop every intermediate reference, collect, and release the Arrow
         arena back to the OS before the caller establishes its baseline.
    """
    pf = pq.ParquetFile(source_path)
    parts: list[pa.Table] = []
    total = 0
    for rg in range(pf.metadata.num_row_groups):
        t = pf.read_row_group(rg, columns=columns)
        parts.append(t)
        total += t.nbytes
        if total >= target_bytes:
            break
    raw = pa.concat_tables(parts) if len(parts) > 1 else parts[0]
    del parts, pf
    view = slice_to_bytes(raw, target_bytes)
    table = view.combine_chunks()   # copies only the sliced portion
    del view, raw
    gc.collect()
    pa.default_memory_pool().release_unused()
    return table


def row_group_rows(table: pa.Table) -> int:
    """Rows per row group to hit ROW_GROUP_BYTES_TARGET of uncompressed data."""
    if table.num_rows == 0:
        return 1
    per_row = max(table.nbytes / table.num_rows, 1e-9)
    return max(1, min(table.num_rows, int(ROW_GROUP_BYTES_TARGET / per_row)))


def write_parquet_buffer(table: pa.Table, arm: str) -> pa.Buffer:
    cfg = CODEC_ARMS[arm]
    sink = pa.BufferOutputStream()
    pq.write_table(
        table,
        sink,
        compression=cfg["compression"],
        compression_level=cfg["compression_level"],
        row_group_size=row_group_rows(table),
        version=WRITER_VERSION,
        data_page_size=WRITER_DATA_PAGE_SIZE,
        write_statistics=WRITER_WRITE_STATISTICS,
        use_dictionary=WRITER_USE_DICTIONARY,
        write_page_index=WRITER_WRITE_PAGE_INDEX,
    )
    return sink.getvalue()


def column_compressed_bytes(buf: pa.Buffer) -> dict[str, int]:
    """Exact per-column compressed footprint from Parquet metadata."""
    md = pq.ParquetFile(pa.BufferReader(buf)).metadata
    out: dict[str, int] = {}
    for rg in range(md.num_row_groups):
        g = md.row_group(rg)
        for c in range(g.num_columns):
            col = g.column(c)
            name = col.path_in_schema
            out[name] = out.get(name, 0) + col.total_compressed_size
    return out


def projected_bytes(buf: pa.Buffer, projection: list[str]) -> int:
    cols = column_compressed_bytes(buf)
    # Parquet paths for nested cols use dotted paths; match on leading segment.
    total = 0
    for name, nbytes in cols.items():
        if name in projection or name.split(".")[0] in projection:
            total += nbytes
    return total


def tables_equal(a: pa.Table, b: pa.Table) -> bool:
    """Lossless verification. Parquet re-encoding is not byte-identical, so the
    correct assertion is full Arrow value+schema+null equality, not file hash."""
    if a.schema != b.schema or a.num_rows != b.num_rows:
        return False
    return a.equals(b)


def run_cell(table: pa.Table, arm: str, projection: list[str], trials: int,
             latency_trials: int = 30) -> dict:
    """One (dataset x scale x codec) cell. Returns a structured result dict."""
    original_bytes = table.nbytes

    # ---- build once: footprint + integrity ----------------------------------
    buf = write_parquet_buffer(table, arm)
    compressed_bytes = buf.size
    col_bytes = column_compressed_bytes(buf)
    proj_bytes = projected_bytes(buf, projection)

    roundtrip = pq.read_table(pa.BufferReader(buf))
    integrity_ok = tables_equal(table, roundtrip)
    del roundtrip

    # ---- timing trials (no sampler attached) --------------------------------
    enc, dec, dec_proj = [], [], []
    write_parquet_buffer(table, arm)                      # warmup
    pq.read_table(pa.BufferReader(buf))                   # warmup
    for _ in range(trials):
        t, out = _time_once(lambda: write_parquet_buffer(table, arm))
        enc.append(t); del out
        t, out = _time_once(lambda: pq.read_table(pa.BufferReader(buf)))
        dec.append(t); del out
    # Projected decode is the query-latency proxy and the cheapest op, so it
    # gets more samples. SKILL.md permits p50/p95/p99 only where the sample
    # count justifies it; n>=30 here, n=trials elsewhere (percentiles suppressed).
    for _ in range(max(latency_trials, trials)):
        t, out = _time_once(lambda: pq.read_table(pa.BufferReader(buf), columns=projection))
        dec_proj.append(t); del out

    # ---- memory-profiled runs (separate from timing) ------------------------
    # The operation's OUTPUT must be released INSIDE the profiled block.
    # Holding it until after __exit__ means post_run_rss still contains it, so
    # the post-run-retention gate (G5) measures the harness's own reference
    # rather than genuine non-returned memory -- which made G5 fail for every
    # arm, including ones with nothing wrong. The sampler has already captured
    # the peak by this point, so freeing early costs no fidelity.
    with profile_memory() as pm_enc:
        _b = write_parquet_buffer(table, arm)
        del _b
    mem_enc = pm_enc.metrics
    with profile_memory() as pm_dec:
        _t = pq.read_table(pa.BufferReader(buf))
        del _t
    mem_dec = pm_dec.metrics

    def series(ts: list[Timing]) -> dict:
        return {
            "wall_ms": [t.wall_ms for t in ts],
            "cpu_ms": [t.cpu_ms for t in ts],
        }

    return {
        "arm": arm,
        "original_bytes": original_bytes,
        "compressed_bytes": compressed_bytes,
        "projected_compressed_bytes": proj_bytes,
        # uncompressed Arrow bytes of the projected subset: needed by the
        # object-store model, which composes transfer bytes with decode time.
        "projected_original_bytes": table.select(projection).nbytes,
        "projection": projection,
        "num_rows": table.num_rows,
        "num_columns": table.num_columns,
        "row_group_rows": row_group_rows(table),
        "compression_ratio": original_bytes / compressed_bytes if compressed_bytes else None,
        "space_savings_pct": 100.0 * (1 - compressed_bytes / original_bytes) if original_bytes else None,
        "integrity_lossless": integrity_ok,
        "column_compressed_bytes": col_bytes,
        "encode": series(enc),
        "decode_full": series(dec),
        "decode_projected": series(dec_proj),
        "memory_encode": {**mem_enc.as_dict(),
                          **derived_memory_metrics(mem_enc, original_bytes, compressed_bytes)},
        "memory_decode": {**mem_dec.as_dict(),
                          **derived_memory_metrics(mem_dec, original_bytes, compressed_bytes)},
        "writer_fixed": {
            "version": WRITER_VERSION,
            "data_page_size": WRITER_DATA_PAGE_SIZE,
            "write_statistics": WRITER_WRITE_STATISTICS,
            "use_dictionary": WRITER_USE_DICTIONARY,
            "write_page_index": WRITER_WRITE_PAGE_INDEX,
            "row_group_bytes_target": ROW_GROUP_BYTES_TARGET,
        },
        "codec_config": CODEC_ARMS[arm],
    }
