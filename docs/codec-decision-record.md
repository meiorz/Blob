# Codec decision record

Status legend: `unexamined` / `tested` / `validated` / `rejected` / `not applicable`.

## Codec matrix — workload P1

| Codec | Status | Role | Notes |
| --- | --- | --- | --- |
| Snappy | `unexamined` | **Baseline** | Current production codec. No entropy stage; whole-byte LZ77 only. |
| Zstd-3 | `unexamined` | Candidate | Iteration 1 hypothesis. Level sweep deliberately deferred to Iteration 2. |
| Gzip-6 | `unexamined` | Compatibility baseline | Promoted to a real arm: the fallback if ZSTD fails the compatibility gate. |
| Uncompressed | `unexamined` | **Required control** | Byte floor; isolates codec cost from Parquet decode + Arrow materialization. |
| Already-compressed | `tested` | **Required control** | All arms ≈1.00× on pre-gzipped random bytes — no gain available; recompression is pure CPU cost. |
| LZ4 | `unexamined` | Not in Iteration 1 | Candidate for S1 hot path, not P1. |
| Brotli | `not applicable` | — | Codec matrix scopes it to web/static delivery. Recorded, not silently dropped. |

All P1 statuses are `unexamined` because **measurement is blocked on dataset acquisition**.
Harness, controls, security and correctness suites are complete and passing.

## Mandatory investigation areas

| Failure mode | Status |
| --- | --- |
| Wrong codec for the workload | `unexamined` — Iteration 1 target |
| Ratio prioritized over latency/CPU/memory/cost | `unexamined` — gates G2/G3/G4 exist to catch it |
| Poor chunk/block/row-group sizing | `unexamined` — held constant in Iteration 1; Iteration 2 candidate |
| Compression of already-compressed/high-entropy data | `tested` — control confirms ≈1.00×, no gain |
| Row-oriented layout where columnar is appropriate | `not applicable` — P1 is already Parquet |
| Missing preconditioning (sorting, delta, RLE, dictionary, clustering) | `unexamined` — Iteration 3 candidate; a confound with codec, so excluded from Iteration 1 |
| Missing custom dictionaries for small repetitive payloads | `unexamined` — later iteration |
| Dictionary drift / versioning / decoder incompatibility | `unexamined` — decoder-rejection tests deferred with the dictionary iteration |
| Expensive recompression across pipeline stages | `unexamined` |
| Poor parallelization / non-splittable files | `unexamined` — **cannot be tested on this host** (1 physical core) |
| Weak random-access / partial-read behaviour | `unexamined` — projected-decode path measures part of this |
| Fragmentation from incremental writes | `not applicable` — P1 writes via batch and compaction only |
| Corruption propagation / insufficient checksums | `tested` — truncation and mutation fuzzing pass |
| Zip/decompression/recursion bombs, resource exhaustion | `validated` — 9/9 hostile-input tests; 3 defects found and fixed |
| Unsafe parsing of attacker-controlled input | `validated` — see threat model F-1/F-2/F-3 |
| Lossy compression violating domain constraints | `not applicable` — both workloads are lossless-only |
| Benchmark bias from unrealistic data or warm-cache-only tests | **`unexamined` and currently the dominant risk** — see `docs/dataset-catalog.md` |

## Not a universal winner

Codec choice is workload-dependent. The Iteration 1 deliverable is a bandwidth-parameterized
decision rule with a crossover point `B*`, not a ranking. The expected shape of the answer is
"Zstd for cold/large tables where transfer dominates; Snappy still defensible on very
high-bandwidth hot paths" — and S1's hot path may land on the opposite side of that line
from P1.
