# Iteration log

Append-only narrative, newest entries appended at the end. A reusable pre-flight checklist
and blank entry for a real-hardware run is at the bottom of this file.

## Iteration 1: Parquet page codec SNAPPY -> ZSTD-3 (COMPLETE — provisional, compat-blocked)

- **Date:** 2026-08-11
- **Environment:** `sandbox` (1 physical core + SMT, 3.8 GiB RAM, Ubuntu 22.04, Python 3.10.12,
  pyarrow 25.0.1, libzstd 1.5.7). `hardware_validated=false` on all 24 rows.
- **Run id:** `20260811T072000Z-g5fix`, 24/24 cells, 10 trials (30 for the latency proxy),
  single-threaded pinned to CPU 0.
- **Hypothesis:** If we change the Parquet page codec from SNAPPY to ZSTD-3 (writer-side only,
  all other writer settings held constant) then projected-column footprint improves by >=20% on
  workload P1, because Parquet applies dictionary/RLE/bit-packing before the page codec and the
  surviving residue is dominated by skewed symbol distributions that Zstd's entropy stage can
  code below 8 bits per literal, while Snappy has no entropy stage.
- **Dataset:** D1 ClickBench `hits` (1,000,000 rows x 105 cols, sha256 fa134fe1...) and
  D2 NYC TLC yellow 2024-01 (2,964,624 rows x 19 cols, sha256 c4d59da7...). Acquired 2026-08-10
  by user-approved browser download. **Both are proxies, not production data.**
- **Baseline:** SNAPPY, Parquet v1.0 pages, 1 MiB data pages, 128 MiB row-group target,
  dictionary on, statistics on. Byte-identical writer config across all 24 cells (verified).
- **Change:** page codec only, swept {none, snappy, zstd-3, gzip-6}.
- **Commands run:**
  - `python3 benchmarks/orchestrate.py --catalog data/metadata/catalog.json --trials 10 --scales S,M,L --arms none,snappy,zstd-3,gzip-6 --run-id 20260811T072000Z-g5fix --resume`
  - `python3 scripts/analyze_results.py`
  - `python3 tests/test_correctness.py` / `tests/test_hostile_inputs.py`
- **Results:**
  - D1 projected footprint: **-38.1 / -38.4 / -38.7 %** (S/M/L) vs Snappy.
  - D1 projected decode throughput: **1.023 / 1.023 / 1.075x** Snappy (Zstd FASTER).
  - D1 modeled scan time at 50/125/250 MiB/s: -32.6/-26.9/-21.2 % (S) to -34.2/-29.5/-24.4 % (L).
    No crossover; Zstd dominates at every bandwidth.
  - D2 projected footprint: **-20.7 / -21.1 / -21.4 %**.
  - D2 projected decode throughput: **0.842 / 0.873 / 0.882x** (Zstd 13-19 % SLOWER).
  - D2 crossover B* = **628 / 788 / 868 MiB/s**; still net-positive at all three evaluated points.
  - Codec-attributable ratio: D1 snappy 3.20x vs zstd 5.04x; D2 snappy 1.24x vs zstd 1.61x.
  - Gzip-6: -34.8 % (D1) footprint but 0.40-0.56x decode throughput; fails G2/G3 everywhere.
- **Correctness result:** 10/10 PASS. 9 edge cases x 4 arms lossless; all arms byte-deterministic;
  all arms decode to identical values; already-compressed control ~1.000x.
- **Security result:** 9/9 PASS. Bomb rejected in 5 ms at +0.0 MiB RSS; 120 fuzz cases,
  0 crashes/OOM/timeouts. Findings F-1/F-2/F-3 fixed.
- **Memory baseline (decode, Snappy):** D1 648 / 928 / 1246 MiB peak RSS at S/M/L.
- **Memory candidate (decode, Zstd-3):** D1 659 / 897 / 1220 MiB.
- **Peak RSS delta:** D1 +1.8 / -3.4 / -2.1 %; D2 -3.4 / -0.8 / -0.8 %. All within the +10 % gate.
- **Post-run retained memory:** -3.4 % to +0.8 % of pre-run baseline; within the +5 % gate.
  Arena retention recorded separately from genuine retention.
- **Growth classification:** **linear** for every dataset/arm (slopes 0.993-1.114). No superlinear
  growth anywhere.
- **Decision:** **provisional keep for D1-shaped tables, compat-blocked. Not accepted for
  D2-shaped tables.**
  - D1: all eight gates pass at all three scales. Cannot advance past "provisional" because the
    compatibility gate is unsatisfied and the environment is `sandbox`.
  - D2: G8 fails on a genuine decode regression (+13 to +19 % median, p <= 5e-08). The 21 %
    footprint win is real and modeled scan time still favours Zstd below ~630-870 MiB/s, so this
    is a bandwidth-dependent call, not a blanket no.
- **Reason:** The hypothesis is supported on the workload it targeted and the stated mechanism
  survived its falsification test — footprint reduction on D1 held at 38.1/38.4/38.7 % while
  row-group count went from 1 to ~4, so the gain is page-local entropy coding and not a window
  effect. The D1/D2 split is the mechanism showing itself: where Parquet's own encodings have
  already removed the redundancy (numeric D2), the page codec has little left to work with.
- **Next hypothesis:** (2) Zstd level sweep crossed with data-page size — the direct test of the
  entropy-vs-window claim, and the natural way to recover D2's decode regression. (3) Row
  ordering / clustering as preconditioning, which SKILL.md flags as a first-class target and which
  plausibly exceeds the codec swap in magnitude. Before either: re-run on real hardware and on at
  least one real production partition.

### Harness defects found and fixed during this iteration

1. Catalog key collision: descriptive `columns: 105` was passed to `Table.select()`; every cell
   failed. Renamed to `num_columns` and `run_cell` now rejects a non-list `columns` explicitly.
2. Memory measurement invalid: the full table was read then sliced and held alive, so
   `baseline_rss` already contained the dataset. Replaced with `load_scaled_table()`
   (read only needed row groups, slice before combine, drop refs, `release_unused`).
3. G5 measured the harness's own reference: the profiled operation's output was still alive at
   context exit, so post-run retention failed for every arm. Output is now freed inside the
   profiled block.
4. Two orchestrators ran concurrently on one core after a `setsid` launch survived unexpectedly;
   all affected cells were discarded and the sweep restarted under a fresh run id.
5. `pkill -f orchestrate.py` matched the invoking shell's own command line and killed it.
6. Background processes do not survive the sandbox's per-call boundary; the sweep now runs in
   resumable chunks with the manifest persisted after every cell.

### Prior state (2026-08-10): infrastructure complete, measurement blocked

## Iteration 1 (prior): BLOCKED on dataset acquisition

- **Date:** 2026-08-10
- **Environment:** `sandbox` (1 physical core + SMT, 3.8 GiB RAM, Ubuntu 22.04,
  Python 3.10.12, pyarrow 25.0.1, libzstd 1.5.7). **Not hardware-validated.**
- **Hypothesis:** If we change the Parquet page-compression codec from SNAPPY to ZSTD
  level 3 — writer-side only, with row-group size, data-page size, writer version,
  dictionary encoding, column order and row ordering all held constant — then compressed
  footprint of the projected column subset will improve by ≥20% on workload P1, because
  Parquet applies dictionary, RLE and bit-packing *before* the page codec, and the residual
  redundancy that survives those encodings is dominated by skewed symbol distributions that
  Zstd's entropy stage (Huffman/FSE) can code below 8 bits per literal, whereas Snappy has
  no entropy stage at all.
  *Mechanism precision:* Parquet compresses each ~1 MiB page independently, so Zstd's
  larger window contributes almost nothing here. Any explanation leaning on "bigger window"
  is wrong for this format. Falsifiable counter-prediction: if the gain tracks row-group
  size rather than page size, the stated mechanism is wrong and will be reported as such.
- **Dataset:** **NOT ACQUIRED.** Approved proxies (ClickBench `hits`, NYC TLC yellow taxi)
  are unreachable from the sandbox — egress allowlist permits only PyPI, github.com and npm.
  Full reachability evidence in `docs/dataset-catalog.md`.
- **Baseline:** Not established. Blocked on dataset.
- **Change:** Not applied. `CODEC_ARMS = {none, snappy, zstd-3, gzip-6}` is implemented and
  smoke-tested; only the page codec varies.
- **Commands run:**
  - `python3 benchmarks/env_capture.py`
  - `python3 benchmarks/orchestrate.py --catalog /tmp/smoke_catalog.json --trials 3` (smoke only)
  - `python3 scripts/analyze_results.py`
  - `python3 tests/test_hostile_inputs.py`
  - `python3 tests/test_correctness.py`
  - `python3 scripts/probe_reader_support.py --write /tmp/compat && --read /tmp/compat`
- **Results:** None. No compression measurement has been performed on approved data, so no
  ratio, throughput or footprint number is claimed.
- **Correctness result:** **10/10 PASS** (dataset-independent). 9 edge cases × 4 arms all
  lossless (empty, single-row, all-null, repeated, incompressible, 4-byte Unicode, 4 MiB
  single string, 512-column row, mixed nulls incl. inf/-0.0). All four arms byte-identical
  across repeated writes. All arms decode to identical values. Already-compressed control:
  none 1.0000×, snappy 1.0000×, zstd-3 1.0000×, gzip-6 0.9976× — no gain available on
  pre-compressed bytes.
- **Security result:** **9/9 PASS**. Three real defects found and fixed:
  - F-1 `ZstdDecompressor.decompress(max_output_size=N)` does not cap frames that declare a
    content size — a 32,787 B frame declaring 1 GiB decompressed fully under a 16 MiB cap.
  - F-2 `ZstdDecompressionObj.decompress()` bounds input, not output — one 32 KiB chunk
    produced 1 GiB in a single call (~4.7 s) before any limit could fire.
  - F-3 `stream_reader` returns truncated frames as clean short reads — unsafe partial
    extraction.
  Final decoder rejects the 32,749× bomb in **5 ms at +0.0 MiB peak RSS**, and refuses to
  return partial output for any truncated or corrupt frame. Fuzzing: 120 mutation cases
  under `RLIMIT_AS` 512 MiB / 20 s → 55 rejected, 65 read, **0 crashes, 0 OOM, 0 timeouts**.
- **Memory baseline:** Not established (blocked on dataset).
- **Memory candidate:** Not established.
- **Peak RSS delta:** n/a.
- **Post-run retained memory:** Instrumented, recorded twice — before and after Arrow
  `release_unused()` — so arena retention is distinguished from a genuine leak.
- **Growth classification:** n/a. Classifier implemented (log-log slope of *incremental*
  peak RSS over three scales) and validated against the smoke run, which correctly reported
  `insufficient_scales` with per-point reasons rather than fabricating a class.
- **Decision:** **revise** — hold Iteration 1 at the dataset gate. Do not proceed to
  measurement until the dataset question is resolved. Substituting a reachable dataset
  silently would violate the approved plan and the "representative data" rule.
- **Reason:** The approved datasets are unreachable and the only offline alternative
  (TPC-H) has no high-cardinality string columns — precisely the column class the
  hypothesis mechanism targets. A null result on TPC-H would be uninformative about P1,
  and a positive one would not generalize.
- **Next hypothesis:** Unchanged. Iteration 1 executes as written once data is available.
  Queued for later iterations, deliberately excluded now to preserve single-variable
  discipline: (2) Zstd level sweep and data-page-size interaction — the direct test of the
  entropy-vs-window mechanism claim; (3) row ordering / clustering as a preconditioning
  step, which SKILL.md flags as a first-class optimization target and which likely exceeds
  the codec swap in magnitude.

### Harness defects found by the smoke test (fixed before any real run)

1. The `none` control was being evaluated against the accept gates, manufacturing a
   guaranteed failure row. Controls are now reported but never gated.
2. Gate G8 required *statistical separation* of latency distributions, so a candidate that
   was harmless (indistinguishable from baseline) failed. G8 now asks whether the conclusion
   is a noise artifact, and fails only on instability or a genuine regression.
3. Growth classification reported `insufficient_scales` without saying why. It now reports
   which scale points were dropped and the reason.
4. Post-run retention conflated Arrow arena behaviour with a leak. Retention is now measured
   before and after `release_unused()`.

---

# Reusable checklist — real-hardware run

Copy the two blocks below into a new entry at the end of this file, one entry per run. The
procedure they enforce is in `docs/benchmark-methodology.md` § *Real-hardware rerun*; which
runs to perform is in `docs/ROADMAP.md`. This is a form to fill in, not a plan.

## Pre-flight

Tick every line **before** starting the sweep. Each one is cheap now and expensive after a
multi-hour run.

```txt
[ ] COMPRESSION_BENCH_ENV exported, and echoed back to confirm the exact string.
    Anything other than "sandbox" sets hardware_validated=true, including a typo.
[ ] COMPRESSION_BENCH_DATA_ROOT points at the datasets; sha256sum matches catalog.json
    for every file that will be read.
[ ] COMPRESSION_BENCH_SKIP_HEAVY is UNSET (`env | grep COMPRESSION_BENCH_`).
[ ] `python3 benchmarks/env_capture.py` reviewed: environment_class, host fingerprint,
    process_memory_sampling_available, and the NOT-MEASURED register as this node reports it.
[ ] scale_bytes decision made: raised for this node, or deliberately left. If raised,
    catalog.json + dataset-catalog.md updated in the same commit as the run.
[ ] A --run-id chosen. Without one the summary of the previous run is overwritten.
[ ] Correctness and hostile-input suites run and green on THIS node, before the sweep.
```

## Post-run

```txt
[ ] `python3 scripts/verify_run_integrity.py` passes for the new manifest.
[ ] results/summary.json archived to results/summary_<run-id>.json.
[ ] Any NOT-MEASURED entry that disappeared on this node is named below, with whether the
    quantity is now measured or merely no longer reported.
[ ] Entry below completed. Decision left to a human.
```

## Entry template

SKILL.md's `docs/iteration-log.md` format, with the fields a hardware run must additionally
pin down. Do not delete a field — record "n/a" and why.

```md
## Iteration <N>: <short title> (<environment_class>)

- **Date:**
- **Environment:** `<environment_class>`; host: <os>, <cpu_model>, <physical cores>,
  <MemTotal>. hardware_validated=<value as recorded in the cells>.
  worker_shape_attested=<true|false>.
- **NOT-MEASURED register on this node:** <entries recorded>. Retired since the previous
  run: <entries>, of which <these> are genuinely measured now and <these> are merely no
  longer reported (harness still does not vary them).
- **Run id:** `<run-id>`, <n>/<n> cells, <trials> trials. Manifest:
  `results/manifest_<run-id>.json`. Summary: `results/summary_<run-id>.json`.
- **Hypothesis:**
- **Dataset:** <ids>, sha256 <prefixes>. scale_bytes: <values> — <unchanged from run X |
  raised from Y, which makes these cells non-comparable to that run's>.
- **Baseline:**
- **Change:** <what differs from the run this is compared against — environment, dataset,
  or configuration. Name exactly one where possible.>
- **Command(s) run:**
- **Results:**
- **Correctness result:**
- **Security result:** <COMPRESSION_BENCH_SKIP_HEAVY was unset; suite fully executed.>
- **Memory baseline:**
- **Memory candidate:**
- **Peak RSS delta:**
- **Post-run retained memory:**
- **Growth classification:**
- **Run integrity:** `verify_run_integrity.py` <pass|fail + which checks>.
- **Decision:** keep / revise / reject
- **Reason:**
- **Next hypothesis:**
```
