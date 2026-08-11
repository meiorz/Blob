# Benchmark methodology

## Environment classification

Every result row carries `environment_class` and `hardware_validated`
(`benchmarks/env_capture.py`). Set via `COMPRESSION_BENCH_ENV`; defaults to the most
conservative value, `sandbox`. **No result produced with `environment_class=sandbox`
may be cited as hardware-validated**, and no "keep" decision may rest on one.

| Class | Meaning |
| --- | --- |
| `sandbox` | Shared 1-physical-core / 3.8 GiB VM. Provisional only. |
| `dev-local` | Developer workstation. Concurrency valid; budget still unverified. |
| `cloud-<shape>` | Node matching the deployment worker shape. Required for a keep decision. |

## NOT MEASURED register (auto-populated per run)

On this host the register self-populates with three entries. These are **deferrals with
named triggers**, not waivers:

1. `concurrency_scaling` — 1 physical core with SMT. SKILL.md forbids comparing across
   concurrency levels on such a host, so all runs are pinned to CPU 0 and single-threaded
   (`pa.set_cpu_count(1)`).
2. `cold_cache` — `/proc/sys/vm/drop_caches` is not writable (unprivileged, uid 1031).
   Rather than fake a cold run, codec benchmarks execute entirely on in-memory Arrow
   buffers, removing I/O from the hot path (permitted by SKILL.md's I/O isolation rule).
3. `absolute_per_worker_memory_budget` — 3.8 GiB total RAM cannot exercise the 4–8 GiB
   target or the 16 GiB hard limit. Only scale-invariant memory metrics
   (`memory_per_input_ratio`, `memory_amplification`, growth class) transfer.

## Measurement conventions

- Byte quantities stored as **integer bytes** in raw JSON; rendered as MiB/GiB only in Markdown.
- Durations stored in **milliseconds**; throughput in **MiB/s on original input bytes**.
- `original_bytes` = **Arrow in-memory table `nbytes`** — the size a scan must materialize.
  Note this makes `compression_ratio` conflate Parquet's encoding layer
  (dictionary/RLE/bit-packing) with the page codec, so
  `codec_attributable_ratio` (candidate ÷ the UNCOMPRESSED Parquet control) is also
  reported and is the quantity the Iteration 1 hypothesis is actually about.
- Timing via `CLOCK_MONOTONIC`; CPU via `CLOCK_PROCESS_CPUTIME_ID` (both 1 ns resolution).
- **10 trials minimum** (shared host ⇒ SKILL.md's noisy-environment rule), plus 30 trials
  for projected decode, which is the query-latency proxy.
- p50/p95/p99 emitted **only where n ≥ 30**; otherwise suppressed with a recorded reason.
- Medians, min, max, stdev and coefficient of variation reported for every series.

## Isolation

One subprocess per cell (`benchmarks/run_cell.py`). This is required, not cosmetic:
`getrusage(ru_maxrss)` is a process-lifetime high-water mark, so running several arms in
one process would make peak RSS meaningless after the first large arm. It also prevents
allocator state and Arrow pool reuse from leaking across arms.

Timing trials run **without** the memory sampler attached; memory is collected in separate
profiled runs, so sampler overhead cannot contaminate latency figures.

`results/raw` is append-only — the workspace mount forbids unlink. A **run manifest**
(`results/latest_manifest.json`) records exactly which cells a sweep produced, and the
analyzer reads only those. Globbing the directory would silently mix arms from different
sweeps, which is precisely the invalid cross-run comparison SKILL.md prohibits.

## Memory profiling

Process level: 5 ms sampler over `/proc/self/statm` with `/proc/self/smaps_rollup` every
8th tick (RSS, USS, PSS, anonymous vs file-backed), plus `getrusage` as cross-check.
Runtime level: Arrow memory-pool `max_memory()`/`bytes_allocated()` — **essential**,
because Arrow allocates off the Python heap and `tracemalloc` alone would report a near-zero
and badly misleading number. `tracemalloc` is still collected for Python-heap attribution.

Arrow's default pool is an arena allocator that retains freed pages by design. Post-run
retention is therefore recorded **twice** — before and after `release_unused()` — so normal
arena behaviour is distinguished from a genuine leak (`arena_retained_bytes`).

Growth class is fitted on the log-log slope of **incremental** peak RSS
(peak − pre-run idle baseline) across three scales. Total RSS carries a fixed ~100–150 MiB
interpreter+Arrow floor that would make every arm look artificially sublinear.
Slope > 1.15 ⇒ superlinear (blocker); < 0.90 ⇒ sublinear; otherwise linear.

## Predeclared decision gates

Fixed **before any data existed**. Implemented in `scripts/analyze_results.py`.

| Gate | Threshold |
| --- | --- |
| G1 projected footprint reduction vs SNAPPY | ≥ 20 % |
| G2 projected decode throughput vs SNAPPY | ≥ 70 % |
| G3 bandwidth model | not worse at 250 MiB/s **and** clear win at 50 and/or 125 MiB/s |
| G4 peak RSS delta vs SNAPPY | ≤ +10 % at every scale |
| G5 post-run RSS vs pre-run idle baseline | ≤ +5 % |
| G6 memory growth class | ≠ superlinear |
| G7 lossless integrity | pass |
| G8 reproducibility | conclusion not an artifact of noise |

**G8 is deliberately not a difference test.** It asks "is the conclusion an artifact of
noise?", not "are the distributions different?". A candidate whose decode latency is
statistically indistinguishable from baseline is a *good* outcome for a footprint-motivated
change. G8 fails only when runs are too unstable to conclude anything (CoV > 20 %) or when
the candidate is genuinely regressed (separated at p < 0.05, slower, and > 5 % worse).
An earlier version required separation and therefore failed candidates for being harmless.

Controls (`none`) are reported but never gated: an uncompressed arm cannot beat Snappy on
footprint, so gating it would manufacture a meaningless failure row.

## Object-store cost model

Codec benchmarks run on local buffers, where a better ratio buys nothing. Bytes and CPU are
therefore measured separately and composed:

    scan_time(B) = projected_compressed_bytes / B + projected_decode_seconds

Crossover bandwidth, solving baseline against candidate:

    B* = (C_snappy − C_zstd) / (D_zstd − D_snappy)

Evaluated at **B ∈ {50, 125, 250} MiB/s** per worker core. The deliverable is `B*` plus the
curve, so the recommendation survives the real bandwidth differing from any single assumption.
