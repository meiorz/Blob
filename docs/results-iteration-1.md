# Iteration 1 — Executive summary

**Question:** should the Parquet page codec for the analytics lake change from Snappy to Zstd-3?

**Answer:** Yes for wide, string-heavy tables — *provisionally*. No as a blanket default.
Nothing here is deployable yet.

| Label | Value |
|---|---|
| Status | **Provisional keep (D1-shaped tables) — compat-blocked** |
| Environment | `sandbox` — 1 physical core + SMT, 3.8 GiB RAM. `hardware_validated=false` on all 24 rows |
| Data | **Proxy datasets, not production tables** |
| Run id | `20260811T072000Z-g5fix`, 24/24 cells, 10 trials (30 for the latency proxy) |
| Date | 2026-08-11 |

---

## The result in one table

| | **D1 — ClickBench `hits`** (105 cols, high-cardinality strings) | **D2 — NYC TLC yellow** (19 cols, numeric/timestamp) |
|---|---|---|
| Projected footprint vs Snappy | **−38.1 / −38.4 / −38.7 %** (S/M/L) | **−20.7 / −21.1 / −21.4 %** |
| Projected decode throughput vs Snappy | **1.023 – 1.075×** (faster) | **0.842 – 0.882×** (13–19 % slower) |
| Crossover bandwidth `B*` | none — wins at every bandwidth | 628 – 868 MiB/s per worker core |
| Modeled scan time @ 50 / 125 / 250 MiB/s | −32.6 / −26.9 / −21.2 % → −34.2 / −29.5 / −24.4 % | −17.5 / −13.6 / −8.7 % → −18.5 / −14.9 / −10.4 % |
| Peak RSS delta | −3.4 % … +1.8 % | −3.4 % … −0.8 % |
| Memory growth class | linear (slope 1.022) | linear (slope 1.042) |
| Gate outcome | **all 8 pass at all 3 scales** | **G8 fails** — real decode regression (p ≤ 5e-08) |
| Verdict | **Provisional keep, compat-blocked** | **Conditional** — storage/egress win paid for with CPU/latency |

---

## Why the two datasets disagree

This is the finding, not an inconsistency. Parquet applies dictionary, RLE and bit-packing
*before* the page codec, so the codec only ever sees what those encodings left behind.

Codec-attributable ratios (arm bytes ÷ uncompressed-Parquet bytes) make it concrete:

| | Snappy | Zstd-3 | Headroom the codec had |
|---|---:|---:|---|
| D1 (strings) | 3.20× | **5.04×** | large — skewed string symbols survive Parquet's encodings |
| D2 (numerics) | 1.24× | **1.61×** | small — bit-packing already removed most redundancy |

**The stated mechanism survived its own falsification test.** Iteration 1 predicted the gain
comes from Zstd's entropy stage acting on page-local residue, *not* from a larger window, and
committed in advance to the test: if the effect were window-driven it would track row-group size.
D1's reduction held at 38.1 / 38.4 / 38.7 % while the number of 128 MiB row groups went from
1 to ~4. It does not track row-group count.

Practical consequence: **expected benefit scales with how much high-cardinality string data a
table carries.** A per-table decision, not a fleet-wide default.

---

## What must happen before this can ship

Two blockers, both outside the sandbox. Neither is optional.

**1. Compatibility matrix — the hard gate.**
`docs/compatibility-matrix.md` has exactly one populated row: pyarrow reading pyarrow, i.e. the
writer reading its own output, which proves nothing about the estate. Under SKILL.md's rule
against creating incompatible artifacts without a migration path, Zstd cannot be recommended
until Spark/parquet-mr and the serving warehouse engine are verified. Probes and the four
pass criteria are specified in that file.

**2. Real-hardware re-run on a real partition.**
The sandbox cannot exercise the 4–8 GiB per-worker budget, cannot produce valid concurrency
numbers (1 physical core), and cannot drop page cache. Those are recorded as deferrals with
named triggers, not waivers. Re-run the same code with `COMPRESSION_BENCH_ENV=cloud-<shape>` on
a node matching worker shape, against at least one real D1-shaped partition.

**Treat this claim as unproven until then:** Zstd-3 decoded *faster* than Snappy on D1
(p = 2.9e-06 at L). The uncompressed control is consistent with a memory-traffic explanation —
`none` decodes only 1.68–1.93× faster than Snappy despite doing no decompression at all — but
"the compressor made reads faster" is exactly the kind of result that usually turns out to be an
artifact of the measurement environment. Do not put it in a proposal yet.

---

## Also settled

- **Gzip-6 is not a candidate.** −34.8 % footprint on D1 but decodes at 0.40–0.56× Snappy;
  fails G2/G3 at every scale. It stays as the compatibility fallback if Zstd fails the reader
  matrix, and that fallback's cost is now measured rather than guessed: roughly half the decode
  throughput for a comparable footprint.
- **Do not recompress compressed bytes.** All arms land at ≈1.000× on pre-gzipped random data
  (gzip-6 0.9976×). Pure CPU cost, zero benefit.
- **Compression is earning its keep on the scan path.** Uncompressed decodes 1.68–1.93× faster
  but carries 2.7–5.0× the projected bytes on D1.
- **Security: 9/9.** Three defects found and fixed; two are library-level footguns that affect
  anyone using the same APIs — `ZstdDecompressor.decompress(max_output_size=N)` does not cap a
  frame that declares its content size, and `decompressobj` bounds input rather than output.
  Documented as F-1/F-2/F-3 in `docs/security-threat-model.md`.
- **Correctness: 10/10.** Lossless across 9 edge cases × 4 arms; all arms byte-deterministic;
  all arms decode to identical values.

---

## What would change the conclusion

Listed so a future reader can tell quickly whether this summary is still valid:

1. A real production partition with a materially different string/numeric mix than D1 — the
   38 % figure is mix-dependent and would move.
2. Any Spark/Trino/warehouse reader failing the Zstd probes — falls back to gzip-6 with its
   measured decode penalty, or blocks the change outright.
3. Real hardware not reproducing the faster-decode result — the D1 case weakens from
   "dominates at every bandwidth" to a crossover-bandwidth decision like D2's.
4. Per-worker effective bandwidth materially above ~870 MiB/s — D2's case disappears entirely,
   and D1's margin narrows.

## Next iterations — planned, deliberately not started

Both are blocked behind the two follow-ups above; optimizing a codec that may not be deployable
is wasted effort.

- **Iteration 2:** Zstd level × data-page size on D1 and D2. Directly tests the entropy-vs-window
  mechanism and is the most likely route to recovering D2's decode regression.
- **Iteration 3:** row ordering / clustering as a preconditioning step. SKILL.md flags data layout
  as a first-class optimization target, and it plausibly exceeds the codec swap in magnitude.
  Deliberately excluded from Iteration 1 because it confounds with the codec variable.

**Sources:** `docs/results.md` (full 24-row matrix), `docs/iteration-log.md` (method and defects),
`docs/compatibility-matrix.md` (the blocking gate), `docs/security-threat-model.md`,
`results/summary.json` (raw gate evaluations).
