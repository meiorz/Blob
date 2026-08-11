# Dataset catalog

## ⚠ Read this before citing any number from this repository

**Every dataset here is a PROXY. None of it is your data.**

Compression behaviour is a function of the *value distributions* in your columns —
string cardinality, run structure, sortedness, null density. A proxy dataset with the
right schema shape but different value distributions can move a codec comparison by tens
of percent. **Results on proxy data must be re-run on at least one real production
partition before any production codec change.** No exceptions, no "it's probably fine".

This is the single largest threat to the validity of Iteration 1.

## Status: RESOLVED via R1 — both proxies acquired 2026-08-10

The approved Iteration 1 proxies (ClickBench `hits`, NYC TLC yellow taxi) were acquired by
**R1** (user-side browser download) after direct sandbox egress failed. Both were used for
the completed Iteration 1 run `20260811T072000Z-g5fix`. They remain **proxies, not
production data** — the warning at the top of this file is unchanged and still governs.

### Corrections log

| Date | Correction |
| --- | --- |
| 2026-08-11 | **SHA-256 digests corrected.** `data/metadata/catalog.json` previously stored 32 hex chars — a truncated *prefix* of the SHA-256, not a different algorithm. Recomputed on the source files with `Get-FileHash -Algorithm SHA256`; each old 32-char value is an exact prefix of the corrected 64-char digest, so dataset identity for the Iteration 1 run is **confirmed, not invalidated**. `results/raw/*.json` still carry the 32-char prefix and were deliberately not rewritten — they are append-only recorded evidence. |
| 2026-08-11 | **Absolute paths removed.** The catalog stored acquiring-session absolute paths (`/sessions/<session>/...`), which made the repo unrunnable on any other host. Replaced with `file` + runtime resolution (see *Acquisition* below). |

### Acquisition

Source files are **not committed** (`.gitignore`: `data/raw/`). To reproduce:

```bash
# Download each dataset's "source" URL from data/metadata/catalog.json, then:
mkdir -p data/raw && mv hits_0.parquet yellow_tripdata_2024-01.parquet data/raw/
sha256sum data/raw/*.parquet          # must match catalog.json "sha256"
```

`orchestrate.py` resolves each entry's `file` under `COMPRESSION_BENCH_DATA_ROOT`
(default `<repo>/data/raw`) and fails with an actionable message if absent.

### Why direct download failed (retained for the record)

Sandbox egress is restricted to an allowlist. Measured on 2026-08-10:

| Host | Purpose | Result |
| --- | --- | --- |
| `datasets.clickhouse.com` | ClickBench `hits` | **blocked** (proxy 403 on CONNECT) |
| `d37ci6vzurychx.cloudfront.net` | NYC TLC trip data | **blocked** (proxy 403) |
| `raw.githubusercontent.com` | raw files | **blocked** |
| `codeload.github.com` | repo tarballs | **blocked** |
| `api.github.com` | release metadata | **blocked** |
| `huggingface.co`, `archive.org` | dataset mirrors | **blocked** |
| `extensions.duckdb.org` | duckdb `tpch`/`tpcds` extensions | **blocked** |
| `pypi.org`, `files.pythonhosted.org` | Python packages | reachable |
| `registry.npmjs.org` | npm | reachable |

GitHub release assets were also tested directly and fail. **PyPI is the only viable data
source from inside the sandbox.**

Per the approved plan ("if either source is unreachable I will report that and stop for a
decision, not silently substitute a synthetic generator"), measurement was halted for a
dataset decision rather than substituting a generator. **R1 was chosen and executed.**

## Candidate resolutions (as evaluated; R1 selected)

| Option | Fidelity to P1 | Cost |
| --- | --- | --- |
| **R1** Download on your machine (Chrome is connected) into the workspace folder | Preserves approved ClickBench + TLC exactly | A few minutes of your time; needs explicit download approval |
| **R2** Drop one real production partition into the workspace folder | **Highest** — eliminates the proxy gap entirely | Depends on data-handling policy |
| **R3** TPC-H via `tpchgen-cli` (verified working offline, PyPI) | **Low for this hypothesis** — see below | Zero; available now |
| **R4** Seeded synthetic generator shaped like ClickBench `hits` | Lowest — see below | Zero |

### Why R3 (TPC-H) is a weak substitute *for this specific hypothesis*

TPC-H is a legitimate, specified, widely cited analytics benchmark, and `tpchgen-cli`
3.0.0 runs offline here. But `lineitem` is **16 columns**, dominated by integers,
decimals and dates. Its only sizeable string column, `l_comment`, is generated from a
small fixed word dictionary and is therefore *unrealistically compressible*. TPC-H has
**no high-cardinality string columns at all** — which is exactly the column class the
Iteration 1 mechanism (entropy coding on already-dictionary-encoded pages) targets.

Running Iteration 1 on TPC-H alone would test the hypothesis on the data type least
likely to exhibit the effect. That is a conservative test, not an invalid one, but it
under-represents a 20–200 column table with high-cardinality strings so badly that a null
result would not be informative.

### Why R4 (synthetic) is worse than it looks

For a compression study the value distributions *are* the experiment. Choosing the string
generator determines the compression ratio; the benchmark would then measure my
assumptions rather than your data. You already declined this category, and the reasoning
holds.

## Dataset definitions (as run in Iteration 1)

| | **D1** (primary) | **D2** (contrast) |
| --- | --- | --- |
| Source | ClickBench `hits` (`hits_0.parquet`) | NYC TLC yellow taxi (`yellow_tripdata_2024-01.parquet`) |
| SHA-256 | `fa134fe1…a219a16d` | `c4d59da7…b2030510` |
| Rows / columns | 1,000,000 / 105 | 2,964,624 / 19 |
| Projection | 12 of 105 columns | 7 of 19 columns |
| Source codec | SNAPPY | ZSTD |
| Type | Tabular / columnar | Tabular / columnar |
| Columns | ~100 | ~19 |
| Composition | Mixed ints, timestamps, high-cardinality strings (URL, Referer, Title), low-cardinality dimensions | Numeric + timestamp dominant |
| Access pattern | Column scan, projection + time-range filter | same |
| Lossless/lossy | **Lossless only** | **Lossless only** |
| Role | High-cardinality-string half of the P1 profile | Tests whether the effect survives on numeric-heavy tables |

Mandatory controls (SKILL.md), both dataset-independent in construction:

- **C1 no-compression** — identical Parquet with `compression=NONE`. Establishes the byte
  floor and isolates codec cost from Parquet decode + Arrow materialization. Implemented
  as the `none` arm.
- **C2 already-compressed** — BINARY column of pre-gzipped payloads plus CSPRNG bytes, and
  recompression of finished Parquet bytes. Demonstrates the do-not-recompress case.

### Scales actually run

`orchestrate.py`'s defaults are S = 128 MiB, M = 512 MiB, L = 1.5 GiB of uncompressed Arrow
bytes, but **both datasets override them in `catalog.json`** because the sandbox host has
3.8 GiB RAM:

| | S | M | L |
| --- | ---: | ---: | ---: |
| D1 | 96 MiB | 288 MiB | 432 MiB |
| D2 | 64 MiB | 192 MiB | 320 MiB |

Peak demand is roughly 3× the scale — integrity verification holds the source table and its
decoded roundtrip simultaneously, plus the Parquet buffer. At 720 MiB the D1 cell was killed.
**L here is a host-RAM ceiling, not a probe of your per-worker budget**, which is precisely
why the memory gates (G4–G6) cannot be treated as validated from sandbox runs.

Every dataset gets SHA-256, row/column counts, per-column cardinality, null rates, entropy
estimates and sortedness recorded in `data/metadata/` before any benchmark runs.

## Smoke-test data (NOT experimental evidence)

TPC-H scale factor 0.05 was generated to validate that the harness executes end to end.
It exercised code paths only. Nothing from it appears in `docs/results.md`, and it is not
Iteration 1 evidence.
