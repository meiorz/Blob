#!/usr/bin/env python3
"""Parquet reader / codec compatibility probe.

Run this against every reader in your estate. Codec support is a HARD GATE for
Iteration 1: readers are unenumerated, and a codec change that a consumer cannot
read is a rejected change under SKILL.md regardless of measured wins.

    python3 scripts/probe_reader_support.py --write /tmp/compat   # produce probe files
    python3 scripts/probe_reader_support.py --read  /tmp/compat   # verify on a reader
"""
from __future__ import annotations

import argparse, json, os, sys

ARMS = [("none", None), ("snappy", None), ("zstd", 3), ("gzip", 6)]
VERSIONS = ["1.0", "2.6"]


def make_table():
    import pyarrow as pa
    n = 20000
    return pa.table({
        "i": pa.array(range(n)),
        "ts": pa.array([1700000000 + i for i in range(n)], pa.timestamp("ms")),
        "low_card": pa.array([f"dim-{i % 12}" for i in range(n)]),
        "high_card": pa.array([f"https://example.invalid/p/{i}?q={i*7}" for i in range(n)]),
        "f": pa.array([i * 1.5 for i in range(n)], pa.float64()),
    })


def write(outdir: str) -> int:
    import pyarrow.parquet as pq
    os.makedirs(outdir, exist_ok=True)
    t = make_table()
    made = []
    for codec, lvl in ARMS:
        for ver in VERSIONS:
            name = f"probe_{codec}_v{ver.replace('.', '')}.parquet"
            path = os.path.join(outdir, name)
            try:
                pq.write_table(t, path, compression=codec, compression_level=lvl,
                               version=ver, data_page_size=1 << 20, use_dictionary=True)
                made.append(name)
            except Exception as e:
                print(f"  write FAILED {name}: {e}", file=sys.stderr)
    with open(os.path.join(outdir, "expected.json"), "w") as fh:
        json.dump({"files": made, "num_rows": t.num_rows,
                   "columns": t.schema.names}, fh, indent=2)
    print(f"wrote {len(made)} probe files to {outdir}")
    print("Now copy this directory to each reader in your estate and verify every file "
          "loads with the expected row count. Record outcomes in docs/compatibility-matrix.md.")
    return 0


def read(outdir: str) -> int:
    import pyarrow.parquet as pq
    exp = json.load(open(os.path.join(outdir, "expected.json")))
    ok = True
    for name in exp["files"]:
        try:
            t = pq.read_table(os.path.join(outdir, name))
            good = t.num_rows == exp["num_rows"]
            print(f"  [{'PASS' if good else 'FAIL'}] {name} rows={t.num_rows}")
            ok &= good
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            ok = False
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write"); ap.add_argument("--read")
    a = ap.parse_args()
    if a.write:
        return write(a.write)
    if a.read:
        return read(a.read)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
