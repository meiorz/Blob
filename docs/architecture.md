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

## Damaged evidence

Operator runbook for a run that went wrong. Recorded cells are evidence, so the response to a
bad one is never to correct it — it is to *supersede* it and leave a trail explaining why.

### Telling a damaged cell from a valid one

Not by eye. A damaged cell is usually complete, well-formed and plausible; what is wrong is
the population it belongs to.

```bash
python3 scripts/verify_run_integrity.py                                  # latest manifest
python3 scripts/verify_run_integrity.py --manifest results/manifest_<id>.json
python3 scripts/verify_run_integrity.py --all-manifests
```

Seven checks: every named cell exists and parses; all cells came from **one host**; one
`environment_class`; memory actually measured; one `dataset_sha256` per `dataset_id`; no
tracked cell modified; cells match the manifest's declared design.

Three of those catch damage that reads as a normal result:

- **Mixed hosts.** `environment_class` defaults to `sandbox` everywhere, so cells from two
  machines carry the same label and look like one population. The host fingerprint
  (`env.os`, `env.cpu_model`, `env.mem_total_bytes`) is what separates them.
- **Unmeasured memory.** A cell whose sampler collected nothing records zeros, and zeros are
  arithmetically indistinguishable from a measurement of nothing.
- **Modified cells.** A recorded cell that differs from its committed version means a later
  run overwrote it — see *superseding a manifest* below.

Run it before drawing a conclusion, not after. It answers "are these cells a valid
population?", which is prior to "what do these cells say?".

### Why `results/raw` is never edited or deleted

The tree is the record of what was actually observed. Editing a cell to correct it destroys
the only evidence that the original measurement happened, and deleting one makes a manifest
that references it unresolvable — the run silently becomes unreproducible instead of visibly
broken. A wrong cell that is *labelled* wrong is strictly more useful than a missing one.

This is also enforced mechanically rather than by convention: `scripts/loop.py publish` and
`verify_run_integrity.py` both fail on `git diff HEAD -- results/raw` being non-empty, and CI
runs the latter. If a cell has already been overwritten in the working tree, restore it from
the committed version rather than re-running to "fix" it:

```bash
git diff --name-only HEAD -- results/raw     # what was overwritten
git checkout HEAD -- results/raw/<cell>.json # restore the recorded evidence
```

### Quarantining a bad run

A run is quarantined by making sure nothing points at it. Cells stay on disk; what changes is
reachability.

1. **Never let it be `latest_manifest.json`.** The analyser reads only that file. If the bad
   run is currently latest, re-point it by re-running the analysis for a known-good run id,
   or restore the previous `latest_manifest.json` from git.
2. **Rename nothing.** `results/manifest_<bad-id>.json` stays exactly as written — it is the
   record of what that run produced.
3. **Mark it in the manifest's own directory**, not inside the manifest: add
   `results/manifest_<bad-id>.QUARANTINED.md` naming the run id, the failing
   `verify_run_integrity.py` checks, and the date. Editing the manifest itself would make it
   disagree with what the sweep actually wrote.
4. **Do not archive its `summary.json`.** If one was already archived, leave it and note it
   in the quarantine file — a summary computed over a damaged population is itself evidence
   of the incident.

A quarantined run is not deleted, not hidden, and not re-analysed. It is unreachable from any
analysis and explained.

### When a manifest names overwritten cells

`orchestrate.py` writes each cell to `<dataset>__<scale>__<arm>.json` — a path derived from
the design, not from the run id. Two runs covering the same triple therefore write the **same
file**, and the second silently replaces the first. Consequences:

- The earlier run's manifest still lists those filenames, but the files now hold the later
  run's measurements. The manifest is intact and its contents are no longer what it recorded.
- A cell carries no `run_id`, so the two cannot be told apart from the cells themselves. Only
  the manifests, the git history, and file mtimes distinguish them.

**Supersede rather than rewrite.** Do not edit the older manifest to drop the affected cells:
that would make it claim it produced fewer cells than it did, and the run becomes
unreproducible from its own record.

1. Leave the older manifest exactly as written.
2. Add `results/manifest_<old-id>.SUPERSEDED.md` naming which cell files were overwritten, by
   which run id, and on what date — so a reader who finds the old manifest learns immediately
   that resolving its filenames today does not reproduce it.
3. If the older run's numbers are still needed, recover the cells from git
   (`git show <commit>:results/raw/<cell>.json`) rather than re-running — a re-run on a
   different host is a new measurement, not a recovery.
4. Give every subsequent run an explicit `--run-id`, and prefer a fresh dataset/scale/arm
   triple when re-measuring something already recorded.

### Recording the incident

The end state to aim for: a future reader who finds cells that no manifest references, or a
manifest whose cells no longer match it, can reconstruct what happened without asking anyone.
That takes three artifacts.

1. **The quarantine or supersede file** next to the manifest — the local, mechanical facts:
   run id, affected cell filenames, failing checks, date.
2. **A `docs/iteration-log.md` entry** for the run that caused it, even when the run produced
   nothing usable. Use the normal entry template; put the failure under **Results** and set
   **Decision** to `reject` with the reason. A run that is absent from the log is
   indistinguishable from one that never happened, which is how orphan cells become
   permanently unexplained.
3. **A line in this section's incident list**, below, when the damage changed how the harness
   or the docs work — so the fix and the incident stay attached to each other.

Do not record the incident by editing the affected cells, the manifest, or an existing log
entry. Every record is additive.

#### Incidents

- **2026-08-13 — `22-confound` / `22-confound-check`.** Two runs, minutes apart, wrote the
  same three cell filenames; the second replaced the first, so
  `results/manifest_22-confound.json` now resolves to cells it did not produce. The runs also
  mixed hosts with the Iteration 1 cells they overwrote (Linux and Windows, both labelled
  `environment_class=sandbox`) and recorded zeroed memory blocks. This is the incident that
  motivated `scripts/verify_run_integrity.py`, the host-fingerprint comparison, and the
  unmeasured-memory guards.

## Single-variable discipline

The held-constant block in `benchmarks/parquet_bench.py` pins writer version, data page
size, statistics, dictionary usage and page-index writing; `ROW_GROUP_BYTES_TARGET` pins
row-group sizing; row and column order are untouched. The only thing `CODEC_ARMS` varies is
the page codec. Anything that needs to change one of the pinned values belongs in a later
iteration, not this one.

(This section previously named a `parquet_bench.WRITER_FIXED` object. No such symbol has
ever existed — the constants are module-level `WRITER_*` names. Corrected 2026-08-13.)

Each raw cell records its writer configuration under `codec_config`, which is the
authoritative per-cell record of what produced that cell's numbers.
