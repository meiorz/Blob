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

### `environment_class` is a label, not a machine identity

It **defaults** to `sandbox` whenever `COMPRESSION_BENCH_ENV` is unset, so two genuinely
different machines carry the same label and a guard keyed on it alone passes the mix
through. This repo hit exactly that: cells measured on `Linux-6.8` and on
`Windows-10` both recorded `environment_class=sandbox`, with encode medians differing by
more than 120 %.

Every cell is therefore also compared on its **host fingerprint** — `env.os`,
`env.cpu_model`, `env.mem_total_bytes` (`env_capture.HOST_FINGERPRINT_FIELDS`).
`analyze_results.py` and `scripts/verify_run_integrity.py` both call
`env_capture.mixed_host_report()` and **refuse to proceed** when a manifest's cells span
more than one fingerprint, naming the offending cells. `hostname` and `cpu_affinity` are
deliberately excluded: they vary legitimately between cells on one machine (container
restarts, per-cell CPU pinning).

## NOT MEASURED register (auto-populated per run)

Recorded per cell in `env.not_measured`. These are **deferrals with named triggers**, not
waivers.

Each entry states its **cause**, because the cause decides whether hardware can retire it:

| Cause | Meaning |
| --- | --- |
| `harness design` | The reason is in the code. **Unconditional** — present on every host, and no machine retires it. `retires_on_better_hardware: false`. |
| `host capability` | The reason is this machine. A different machine may retire it. `retires_on_better_hardware: true`. |

**Host capability may ADD an entry; it may never remove one the code guarantees.** The
register was previously conditional throughout, so on a 4–8 core node the concurrency
caveat disappeared while `run_cell.py` went on calling `pa.set_cpu_count(1)` and measuring
exactly one thread. A register that shrinks on a bigger machine reads as "we measured more
this time".

### Unconditional — `harness design`

1. `concurrency_scaling` — `benchmarks/run_cell.py` pins to a single CPU and hardcodes
   `pa.set_cpu_count(1)` / `set_io_thread_count(1)`, so every cell is single-threaded by
   construction and no run varies concurrency. SKILL.md forbids comparing across
   concurrency levels; here there is nothing to compare. Retiring this needs a harness
   change, not a bigger host.
2. `cold_cache` — codec benchmarks run entirely on preloaded in-memory Arrow buffers with
   no disk on the hot path (permitted by SKILL.md's I/O isolation rule), so neither a cold
   nor a warm cache is exercised. Root access to `/proc/sys/vm/drop_caches` would not
   change this. End-to-end object-store cost is modelled analytically by
   `benchmarks/model_crossover.py` instead.

### Conditional — `host capability`

3. `absolute_per_worker_memory_budget` — emitted below 16 GiB `MemTotal`; the 4–8 GiB
   per-worker target and 16 GiB hard limit cannot be exercised. Only scale-invariant memory
   metrics (`memory_per_input_ratio`, `memory_amplification`, growth class) transfer.
   **Genuinely retires on a larger node.**
4. `process_memory_profiling` — **a hard stop rather than a deferral.** Process-level
   sampling in `benchmarks/memory_profiler.py` is implemented entirely against the Linux
   `/proc` filesystem (`/proc/self/statm` for RSS, `/proc/self/smaps_rollup` for
   USS/PSS/anonymous); there is no Windows or macOS backend. Where this entry appears,
   **no benchmark cell can be produced at all**: `profile_memory` raises
   `MemoryProfilerUnavailable` before the operation runs, and `run_cell.py` /
   `orchestrate.py` refuse to record. G4/G5/G6 are `UNEVALUABLE`. `capture()` also records
   `process_memory_sampling_available` so an unsupported host says so before a sweep is
   attempted. **Genuinely retires on any Linux host.**
5. `concurrency_scaling_host_constraint` — emitted when the host has SMT active or fewer
   than 2 physical cores. This *narrows why* a concurrency comparison would be invalid
   here; it does not make entry 1 conditional. Entry 1 stands on every host regardless.

Entries 3 and 4 are the only two a better machine retires.

**Shape.** Since 2026-08-13 each entry is an object —
`{id, cause, retires_on_better_hardware, detail}`. Cells recorded before that date carry a
flat list of strings. Readers must tolerate both; recorded cells are append-only and were
not rewritten.

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

## Dataset provenance in a result row

Two distinct fields, not one:

- **`dataset_id`** — a human label (`D1-clickbench-hits`). Not a hash; not unique across
  file revisions.
- **`dataset_sha256`** — **SHA-256 of the on-disk Parquet file**, 64 lowercase hex chars.
  This is the field that identifies *which bytes* were measured. `run_cell.py` takes it
  from the catalog when present and otherwise computes it with `env_capture.sha256_file`.

Source files are not committed; the catalog's `file` + `source` + `sha256` triple is the
whole provenance chain, so a wrong or truncated digest breaks it.

> **Known defect, corrected 2026-08-11.** Every cell in run `20260811T072000Z-g5fix` carries
> a **32-char** `dataset_sha256` — a truncated prefix, because the hand-written catalog
> supplied it and `run_cell.py` prefers a supplied value over recomputing. The catalog now
> holds the full 64-char digests, and each old prefix matches, so the run's dataset identity
> is confirmed. The raw cells were **not** rewritten: they are recorded evidence. Treat a
> 32-char digest in `results/raw` as "pre-correction", not as a different algorithm.

## Memory profiling

Process level: 5 ms sampler over `/proc/self/statm` with `/proc/self/smaps_rollup` every
8th tick (RSS, USS, PSS, anonymous vs file-backed), plus `getrusage` as cross-check.
Runtime level: Arrow memory-pool `max_memory()`/`bytes_allocated()` — **essential**,
because Arrow allocates off the Python heap and `tracemalloc` alone would report a near-zero
and badly misleading number. `tracemalloc` is still collected for Python-heap attribution.

Arrow's default pool is an arena allocator that retains freed pages by design. Post-run
retention is therefore recorded **twice** — before and after `release_unused()` — so normal
arena behaviour is distinguished from a genuine leak (`arena_retained_bytes`).

### Unmeasured memory fails; it never passes

**Corrected 2026-08-13 after three gates reported green on a host that measured nothing.**

The profiler had no platform guard. Off Linux every `/proc` read failed and the metrics
were written as zeros, which the gates could not distinguish from a measurement:

| Gate | Arithmetic on an all-zero block | Reported |
| --- | --- | --- |
| G4 | `100*(0-0)/0`, guarded to `0.0` → `0.0 ≤ +10` | **PASS** |
| G5 | short-circuits to `0.0` → `0.0 ≤ +5` | **PASS** |
| G6 | no usable scales → `"insufficient_scales" != "superlinear"` | **PASS** |

Cells carrying those zeros were recorded into `results/raw`. The rule now holds at three
independent points, because `results/raw` is append-only and a bad cell cannot be withdrawn:

1. **Profiler** — `process_sampling_support()` probes by reading the counter (not by
   inspecting `sys.platform`, so a container with `/proc` masked is caught too).
   `profile_memory.__enter__` raises `MemoryProfilerUnavailable` *before* the operation
   runs. `_rss_now()` returns `None`, never `0`, when a read fails.
2. **Writers** — `run_cell.py` refuses to emit and `orchestrate.py` refuses to record a
   cell whose sampler collected zero samples, has a zero peak, or a zero baseline.
3. **Analyser** — `analyze_results.py` marks G4/G5/G6 `unevaluable: true, pass: false`
   and lists them under `UNEVALUABLE` in `results/summary.json`, separately from genuine
   failures. A growth class of `insufficient_scales` or `undetermined` is likewise
   unevaluable: "not superlinear" must mean *measured and not superlinear*, not
   *we could not tell*.

All three share one predicate, `memory_profiler.memory_block_problems()`, so the writer
and the reader cannot drift apart on what "measured" means.

**UNEVALUABLE and FAIL both block a keep, and they are different findings.** A failure is
fixed by changing the codec; an unevaluable gate is fixed by changing the host.

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

**A gate has three outcomes, not two.** `pass`, `fail`, and `unevaluable` — the last when
the data a gate needs was never collected. An unevaluable gate sets `pass: false` and is
listed in `summary.json`'s per-comparison `UNEVALUABLE` array. Missing data can never
satisfy a gate; see *Unmeasured memory fails* above.

## Object-store cost model

Codec benchmarks run on local buffers, where a better ratio buys nothing. Bytes and CPU are
therefore measured separately and composed:

    scan_time(B) = projected_compressed_bytes / B + projected_decode_seconds

Crossover bandwidth, solving baseline against candidate:

    B* = (C_snappy − C_zstd) / (D_zstd − D_snappy)

Evaluated at **B ∈ {50, 125, 250} MiB/s** per worker core. The deliverable is `B*` plus the
curve, so the recommendation survives the real bandwidth differing from any single assumption.

---

## Real-hardware rerun

Operator runbook: how to execute a sweep on a real node. **Which** runs to perform and when
is defined only in `docs/ROADMAP.md`; the command blocks below are copied from it. Where a
block and the code disagree, the roadmap wins and the disagreement is called out inline.

A copyable per-run checklist, in SKILL.md's iteration-entry format, is at the end of
`docs/iteration-log.md`.

### 1. Environment label — `COMPRESSION_BENCH_ENV`

Read by `benchmarks/env_capture.py`, written into every cell, and the basis of
`hardware_validated`:

```python
"hardware_validated": ENVIRONMENT_CLASS not in ("sandbox",)
```

| Value | `hardware_validated` | Implication |
| --- | --- | --- |
| unset | `false` | Defaults to `sandbox`. This is the safe default and why forgetting the variable cannot silently produce a validated-looking result. |
| `sandbox` | `false` | Explicitly the conservative label. |
| `dev-local` | `true` | Developer workstation. |
| `cloud-<shape>` | `true` | Node approximating the deployment worker shape. |

**Two traps.**

*Any* value other than the exact string `sandbox` flips `hardware_validated` to `true` —
including a typo. `COMPRESSION_BENCH_ENV=Sandbox` (capital S) produces cells claiming
hardware validation. Echo the value back before the sweep:

```bash
export COMPRESSION_BENCH_ENV=cloud-<shape>
python3 benchmarks/env_capture.py | head -20     # confirm the label and the register
```

And the label is not a machine identity — see *`environment_class` is a label* above. Two
different machines both labelled `cloud-8x32` are still two machines; the host fingerprint
is what separates them, and `scripts/verify_run_integrity.py` is what checks it.

### 2. Data location — `COMPRESSION_BENCH_DATA_ROOT`

Defaults to `<repo>/data/raw`. `benchmarks/orchestrate.py` resolves each catalog entry's
`file` field beneath it and fails loudly — naming the missing file and its `source` URL —
rather than skipping the dataset. Source files are never committed; the catalog's
`file` + `source` + `sha256` triple is the whole provenance chain.

```bash
export COMPRESSION_BENCH_DATA_ROOT=/mnt/data/parquet
sha256sum "$COMPRESSION_BENCH_DATA_ROOT"/*.parquet   # must match data/metadata/catalog.json
```

Verify the digests **before** the sweep. A wrong file produces a complete, plausible,
mislabelled run.

### 3. `COMPRESSION_BENCH_SKIP_HEAVY` must be unset

It does not skip one test. It skips the **entire hostile-input suite**, because
`tests/test_suites.py` exposes that suite through a single pytest surface and the flag
decorates it. A run with the flag set reports green having executed zero security tests.

It exists as an escape hatch for memory-constrained *local* hosts — a real node has the
headroom, so there is no reason to set it there. `scripts/loop.py run` actively unsets it
before the suites rather than merely not setting it; CI never sets it.

```bash
unset COMPRESSION_BENCH_SKIP_HEAVY
env | grep COMPRESSION_BENCH_      # confirm what is actually exported
```

### 4. Raise `scale_bytes` — and record that you did

The sandbox `L` scale was capped by that host's RAM, not chosen for the experiment. On a
16–32 GiB node, raise `scale_bytes` in `data/metadata/catalog.json`; leaving it wastes the
one thing real hardware buys.

Raising it **changes the measurement**. Cells at the new scale are not comparable to sandbox
cells at the old one — that is a different input size, not a different machine running the
same test. So record the change in three places, in the same commit as the run:

1. `data/metadata/catalog.json` — the new `scale_bytes` values.
2. `docs/dataset-catalog.md` — old → new, and the node's memory that justifies it.
3. The `docs/iteration-log.md` entry for the run — under **Dataset**, so a reader of the
   entry alone can see the scales differ from the earlier run's.

### 5. Run

Copied from `docs/ROADMAP.md` Milestone 1.2. Common setup first:

```bash
export COMPRESSION_BENCH_ENV=cloud-<shape>
python3 tests/test_correctness.py
python3 tests/test_hostile_inputs.py
```

Then the sweep:

```bash
COMPRESSION_BENCH_ENV=cloud-<shape> python3 benchmarks/orchestrate.py \
    --catalog data/metadata/catalog.json \
    --trials 10 --scales S,M,L \
    --arms none,snappy,zstd-3,gzip-6
python3 scripts/analyze_results.py
```

**Where the roadmap's block and the harness disagree — the roadmap wins, and here is the
gap to close.** The block omits `--run-id`, so `orchestrate.py` generates a timestamped one;
and `analyze_results.py` writes a fixed `results/summary.json` that the next sweep
overwrites. The roadmap's own acceptance criteria for this milestone already require each
run under its own manifest with its summary archived by run id, so the block is incomplete
rather than wrong. Supply the id and archive immediately:

```bash
    ... --run-id <run-id>
python3 scripts/analyze_results.py
cp results/summary.json results/summary_<run-id>.json
```

`scripts/loop.py run` does both automatically and is the supported path when the run belongs
to a locked iteration.

A full sweep may exceed a session; `--resume` skips cells already recorded **for the same
run id**. Cells from a different run id are deliberately not trusted, because a harness
change invalidates every cell produced before it.

### 6. Verify the run before concluding anything from it

```bash
python3 scripts/verify_run_integrity.py
```

This asks whether the cells are a valid population — one host, memory actually measured,
manifest intact, digests consistent, nothing modified — which is a different question from
what the numbers say. If it fails, see `docs/architecture.md` § *Damaged evidence*.

### 7. Which NOT-MEASURED entries the move retires

Exactly two, and the register now says which by itself — check `cause` and
`retires_on_better_hardware` on each entry rather than inferring from its absence.

| Entry | Cause | Retired by the move? |
| --- | --- | --- |
| `process_memory_profiling` | host capability | **Yes.** Any Linux host restores `/proc`-based sampling. This is the entry that gates whether cells can be recorded at all. |
| `absolute_per_worker_memory_budget` | host capability | **Yes**, once `MemTotal` ≥ 16 GiB — and only then is raising `scale_bytes` (step 4) meaningful. |
| `concurrency_scaling` | harness design | **No.** `run_cell.py` still pins to one CPU and sets `pa.set_cpu_count(1)`, so concurrency is not varied on any host. Unconditional by construction. |
| `cold_cache` | harness design | **No.** Codec benchmarks still run on in-memory buffers with no disk on the hot path. Unconditional by construction. |
| `concurrency_scaling_host_constraint` | host capability | **Yes** on a multi-core non-SMT node — but it only ever narrowed *why* a comparison would be invalid here. Its disappearance does not retire `concurrency_scaling`. |

Check what the node actually claims rather than assuming:

```bash
python3 benchmarks/env_capture.py     # prints the register this host will record
```

`concurrency_scaling` and `cold_cache` are **unconditional** — their cause is in the harness,
so they appear on every host and a bigger node cannot remove them. If either is ever absent
from a recorded register, that is a harness change, not a better machine; say which change
in the iteration-log entry under **Environment**.
