# Large-Scale Compression Research and Optimization

Evidence-driven investigation of compression choices for two workloads. Process and gates
are defined by `SKILL.md`, which is the single source of truth; this README only orients.

## Status

| | |
| --- | --- |
| Iteration | 1 — Parquet page codec: SNAPPY → ZSTD-3 |
| State | **BLOCKED on dataset acquisition** (see `docs/dataset-catalog.md`) |
| Harness | Built and smoke-tested end to end |
| Security suite | 9/9 passing; 3 real findings, all fixed (`docs/security-threat-model.md`) |
| Environment | `sandbox` — **no result here is hardware-validated** |

## Workloads

- **P1 (primary)** — analytics data lake, Parquet on object storage, read-dominated
  scan-heavy queries, lossless only. Authoritative when choices conflict.
- **S1 (secondary)** — streaming logs/events, latency-sensitive hot path, lossless only.
  Used as a design check so P1 conclusions do not silently assume batch context.

## Layout

```txt
benchmarks/   env capture, memory profiler, benchmark cell, subprocess runner, orchestrator, cost model
security/     safe_decompress.py — bounded decoding for untrusted input
scripts/      dataset profiling, reader-compatibility probe, result analysis
tests/        correctness, edge cases, hostile inputs, fuzz fixtures
docs/         problem statement, methodology, threat model, catalog, compatibility, results, iteration log
data/metadata/ dataset fingerprints and column profiles (no bulk data)
results/      raw per-cell JSON (integer bytes, ms) + run manifests
```

## Reproducing

```bash
pip install pyarrow cramjam zstandard

python3 tests/test_hostile_inputs.py                        # security suite
python3 scripts/probe_reader_support.py --write /tmp/compat # compatibility probes
python3 benchmarks/orchestrate.py --catalog data/metadata/catalog.json --trials 10
python3 scripts/analyze_results.py
```

Set `COMPRESSION_BENCH_ENV=cloud-<shape>` when running on real hardware. It defaults to
`sandbox`, and results carry that label so they can never be misread as validated.

## Ground rules

Baseline before optimization. One meaningful variable per iteration. No improvement
claimed without reproducible benchmark evidence. External compressed input is hostile.
Ratio alone is never sufficient — memory, latency, correctness, compatibility and security
are hard constraints.
