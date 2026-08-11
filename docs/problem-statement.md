# Problem statement and scope lock

## P1 — Analytics data lake on columnar storage (PRIMARY, authoritative)

Parquet files in object storage; 20–200 columns mixing numerics, timestamps, and low- and
high-cardinality strings; date partitioning. Read-dominated, scan-heavy analytics filtering
on time plus a few dimensions, projecting a subset of columns, aggregating tens of millions
to billions of rows. Terabytes/day arriving via batch jobs and compaction. Current baseline
is Parquet + Snappy; Zstd is an allowed candidate.

- **SLOs** — interactive p95 ≤ 20 s on warm data; batch is throughput/cost-oriented;
  engines must stay responsive under concurrent load.
- **Memory** — 4–8 GiB per query task target, 16 GiB hard limit. Superlinear growth with
  input size is unacceptable without explicit approval.
- **Environment** — Linux, multi-core, distributed engine (Spark/Trino/warehouse) over
  object storage. Local caches exist but correctness must not depend on them.
- **Lossless only.** No lossy compression on these tables, ever.

**Inferred fields** (not stated in the brief; correct if wrong):

- *Read/write ratio* — write-once-read-many. Encode cost amortizes across many reads, so
  encode throughput is a soft constraint and decode/footprint are the hard ones.
- *Durability/compatibility* — durability delegated to the object store. Compatibility is a
  **hard gate** because readers are unenumerated (below).

## S1 — Streaming log and event compression (SECONDARY, contrast)

JSON-lines and plain-text application logs plus telemetry/trace events with repetitive
schemas. High-throughput append-only ingestion; continuous stream consumers plus bursty
incident-time tailing. LZ4/Snappy acceptable in the hot path; Zstd acceptable for offline
recompression only. Ingestion p95 in the hundreds of ms to a few seconds. Incremental
compression memory in the low hundreds of MiB per process; no multi-GiB buffers.
**Lossless only** — logs must stay exact for debugging, audit and compliance.

**Role in Iteration 1: not benchmarked.** S1 is a design check. The failure being guarded
against is "Zstd won on the lake, therefore Zstd everywhere" — S1's per-batch latency budget
and memory ceiling do not permit that inference. Iteration 1's output is therefore a decision
*rule* parameterized by bandwidth, not a codec verdict.

## Conflict register

SKILL.md sets gates this host cannot satisfy. Recorded as deferrals with named triggers,
not waivers. Auto-populated per run into `env.not_measured`; see
`docs/benchmark-methodology.md`.

| SKILL.md requirement | Host reality | Resolution |
| --- | --- | --- |
| Per-worker memory budget (4–8 GiB / 16 GiB) | 3.8 GiB total RAM | Absolute budget NOT MEASURABLE. Scale-invariant metrics only; absolute check on real hardware before any keep decision. |
| Concurrency memory behaviour; thread labeling | 1 physical core + SMT sibling | Concurrency NOT MEASURED; all runs pinned single-threaded. |
| Cold vs warm cache separation | `drop_caches` denied (unprivileged) | Cold-cache NOT MEASURED; codec benchmarks run in-process on preloaded buffers. |
| ≥5 trials local / ≥10 noisy | Shared VM | 10 trials minimum; 30 for the latency proxy. |
| Brotli in codec matrix | — | **Not applicable** to P1 (web/static delivery only). Recorded, not dropped. |
| Gzip as "compatibility baseline" | Unenumerated readers | Promoted to a real arm: GZIP is the fallback if ZSTD fails the compatibility matrix. |

**Additional validity threat.** The harness writes Parquet with pyarrow; production writers
are almost certainly parquet-mr (Spark). Encoder implementations differ in dictionary fallback
thresholds and page packing, so absolute footprints will not match production. Only
*ratios between codecs under the same writer* transfer — which is why the accept threshold
is stated as a relative delta.

## Compatibility floor

**Unknown / mixed legacy readers present.** Compatibility is therefore a first-class hard
gate: no codec change is recommendable until `docs/compatibility-matrix.md` is populated for
the estate, regardless of measured wins.
