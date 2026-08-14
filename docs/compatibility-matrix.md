# Parquet reader / codec compatibility matrix

**This is a HARD GATE.** The declared compatibility floor for this project is
*"unknown / mixed legacy readers present"*. Under SKILL.md's decision rules a change that
"creates an incompatible artifact without an explicit migration path" is rejected —
so **no codec change may be recommended until this matrix is populated for your estate**,
regardless of how favourable the benchmark numbers are.

## Required before any "keep" decision

1. Files written by this harness with `compression=zstd`, `version=1.0` load successfully in:
   - [ ] parquet-mr (Spark)
   - [ ] Arrow / parquet-cpp
   - [ ] at least one warehouse engine (DuckDB / Trino / equivalent)
2. [ ] Every reader in the estate that does **not** support ZSTD is enumerated, with a
       documented deprecation or migration plan.
3. [ ] Rollback plan for already-persisted Snappy data is written and reviewed.

## What I need you to run, precisely

Generate the probes in the sandbox:

```bash
python3 scripts/probe_reader_support.py --write /tmp/compat
```

That emits 8 files: `probe_{none,snappy,zstd,gzip}_{v10,v26}.parquet`, each with 20,000 rows and
5 columns (`i` int64, `ts` timestamp[ms], `low_card` string, `high_card` string, `f` double),
plus `expected.json`.

**Engines to test**, with the version recorded for each:

| Engine | Why it is on the list |
| --- | --- |
| Spark / parquet-mr | Your production writer and reader; the decisive one |
| Trino (or the warehouse engine you actually serve dashboards from) | Interactive query path with the 20 s p95 SLO |
| DuckDB | Modern-reader proxy; cheap to run and isolates whether a failure is engine-specific or format-specific |

**A file counts as PASS only if all four of these succeed:**

1. `SELECT count(*)` returns exactly **20000**.
2. Schema matches: 5 columns with the types above — a reader that silently coerces
   `timestamp[ms]` or widens a type is a **FAIL**, not a pass.
3. Filter + aggregate returns a correct answer, e.g.
   `SELECT count(*), sum(f) FROM t WHERE i >= 10000` → expect **10000** rows and
   **sum(f) = 224992500.0** (f = i × 1.5 for i in 10000..19999).
   Verified against `probe_zstd_v10.parquet` on 2026-08-11, not derived by hand.
4. `SELECT high_card FROM t WHERE i = 19999` → `https://example.invalid/p/19999?q=139993`.
   This reads an actual compressed string page rather than metadata, so it catches readers that
   parse the footer happily and then fail on page decompression.

`--read` enforces all four criteria and reports each one **independently**, so the output
distinguishes *"this reader cannot parse the file at all"* from *"this reader parses the
metadata and then cannot decompress the pages"* — different problems with different fixes.
Criteria 1–2 are answered from the footer alone; 3–4 decompress real data pages.

**Capture the exact error text on any failure.** "Doesn't work" is not actionable; the error
distinguishes an unsupported codec from an unsupported page version from a dictionary-page issue.

Paste the results back as a table of `(engine, version, codec, page_version) -> PASS/FAIL + error`
and I will populate the matrix below and lift the gate.

## How to populate

`scripts/probe_reader_support.py --help` is the self-contained operator reference: it documents
both modes, the four criteria, the exact SQL to run by hand on engines pyarrow cannot drive, and
the exit codes. Nothing below requires reading the rest of this repo.

**1. Generate the probes** (once, anywhere with pyarrow):

```bash
python3 scripts/probe_reader_support.py --write /tmp/compat
```

**2. Verify each pyarrow-based reader.** This runs all four criteria on all 8 files:

```bash
python3 scripts/probe_reader_support.py --read /tmp/compat \
    --json /tmp/compat/result-pyarrow.json --engine pyarrow --engine-version 25.0.1
```

Exit status is `0` only if every file passed all four checks; `1` if any check failed;
`2` on a usage error or a directory that is not a probe directory. `--engine` /
`--engine-version` are recorded in the JSON and default to `null` — fill them in, because a
verdict with no engine version attached cannot be audited later.

`--json` emits one entry per `<codec>|<page_version>` arm:

```json
{"engine": "pyarrow", "engine_version": "25.0.1",
 "probes": {"zstd|1.0": {"pass": true,
                         "checks": {"row_count": true, "schema": true,
                                    "filter_agg": true, "string_page": true},
                         "error": null}}}
```

**3. Verify every other reader in the estate.** Copy `/tmp/compat` to Spark/parquet-mr, the
warehouse engine serving dashboards, DuckDB, Hive and any legacy reader, then run the four
SQL statements from `--help` against each file. `--read` also prints a ready-made Markdown row
for the Results table below; paste the exact error text of any failure into its Notes column.

Probe matrix: {none, snappy, zstd-3, gzip-6} × Parquet writer version {1.0, 2.6}.
Writer version is included because it constrains future iterations independently of the
codec — V2 data pages unlock DELTA_BINARY_PACKED and BYTE_STREAM_SPLIT, which are the
natural Iteration 3+ candidates.

## Results

`data/metadata/compat_matrix.json` is the source of truth for the table below; analysis code
should read that file and decide "compat-blocked" from it rather than parsing this Markdown.
Record a probe outcome by editing the JSON and re-running:

```bash
python3 scripts/render_compat_matrix.py
```

Write `true` / `false` only for outcomes you actually observed. **`null` means "not tested yet"
and is the honest default** — a `false` that really means "unknown" renders as FAIL and makes the
gate look decided on data that does not exist.

**Collapsed columns are four-state, symmetric on both axes.** The real verdicts live per
`(codec, page_version)` in `checks`; the columns are a projection of them:

| Cell | Meaning |
| --- | --- |
| `PASS` | every tested combination in that row/column passed |
| `FAIL` | every tested combination failed |
| `PARTIAL` | mixed — some passed, some failed; read `checks` for which |
| `*TBD*` | nothing tested yet |

Any two-state projection is lossy in one direction or the other. Collapsing with ALL reports
`snappy = FAIL` for a reader that reads Snappy perfectly well on v1.0 pages and only fails on
v2.6; collapsing with ANY reports `v1.0 pages = PASS` when some codec fails there. Either way the
cell contradicts the `checks` beneath it, and in a document that gates codec adoption a false
`FAIL` is as damaging as a false `PASS` — it can retire a migration path that actually works.

Read the diagnosis off the pattern: a **pure `FAIL` in a codec column** is an unsupported codec;
a **pure `FAIL` in a page column** is an unsupported page version; `PARTIAL` on the other axis is
the expected shadow of either. The generated block spells out each `PARTIAL` — including which
arms still work, so a codec that fails only on v2.6 is still visibly available on v1.0.

**`PARTIAL` is not a pass.** The gate stays closed unless the exact `(codec, page version)` a
partition would be written with is explicitly `PASS`. The renderer refuses to render a
`codec_support` / `page_support` summary that contradicts `checks`, and refuses a boolean where
the measured result is mixed — a boolean cannot express `PARTIAL`, so leave it `null` and let
`checks` carry the detail.

<!-- BEGIN GENERATED: results -- edit data/metadata/compat_matrix.json, not this block -->

**Gate: BLOCKED — 5 of 6 readers untested (parquet-mr (Spark), Trino / Presto, DuckDB, Hive, *(unenumerated legacy readers)*).** No codec change may be recommended while any required reader is unverified, regardless of how favourable the benchmark numbers are. **PARTIAL is not a pass**: adoption needs an explicit PASS for the exact (codec, page version) a partition would be written with.

ZSTD-3 fully verified on: pyarrow / parquet-cpp.

| Reader | Version | none | snappy | zstd-3 | gzip-6 | v1.0 pages | v2.6 pages | Notes |
|---|---|---|---|---|---|---|---|---|
| pyarrow / parquet-cpp | 25.0.1 | PASS | PASS | PASS | PASS | PASS | PASS | Writer reading its own output -- carries no information about the estate. All four criteria enforced 2026-08-11 (8/8 files, 32/32 checks) on a dev workstation, not the benchmark host the original 2026-08-10 row cites. The earlier row predates enforcement of criteria 2-4. |
| parquet-mr (Spark) | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | The decisive engine. You must fill this in. |
| Trino / Presto | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | Interactive query path with the 20 s p95 SLO. |
| DuckDB | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | Isolates whether a failure is engine-specific or format-specific. |
| Hive | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | Older Hive is a usual ZSTD failure point. |
| *(unenumerated legacy readers)* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | The reason this gate exists. |

_Rendered from `data/metadata/compat_matrix.json` (last_updated 2026-08-12T04:15:18Z) by `scripts/render_compat_matrix.py`._
<!-- END GENERATED: results -->

Only one row is filled, and it is the row that proves the least: the writer and the reader
are the same library. It carries no information about your estate.

That row also predates enforcement: when it was recorded, `--read` checked **only** criterion 1
(row count), so its PASS cells never tested schema coercion, the filter/aggregate or string-page
decompression. All four have since been enforced in the tool, and pyarrow 25.0.1 passes them
(8/8 files, 32/32 checks) — but that re-run was on a dev workstation, not the benchmark host the
row cites. Re-run it there before treating the row as current.

## Known support floors (verify — do not treat as evidence)

ZSTD in Parquet requires roughly parquet-mr ≥ 1.10 (Spark ≥ 2.3), parquet-cpp/Arrow ≥ 0.13,
Impala ≥ 3.4. Older Hive and vendor readers are the usual failure points. These are
starting points for your audit, not substitutes for running the probe.

## Migration and rollback

<!-- BEGIN GENERATED: fallback -- edit data/metadata/compat_matrix.json, not this block -->

- **GZIP-6 is the fallback.**
- It is the most broadly supported Parquet codec. If ZSTD fails the matrix, the `gzip-6` arm is already measured and available as the compatible candidate -- at roughly half Snappy's decode throughput (0.40-0.56x, Iteration 1), so the fallback is a real cost, not a free swap.
- Snappy and ZSTD files can coexist within the same table: Parquet records the codec per column chunk, so a transition window needs no flag day.
- Rollback = rewrite affected partitions with `compression=snappy`. Cost is proportional to the partitions already converted, which argues for converting newest partitions first and oldest last.

_Rendered from `data/metadata/compat_matrix.json` (last_updated 2026-08-12T04:15:18Z) by `scripts/render_compat_matrix.py`._
<!-- END GENERATED: fallback -->
