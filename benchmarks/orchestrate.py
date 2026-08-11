"""Sweep the single variable (Parquet page codec) across datasets and scales.

One subprocess per cell. Emits results/raw/<dataset>__<scale>__<arm>.json with
integer bytes and millisecond durations, per SKILL.md measurement conventions.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, "results", "raw")

DEFAULT_SCALES = {"S": 128 * 1048576, "M": 512 * 1048576, "L": 1536 * 1048576}


def load_catalog(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def run(dataset: dict, scale_label: str, scale_bytes: int, arm: str,
        trials: int, pin: int, timeout: int) -> dict | None:
    cfg = {
        "dataset_id": dataset["id"],
        "source_path": dataset["path"],
        "dataset_sha256": dataset.get("sha256"),
        "columns": dataset.get("columns"),
        "projection": dataset["projection"],
        "scale_label": scale_label,
        "scale_bytes": scale_bytes,
        "arm": arm,
        "trials": trials,
        "pin_cpu": pin,
    }
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "run_cell.py"), json.dumps(cfg)],
        capture_output=True, text=True, timeout=timeout,
    )
    dt = time.monotonic() - t0
    if proc.returncode != 0:
        print(f"  !! {dataset['id']}/{scale_label}/{arm} FAILED rc={proc.returncode}: "
              f"{proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ''}",
              file=sys.stderr)
        return None
    res = json.loads(proc.stdout)
    res["cell_wall_s"] = dt
    os.makedirs(RAW, exist_ok=True)
    with open(os.path.join(RAW, f"{dataset['id']}__{scale_label}__{arm}.json"), "w") as fh:
        json.dump(res, fh)
    print(f"  {dataset['id']}/{scale_label}/{arm}: "
          f"ratio={res['compression_ratio']:.3f} "
          f"integrity={'PASS' if res['integrity_lossless'] else 'FAIL'} "
          f"peakRSS={res['memory_decode']['peak_rss_mb']:.0f}MiB ({dt:.1f}s)")
    return res


def _write_manifest(root, run_id, args, produced, arms, scales, datasets, complete):
    manifest = {"run_id": run_id,
                "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "trials": args.trials, "pin_cpu": args.pin, "cells": produced,
                "arms": arms, "scales": scales,
                "datasets": [d["id"] for d in datasets], "complete": complete}
    os.makedirs(os.path.join(root, "results"), exist_ok=True)
    for name in ("latest_manifest.json", f"manifest_{run_id}.json"):
        with open(os.path.join(root, "results", name), "w") as fh:
            json.dump(manifest, fh, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=os.path.join(ROOT, "data", "metadata", "catalog.json"))
    ap.add_argument("--trials", type=int, default=10)   # shared host -> SKILL.md minimum 10
    ap.add_argument("--pin", type=int, default=0)
    ap.add_argument("--scales", default="S,M,L")
    ap.add_argument("--arms", default="none,snappy,zstd-3,gzip-6")
    ap.add_argument("--datasets", default="")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--resume", action="store_true",
                    help="skip cells already recorded in the current run manifest")
    args = ap.parse_args()

    catalog = load_catalog(args.catalog)
    wanted = set(filter(None, args.datasets.split(",")))
    datasets = [d for d in catalog["datasets"] if not wanted or d["id"] in wanted]
    scales = [s for s in args.scales.split(",") if s]
    arms = [a for a in args.arms.split(",") if a]

    # Baseline first, always: SKILL.md forbids optimizing before a baseline exists.
    arms.sort(key=lambda a: (a != "snappy", a))

    run_id = args.run_id or (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6])
    # Resume support: a full sweep exceeds the host's per-invocation limit, so it
    # runs in chunks. Cells already recorded FOR THIS run_id are skipped; cells
    # from an earlier run_id are NOT trusted, because a harness change (e.g. the
    # memory-measurement fix) invalidates every cell produced before it.
    manifest_path = os.path.join(ROOT, "results", "latest_manifest.json")
    produced = []
    if args.resume and os.path.exists(manifest_path):
        try:
            prev = json.load(open(manifest_path))
            if prev.get("run_id") == run_id:
                produced = list(prev.get("cells", []))
                print(f"resuming run_id={run_id}: {len(produced)} cells already done")
        except Exception:
            pass
    done = set(produced)
    ok = True
    for d in datasets:
        for s in scales:
            sb = d.get("scale_bytes", {}).get(s) or DEFAULT_SCALES[s]
            print(f"[{d['id']}] scale {s} = {sb/1048576:.0f} MiB uncompressed Arrow")
            for arm in arms:
                name = f"{d['id']}__{s}__{arm}.json"
                if name in done:
                    print(f"  skip (done) {name}")
                    continue
                if run(d, s, sb, arm, args.trials, args.pin, args.timeout) is None:
                    ok = False
                else:
                    produced.append(name)
                    done.add(name)
                    # Persist after EVERY cell so a killed chunk loses at most one.
                    _write_manifest(ROOT, run_id, args, produced, arms, scales, datasets, False)

    # The workspace mount does not permit unlink, so stale cells from earlier
    # sweeps cannot be deleted. A manifest -- not deletion -- is what keeps the
    # analysis honest: analyze_results.py reads ONLY the cells this run produced.
    expected = len(datasets) * len(scales) * len(arms)
    _write_manifest(ROOT, run_id, args, produced, arms, scales, datasets,
                    ok and len(produced) == expected)
    print(f"run_id={run_id} cells={len(produced)}/{expected} complete={ok and len(produced) == expected}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
