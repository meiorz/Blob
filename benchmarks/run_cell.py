"""Run ONE benchmark cell in a fresh process.

Fresh-process isolation is required, not cosmetic: getrusage(ru_maxrss) is a
process-lifetime high-water mark, so running multiple cells in one process
would make peak-RSS meaningless after the first large arm. It also prevents
allocator state and Arrow pool reuse from leaking between arms.

Usage:  python3 run_cell.py '<json-config>'   -> JSON result on stdout
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    cfg = json.loads(sys.argv[1])

    # Pin before importing pyarrow so any pool threads inherit the affinity.
    pin = cfg.get("pin_cpu")
    sched_setaffinity = getattr(os, "sched_setaffinity", None)
    if pin is not None and sched_setaffinity is not None:
        sched_setaffinity(0, {int(pin)})
    os.environ.setdefault("OMP_NUM_THREADS", "1")

    import pyarrow as pa
    import pyarrow.parquet as pq
    pa.set_cpu_count(1)          # single-threaded: concurrency is NOT MEASURED here
    pa.set_io_thread_count(1)

    import parquet_bench as pb
    from env_capture import capture, sha256_file

    src = cfg["source_path"]
    cols = cfg.get("columns")
    if cols is not None:
        if not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):  # noqa
            raise TypeError(
                f"config key 'columns' must be a list of column names to select, got "
                f"{type(cols).__name__}={cols!r}. Descriptive counts belong in 'num_columns'.")
        pass
    table = pb.load_scaled_table(src, int(cfg["scale_bytes"]), columns=cols)
    proj = cfg["projection"]
    missing = [c for c in proj if c not in table.schema.names]
    if missing:
        raise KeyError(f"projection columns not present in {cfg['dataset_id']}: {missing}")

    result = pb.run_cell(
        table=table,
        arm=cfg["arm"],
        projection=cfg["projection"],
        trials=int(cfg.get("trials", 10)),
    )
    result.update({
        "dataset_id": cfg["dataset_id"],
        "dataset_sha256": cfg.get("dataset_sha256") or sha256_file(src),
        "scale_label": cfg["scale_label"],
        "scale_bytes_target": int(cfg["scale_bytes"]),
        "thread_count": 1,
        "pinned_cpu": pin,
        "env": capture(),
    })
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
