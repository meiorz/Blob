# Harness architecture

```md
orchestrate.py ──spawns──> run_cell.py (one FRESH process per cell)
      │                          │
      │                          ├── env_capture.py    provenance + NOT MEASURED register
      │                          ├── parquet_bench.py  the measured cell
      │                          └── memory_profiler.py 5 ms sampler + Arrow pool + tracemalloc
      │
      └──writes──> results/raw/*.json + results/latest_manifest.json
                            │
                            └──> analyze_results.py ──> results/summary.json, docs/results.md
                                        │
                                        └── model_crossover.py   bandwidth curve + B*
security/safe_decompress.py  <── tests/test_hostile_inputs.py
                             <── production read path for untrusted input
```

## Why one process per cell

`getrusage(ru_maxrss)` is a process-lifetime high-water mark. Running several arms in one
process would make peak RSS meaningless after the first large arm — every later arm would
inherit the largest earlier peak. Fresh processes also stop allocator state and Arrow pool
reuse from leaking across arms, which would otherwise make arm ordering affect results.

## Why timing and memory are separate runs

The 5 ms sampler thread costs CPU and perturbs latency. Timing trials run with no sampler
attached; memory is collected in dedicated profiled runs. Mixing them would make every
latency number quietly wrong.

## Why codec benchmarks avoid disk

This host cannot drop page cache (unprivileged), so a "cold" disk run is unobtainable and a
warm one would silently measure page cache. Writing to and reading from in-memory Arrow
buffers removes the confound rather than faking it, and matches SKILL.md's instruction to
keep storage I/O off the hot path unless storage is the metric under test. End-to-end
object-store cost is handled analytically by `model_crossover.py` instead.

## Why a run manifest instead of cleanup

The workspace mount forbids unlink, so `results/raw` is append-only. Globbing it would mix
arms from different sweeps — an invalid cross-run comparison. `analyze_results.py` reads
only the cells named in `results/latest_manifest.json`.

## Single-variable discipline

`parquet_bench.WRITER_FIXED` pins writer version, data page size, statistics and dictionary
usage; `ROW_GROUP_BYTES_TARGET` pins row-group sizing; row and column order are untouched.
The only thing `CODEC_ARMS` varies is the page codec. Anything that needs to change one of
the pinned values belongs in a later iteration, not this one.
