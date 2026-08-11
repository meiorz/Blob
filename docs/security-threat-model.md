# Security threat model — compression and decompression inputs

**Stance (SKILL.md):** all externally supplied compressed data is hostile.
For workload P1 that means every Parquet file read from object storage, including
files written by our own compaction jobs, because an attacker who can write to a
bucket prefix can supply arbitrary bytes to a query worker.

## Root vulnerability class: trusting attacker-declared sizes

Every bomb in scope reduces to one pattern — a decoder reads a size field that the
attacker controls and allocates that much memory *before* validating anything.

| Vector | Attacker-controlled field | Naive behaviour | Defence |
| --- | --- | --- | --- |
| zstd frame | `frameContentSize` in the frame header | Preallocate declared size | Never allocate from it; stream with an output cap, use the declaration only as an after-the-fact consistency check |
| Parquet page | `uncompressed_page_size` in the page header | Allocate declared size per page | Validate declared totals against limits from footer metadata before any page is materialized |
| Any codec | ratio between input and output | Unbounded expansion | `max_expansion_ratio` evaluated incrementally during the read |

## Measured findings against real libraries

Both were found by `tests/test_hostile_inputs.py` on this host
(python-zstandard 0.25.0 / libzstd 1.5.7, pyarrow 25.0.1) and both changed the implementation.

**F-1 — `ZstdDecompressor.decompress(data, max_output_size=N)` is not a security boundary.**
A 32,787-byte frame declaring 1 GiB of content decompressed **fully and without error**
under `max_output_size=16 MiB`. The cap is only consulted for frames that do *not*
declare a content size; when a size is declared the library honours the declaration.
Any code using this argument to bound untrusted input is vulnerable.
→ `security/safe_decompress.py` does not use it.

**F-2 — bounding input per call does not bound output.**
`ZstdDecompressionObj.decompress(chunk)` limits how much *compressed* data is fed per
call, not how much is produced. Feeding the entire 32 KiB bomb in one chunk produced
1 GiB inside a single call (~4.7 s) before any limit could be evaluated.
`stream_reader().read(n)` is the only primitive tested here that bounds **output**.
→ Phase 1 of `safe_zstd_decompress` uses `stream_reader`; measured rejection of the
same bomb is **5 ms at +0.0 MiB peak RSS**.

**F-3 — clean EOF does not mean a complete frame.**
`stream_reader` returns a truncated frame's partial contents and then reports EOF with
no error, so the caller cannot distinguish real data from an attacker-chosen prefix —
an unsafe partial extraction. → Phase 2 verifies completeness (declared-size consistency,
or a bounded `decompressobj` pass when no size is declared) and raises
`MalformedCompressedInput`. Partial output is **never** returned.

## Configured limits

`security/safe_decompress.py :: DecompressionLimits` (defaults; tune per deployment):

| Limit | Default | Rationale |
| --- | --- | --- |
| `max_compressed_bytes` | 256 MiB | Reject oversized inputs before decoding |
| `max_output_bytes` | 1 GiB | Hard ceiling on produced bytes |
| `max_expansion_ratio` | 100× | Catches bombs whose absolute size is modest |
| `timeout_s` | 30 s | Decoder cancellation |
| `chunk_bytes` | 1 MiB | Output-bounded read granularity |
| `max_entries` | **N/A** | Parquet page codecs are not archives |
| `max_nesting_depth` | **N/A** | as above |

**Not applicable, recorded rather than omitted:** nested-archive depth, entry count, and
path traversal do not apply to Parquet page codecs in P1. They become live for the
streaming-log workload (S1) if it ever ingests archive containers.

## Test coverage

`tests/test_hostile_inputs.py` — 9 cases, all passing. Assertions are on **bounded time
and bounded memory**, not merely "an exception was raised": a decoder that materializes
1 GiB and then complains has already lost.

Mutation fuzzing: 120 seeded byte-flip cases (seed 20260810) against a Parquet file, each
in a child process under `RLIMIT_AS = 512 MiB` and a 20 s timeout.
Outcome: 55 rejected, 65 read successfully (flips landing in data regions), **0 crashes,
0 OOMs, 0 timeouts**. Any crash/OOM/timeout case is minimized and preserved under
`tests/fixtures/` as a regression fixture.

## Known gaps

- Fuzz corpus is small (120 cases) and single-seed. Escalate to a longer campaign before
  production reliance.
- No coverage yet of dictionary-related failures (wrong/missing/version-drifted dictionary).
  Deferred to the dictionary iteration; the decoder must reject clearly and safely.
- Limits are enforced in our wrapper, not inside the query engine. A production rollout
  must confirm the engine's own Parquet reader enforces equivalent bounds, since it, not
  this wrapper, is what reads from object storage.
