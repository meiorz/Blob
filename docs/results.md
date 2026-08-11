# Results

**Environment: `sandbox` on every row — none of this is hardware-validated.**
Run id `20260811T072000Z-g5fix`, 24/24 cells, 10 trials per cell (30 for the projected-decode
latency proxy), single-threaded pinned to CPU 0. Concurrency, cold-cache, and the absolute
per-worker memory budget are NOT MEASURED on this host — see `docs/benchmark-methodology.md`.

**Zstd-3 is PROVISIONAL and COMPAT-BLOCKED.** It cannot move to "keep" until
`docs/compatibility-matrix.md` is populated against real readers, no matter how the numbers look.

## Headline

| | **D1 — ClickBench hits** (105 cols, high-card strings) | **D2 — NYC TLC** (19 cols, numeric) |
| --- | --- | --- |
| Projected footprint reduction | **38.1 / 38.4 / 38.7 %** (S/M/L) | **20.7 / 21.1 / 21.4 %** |
| Projected decode throughput vs Snappy | **1.023 – 1.075×** (faster) | **0.842 – 0.882×** (slower) |
| Crossover bandwidth B* | none — dominates at every bandwidth | 628 – 868 MiB/s |
| Modeled scan time @ 50/125/250 MiB/s | −32.6 / −26.9 / −21.2 % (S) … −34.2 / −29.5 / −24.4 % (L) | −17.5 / −13.6 / −8.7 % … −18.5 / −14.9 / −10.4 % |
| Peak RSS delta | −3.4 % … +1.8 % | −3.4 % … −0.8 % |
| Growth class | linear (slope 1.022) | linear (slope 1.042) |
| Gates | **all pass at all three scales** | **G8 fails** — real latency regression |

**The mechanism prediction held.** Iteration 1 predicted the win comes from Zstd's entropy stage
acting on page-local residue, not from a larger window, and offered a falsifiable counter-test:
if the gain were window-driven it would track row-group size. Footprint reduction on D1 is
38.1 / 38.4 / 38.7 % while the number of 128 MiB row groups goes from 1 to ~4. It does not track
row-group count. The codec-attributable ratios say the same thing directly: on D1 Snappy reaches
3.20× over uncompressed Parquet and Zstd 5.04×, whereas on D2 the same figures are 1.24× and
1.61× — on numeric columns Parquet's own dictionary/RLE/bit-packing has already removed nearly
everything, leaving the page codec almost no residue to entropy-code.

**Zstd decoding *faster* than Snappy on D1 (p = 0.0087 / 0.0051 / 2.9e-06)** is the one result
that should draw suspicion rather than celebration. The plausible reading is that a 38 % smaller
page stream costs proportionally less memory traffic, and that saving exceeds Zstd's higher
per-byte decode cost on wide string columns. The uncompressed control supports this: `none`
decodes only 1.68–1.93× faster than Snappy despite doing no decompression at all. This should be
re-tested on real hardware before anyone leans on it.

## Full matrix

| ID | Env | Dataset | Config | Ratio | Codec-attrib | Proj MiB | vs Snappy | Enc MiB/s | Dec MiB/s | Peak RSS MiB | Peak USS MiB | Post-run RSS MiB | Mem/In | Mem/Out | Growth | p95 ms | Integrity | Security | Decision |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- | --- | --- |
| D1-clickbench-hits/L/gzip-6 | sandbox | D1-clickbench-hits | gzip-6 | 9.63 | 4.65 | 14.55 | +34.8% | 99 | 670 | 1207 | 1184 | 678 | 2.65 | 25.5 | linear | 173.67 | PASS | 9/9 | reject (G2,G3,G8) |
| D1-clickbench-hits/L/none | sandbox | D1-clickbench-hits | none | 2.07 | 1.00 | 82.48 | -269.5% | 723 | 1289 | 1401 | 1390 | 851 | 3.08 | 6.4 | linear | 46.29 | PASS | 9/9 | control |
| D1-clickbench-hits/L/snappy | sandbox | D1-clickbench-hits | snappy | 6.57 | 3.17 | 22.32 | +0.0% | 600 | 992 | 1246 | 1224 | 695 | 2.74 | 18.0 | linear | 89.74 | PASS | 9/9 | baseline |
| D1-clickbench-hits/L/zstd-3 | sandbox | D1-clickbench-hits | zstd-3 | 10.16 | 4.91 | 13.68 | +38.7% | 498 | 992 | 1220 | 1177 | 679 | 2.68 | 27.2 | linear | 93.38 | PASS | 9/9 | **provisional keep — compat-blocked** |
| D1-clickbench-hits/M/gzip-6 | sandbox | D1-clickbench-hits | gzip-6 | 10.34 | 4.79 | 7.96 | +34.8% | 101 | 727 | 886 | 863 | 550 | 3.07 | 31.8 | linear | 104.70 | PASS | 9/9 | reject (G2,G3,G8) |
| D1-clickbench-hits/M/none | sandbox | D1-clickbench-hits | none | 2.16 | 1.00 | 46.10 | -277.6% | 733 | 1317 | 1003 | 943 | 649 | 3.48 | 7.5 | linear | 27.71 | PASS | 9/9 | control |
| D1-clickbench-hits/M/snappy | sandbox | D1-clickbench-hits | snappy | 6.98 | 3.24 | 12.21 | +0.0% | 646 | 1114 | 928 | 917 | 564 | 3.22 | 22.5 | linear | 50.93 | PASS | 9/9 | baseline |
| D1-clickbench-hits/M/zstd-3 | sandbox | D1-clickbench-hits | zstd-3 | 11.00 | 5.10 | 7.53 | +38.4% | 514 | 1060 | 897 | 856 | 554 | 3.11 | 34.2 | linear | 47.03 | PASS | 9/9 | **provisional keep — compat-blocked** |
| D1-clickbench-hits/S/gzip-6 | sandbox | D1-clickbench-hits | gzip-6 | 10.23 | 4.75 | 2.65 | +34.8% | 99 | 750 | 644 | 629 | 524 | 6.68 | 68.4 | linear | 27.90 | PASS | 9/9 | reject (G2,G3,G8) |
| D1-clickbench-hits/S/none | sandbox | D1-clickbench-hits | none | 2.15 | 1.00 | 15.08 | -270.6% | 768 | 1577 | 689 | 668 | 568 | 7.14 | 15.4 | linear | 9.48 | PASS | 9/9 | control |
| D1-clickbench-hits/S/snappy | sandbox | D1-clickbench-hits | snappy | 6.89 | 3.20 | 4.07 | +0.0% | 648 | 1193 | 648 | 636 | 527 | 6.72 | 46.3 | linear | 15.89 | PASS | 9/9 | baseline |
| D1-clickbench-hits/S/zstd-3 | sandbox | D1-clickbench-hits | zstd-3 | 10.84 | 5.04 | 2.52 | +38.1% | 527 | 1237 | 659 | 648 | 530 | 6.84 | 74.2 | linear | 18.77 | PASS | 9/9 | **provisional keep — compat-blocked** |
| D2-nyc-tlc-yellow/L/gzip-6 | sandbox | D2-nyc-tlc-yellow | gzip-6 | 8.85 | 1.63 | 21.80 | +20.1% | 122 | 990 | 850 | 803 | 499 | 2.70 | 23.9 | linear | 131.30 | PASS | 9/9 | reject (G2,G3,G8) |
| D2-nyc-tlc-yellow/L/none | sandbox | D2-nyc-tlc-yellow | none | 5.41 | 1.00 | 32.93 | -20.6% | 834 | 2406 | 890 | 789 | 523 | 2.82 | 15.3 | linear | 29.42 | PASS | 9/9 | control |
| D2-nyc-tlc-yellow/L/snappy | sandbox | D2-nyc-tlc-yellow | snappy | 6.87 | 1.27 | 27.30 | +0.0% | 707 | 1642 | 866 | 795 | 511 | 2.75 | 18.9 | linear | 59.76 | PASS | 9/9 | baseline |
| D2-nyc-tlc-yellow/L/zstd-3 | sandbox | D2-nyc-tlc-yellow | zstd-3 | 9.00 | 1.66 | 21.45 | +21.4% | 557 | 1526 | 859 | 848 | 506 | 2.73 | 24.5 | linear | 65.87 | PASS | 9/9 | reject (G8) |
| D2-nyc-tlc-yellow/M/gzip-6 | sandbox | D2-nyc-tlc-yellow | gzip-6 | 8.76 | 1.62 | 13.18 | +19.9% | 121 | 1018 | 565 | 523 | 353 | 2.99 | 26.2 | linear | 75.39 | PASS | 9/9 | reject (G1,G2,G3,G8) |
| D2-nyc-tlc-yellow/M/none | sandbox | D2-nyc-tlc-yellow | none | 5.41 | 1.00 | 19.76 | -20.1% | 828 | 2762 | 613 | 583 | 395 | 3.24 | 17.5 | linear | 17.47 | PASS | 9/9 | control |
| D2-nyc-tlc-yellow/M/snappy | sandbox | D2-nyc-tlc-yellow | snappy | 6.83 | 1.26 | 16.45 | +0.0% | 694 | 1904 | 576 | 564 | 363 | 3.05 | 20.8 | linear | 30.96 | PASS | 9/9 | baseline |
| D2-nyc-tlc-yellow/M/zstd-3 | sandbox | D2-nyc-tlc-yellow | zstd-3 | 8.92 | 1.65 | 12.97 | +21.1% | 551 | 1792 | 571 | 560 | 359 | 3.02 | 27.0 | linear | 36.75 | PASS | 9/9 | reject (G8) |
| D2-nyc-tlc-yellow/S/gzip-6 | sandbox | D2-nyc-tlc-yellow | gzip-6 | 8.72 | 1.58 | 4.47 | +19.5% | 124 | 1067 | 367 | 325 | 303 | 5.74 | 50.0 | linear | 25.11 | PASS | 9/9 | reject (G1,G2,G3,G8) |
| D2-nyc-tlc-yellow/S/none | sandbox | D2-nyc-tlc-yellow | none | 5.50 | 1.00 | 6.58 | -18.5% | 781 | 2774 | 379 | 299 | 311 | 5.92 | 32.6 | linear | 6.18 | PASS | 9/9 | control |
| D2-nyc-tlc-yellow/S/snappy | sandbox | D2-nyc-tlc-yellow | snappy | 6.83 | 1.24 | 5.55 | +0.0% | 688 | 1989 | 384 | 373 | 308 | 6.01 | 41.0 | linear | 10.32 | PASS | 9/9 | baseline |
| D2-nyc-tlc-yellow/S/zstd-3 | sandbox | D2-nyc-tlc-yellow | zstd-3 | 8.86 | 1.61 | 4.40 | +20.7% | 530 | 1802 | 371 | 360 | 300 | 5.80 | 51.4 | linear | 12.26 | PASS | 9/9 | reject (G8) |

`Ratio` = Arrow in-memory bytes ÷ Parquet bytes, so it conflates Parquet's encoding layer with
the page codec. `Codec-attrib` = uncompressed-Parquet bytes ÷ arm bytes and isolates the codec —
cite that one for codec claims. `Mem/In`, `Mem/Out` are dimensionless (peak RSS ÷ data bytes);
the spec-verbatim `memory_per_input_mb` fields are in the raw JSON with the unit discrepancy noted.

## Gzip-6 — rejected as candidate, retained as compatibility fallback

Gzip reaches 34.8 % (D1) / ~20 % (D2) footprint reduction but decodes at **0.40–0.56×** Snappy,
failing G2 and G3 at every scale. It is not a viable primary. It remains the fallback if Zstd
fails the compatibility matrix, and the cost of that fallback is now measured rather than guessed:
roughly half the decode throughput for a similar footprint.

## Controls

- **No-compression** (`none`): decodes 1.68–1.93× faster than Snappy, at 2.7–5.0× the projected
  bytes on D1. Compression is paying for itself on the scan path.
- **Already-compressed**: all arms ≈1.000× on pre-gzipped random bytes (gzip-6 0.9976×).
  Recompressing compressed bytes buys nothing and costs CPU.

## Security

9/9 passing, unchanged from acquisition. Three defects found and fixed; two are library-level
footguns documented as F-1/F-2/F-3 in `docs/security-threat-model.md`.

## Compatibility

**Unsatisfied and blocking.** See `docs/compatibility-matrix.md`.
