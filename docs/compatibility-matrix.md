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

**Capture the exact error text on any failure.** "Doesn't work" is not actionable; the error
distinguishes an unsupported codec from an unsupported page version from a dictionary-page issue.

Paste the results back as a table of `(engine, version, codec, page_version) -> PASS/FAIL + error`
and I will populate the matrix below and lift the gate.

## How to populate

```bash
python3 scripts/probe_reader_support.py --write /tmp/compat   # 8 probe files
# copy /tmp/compat to each reader, then verify each file loads with 20000 rows
python3 scripts/probe_reader_support.py --read  /tmp/compat   # for pyarrow-based readers
```

Probe matrix: {none, snappy, zstd-3, gzip-6} × Parquet writer version {1.0, 2.6}.
Writer version is included because it constrains future iterations independently of the
codec — V2 data pages unlock DELTA_BINARY_PACKED and BYTE_STREAM_SPLIT, which are the
natural Iteration 3+ candidates.

## Results

| Reader | Version | none | snappy | zstd-3 | gzip-6 | v1.0 pages | v2.6 pages | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pyarrow / parquet-cpp | 25.0.1 | PASS | PASS | PASS | PASS | PASS | PASS | Verified on the benchmark host 2026-08-10 |
| parquet-mr (Spark) | *TBD* | | | | | | | **you must fill this in** |
| Trino / Presto | *TBD* | | | | | | | |
| DuckDB | *TBD* | | | | | | | |
| Hive | *TBD* | | | | | | | |
| *(unenumerated legacy readers)* | *TBD* | | | | | | | The reason this gate exists |

Only one row is filled, and it is the row that proves the least: the writer and the reader
are the same library. It carries no information about your estate.

## Known support floors (verify — do not treat as evidence)

ZSTD in Parquet requires roughly parquet-mr ≥ 1.10 (Spark ≥ 2.3), parquet-cpp/Arrow ≥ 0.13,
Impala ≥ 3.4. Older Hive and vendor readers are the usual failure points. These are
starting points for your audit, not substitutes for running the probe.

## Migration and rollback

- **GZIP is the fallback.** It is the most broadly supported Parquet codec. If ZSTD fails
  the matrix, the `gzip-6` arm is already measured and available as the compatible candidate.
- Snappy and ZSTD files can coexist within the same table — Parquet records the codec per
  column chunk, so a transition window needs no flag day.
- **Rollback = rewrite affected partitions with `compression=snappy`.** Cost is proportional
  to the partitions already converted, which argues for converting newest partitions first
  and oldest last.
