# Large-Scale Compression Research and Optimization

## Mission

Identify, validate, and document measurable improvements to a defined large-data compression workload.

An improvement is valid only when it is reproducible and does not violate correctness, memory, latency, compatibility, security, or quality constraints.

---

## Scope lock

Before implementation, identify one primary workload and one secondary workload.

For each workload, define:

- Data format and schema
- Read/write ratio
- Latency and throughput SLOs
- Maximum memory budget
- Deployment environment
- Durability and compatibility requirements
- Whether lossy compression is allowed

Do not add codec integrations or benchmark scenarios outside those workloads unless they are explicitly approved.

---

## Non-negotiable rules

- Establish a baseline before optimization.
- Change one meaningful variable per iteration.
- Benchmark compression, decompression, memory, correctness, and security together.
- Use representative data and record provenance.
- Do not optimize for compression ratio alone.
- Do not claim improvement without reproducible evidence.
- Treat externally supplied compressed data as hostile.
- Keep or reject changes using the documented decision gates.
- Prefer small, controlled iterations over broad rewrites.
- Always include a no-compression control and an already-compressed-data control.

---

## Chat response after each loop

After updating the repository artifacts for an iteration, respond in chat using the format defined in **“Output format after each loop”** below. Do not add extra narrative outside that template.

---

## Measurement conventions

- Store byte quantities as integer bytes in raw benchmark JSON/CSV.
- Display memory in MiB and data sizes in MiB/GiB in Markdown reports.
- Store durations in milliseconds; calculate throughput in MiB/s.
- State whether throughput uses original bytes or compressed bytes; default to original input bytes.
- Use monotonic clocks for timing.
- Report medians and variability across runs, not only a single best run.
- Define p50/p95/p99 only for workloads with sufficient repeated samples.

---

## Core objective

Find measurable weaknesses in current compression pipelines and propose fixes that improve one or more of:

- Compression ratio
- Compression throughput
- Decompression throughput
- Memory consumption
- CPU and energy cost
- Query/read latency
- Random-access performance
- Distributed-processing compatibility
- Data integrity and recovery
- Security against malicious compressed inputs
- Lossy-compression quality or downstream ML accuracy

Do not claim an improvement without reproducible benchmark evidence.

---

## Operating mode

Work in small, testable iterations. Do not attempt a massive rewrite or broad research dump before creating a baseline.

For every iteration, follow this exact loop:

1. Inspect the repository, existing documentation, code, tests, benchmarks, datasets, and prior results.
2. State one specific hypothesis in this format:

   `Hypothesis: If we change <X>, then metric <Y> will improve by <target> on workload <Z>, because <reason>.`

3. Select or create a representative dataset. Label its characteristics:
   - Data type: text, JSON, CSV, time series, logs, tabular, binary, image, media, mixed
   - Size and record count
   - Entropy/compressibility estimate where possible
   - Cardinality and repetition characteristics
   - Sorted versus unsorted state
   - Already-compressed versus raw
   - Access pattern: sequential scan, random read, column scan, streaming, archival
   - Lossless or allowed lossy error bound
4. Establish a baseline using the current pipeline or a documented default configuration.
5. Change exactly one meaningful variable unless testing a deliberately defined interaction.
6. Run benchmark, correctness, security, and memory-profile checks.
7. Compare results against the baseline.
8. Keep, revise, or reject the change using the decision rules below.
9. Update project artifacts before starting another iteration.
10. Repeat until the success criteria are met, evidence shows the approach is unproductive, or a blocking constraint is identified.

---

## Mandatory investigation areas

Systematically evaluate these failure modes. Mark each as `unexamined`, `tested`, `validated`, `rejected`, or `not applicable`.

- Wrong codec for the workload
- Compression ratio prioritized over latency, CPU, memory, or cost
- Poor chunk/block/row-group sizing
- Compression of already-compressed or high-entropy data
- Row-oriented layout where a columnar layout is appropriate
- Lack of preconditioning: sorting, delta encoding, bit packing, RLE, dictionary encoding, schema normalization, or record clustering
- Missing custom dictionaries for repetitive small payloads
- Dictionary drift, versioning failures, or incompatible decoder behavior
- Expensive recompression across pipeline stages
- Poor parallelization or non-splittable compressed files
- Weak random-access and partial-read behavior
- Fragmentation from incremental writes/updates
- Corruption propagation and insufficient checksums/recovery
- Zip bombs, decompression bombs, recursion bombs, and resource exhaustion
- Unsafe parsing of attacker-controlled compressed inputs
- Lossy compression that violates quality, accuracy, or domain constraints
- Benchmark bias caused by unrealistic datasets or warm-cache-only tests

Data layout and ordering are first-class optimization targets: grouping similar records or reordering them can materially improve compression ratio in column-oriented systems.

---

## Codec and technology matrix

Evaluate only technologies appropriate to the target data and environment. Include at least:

- Zstandard: multiple levels and optional trained dictionaries
- LZ4 or Snappy: low-latency baseline
- Gzip/DEFLATE: compatibility baseline
- Brotli: web/static-delivery scenario only
- Parquet or ORC: analytical/tabular scenario
- Domain encodings where applicable: delta, RLE, dictionary, bit packing, quantization, transform coding
- No compression: required control case
- Existing compressed formats: required “do not recompress” control case

Do not declare a universal winner. Codec choice is workload-dependent, and compression uses compute resources in exchange for lower byte volume.

---

## Benchmark requirements

For every benchmark, capture:

- Dataset identifier and SHA-256 hash
- Dataset size before compression
- Codec, codec version, level, and configuration
- Dictionary ID and SHA-256, if used
- Chunk/block/row-group size
- Hardware and OS metadata
- Python/runtime/compiler/library versions
- Thread count and concurrency level
- Cold-cache and warm-cache conditions, when relevant
- Compression time and throughput
- Decompression time and throughput
- Original size, compressed size, and compression ratio
- Peak RSS / memory use
- CPU utilization or process CPU time
- p50, p95, and p99 latency for request/read workloads
- Random-access cost where supported
- Data-integrity result after round trip
- For lossy tests: quality metric and domain-specific error/accuracy metric
- For security tests: whether configured limits safely reject hostile inputs

Use the following metrics:

```text
compression_ratio       = original_bytes / compressed_bytes
space_savings_pct       = 100 * (1 - compressed_bytes / original_bytes)
compression_throughput  = original_bytes / compression_seconds
decompression_throughput= original_bytes / decompression_seconds
```

### CPU and concurrency notes

- Record wall-clock time, process CPU time, and thread count.
- Distinguish single-threaded and multi-threaded runs explicitly.
- Do not compare results with different thread counts without labeling the comparison invalid.
- Where feasible, pin CPU affinity for performance-sensitive runs or document why it is not possible.

### I/O and cache isolation

- State whether input is generated in-process, read from local SSD, or read from network/remote storage.
- Separate codec-only benchmarks from end-to-end pipeline benchmarks.
- For codec-only tests, avoid disk writes on the hot path unless storage I/O is the metric under test.
- Clearly label cold-cache versus warm-cache runs.

### Run counts and variance

- Run at least 5 trials for local performance claims and at least 10 trials in noisy or shared environments.
- Report median, minimum, maximum, and a variability measure (e.g., standard deviation or coefficient of variation).
- Treat a result as inconclusive when candidate and baseline distributions materially overlap.

Never compare results collected with different datasets, different machines, materially different concurrency, or inconsistent cache states without labeling the comparison invalid.

---

## Correctness and compatibility requirements

For lossless compression:

- Verify byte-for-byte equality after every compression/decompression cycle.
- Verify chunked and parallel decoding where supported.
- Test empty data, tiny files, incompressible data, very large files, repeated values, Unicode text, malformed input, truncated streams, and corrupted frames.
- Verify that the decoder rejects a missing or incorrect dictionary clearly and safely.
- Store a checksum per output artifact and per independently decodable block when architecture permits.
- Record encoder and decoder versions.
- Test decoding artifacts produced by previously supported versions.
- Where relevant, test whether repeated compression with identical settings produces identical output; if not, document why.

For lossy compression:

- Define the approved error budget before implementation.
- Measure objective quality metrics and domain-specific utility.
- Reject any configuration exceeding the error budget, even if its compression ratio is better.
- Never use lossy compression for data labeled lossless, forensic, financial, legal, or integrity-critical.

---

## Security requirements

Treat all externally supplied compressed data as hostile.

Implement and test:

- Maximum compressed input size
- Maximum decompressed output size
- Maximum expansion ratio
- Maximum nested archive depth
- Maximum file and entry count
- Decoder timeout or cancellation support
- Per-task CPU and memory limits where feasible
- Safe handling of malformed headers, truncated frames, corrupted checksums, and incorrect dictionaries
- No extraction of untrusted archives outside a controlled directory
- No path traversal during archive extraction

A decompression bomb is a deliberately small archive that expands massively and can exhaust system resources; defenses must limit expansion and extraction resources.

### Fuzzing and hostile-input testing

- Add mutation-based fuzz tests covering malformed headers, truncated streams, invalid length fields, corrupted checksums, incorrect dictionaries, and nested archives.
- Assert safe failure: bounded time, bounded memory, no crash, and no unsafe partial extraction.
- Preserve minimized crash cases as regression fixtures.

---

## Decision rules

Accept a change only if all are true:

- It passes correctness, regression, security, and memory tests.
- It improves the primary target metric by at least the predeclared threshold.
- It does not regress any hard constraint: latency, memory, cost, compatibility, quality, or security.
- It is reproducible across at least the required number of runs.
- The median improvement exceeds ordinary run-to-run variance.
- Configuration and operational complexity are justified by the gain.

Reject or revert a change if:

- It improves ratio but violates latency, memory, quality, or safety budgets.
- It depends on nonrepresentative data.
- It is not reproducible.
- It creates an incompatible artifact without an explicit migration path.
- It adds operational complexity with negligible measured benefit.

---

## Required repository artifacts

Maintain these files as work progresses:

```text
README.md
docs/problem-statement.md
docs/architecture.md
docs/codec-decision-record.md
docs/dataset-catalog.md
docs/security-threat-model.md
docs/benchmark-methodology.md
docs/results.md
docs/iteration-log.md
benchmarks/
tests/
scripts/
data/metadata/
results/
```

### `docs/iteration-log.md`

Append one entry per iteration:

```md
## Iteration <N>: <short title>

- Date:
- Hypothesis:
- Dataset:
- Baseline:
- Change:
- Command(s) run:
- Results:
- Correctness result:
- Security result:
- Memory baseline:
- Memory candidate:
- Peak RSS delta:
- Post-run retained memory:
- Growth classification:
- Decision: keep / revise / reject
- Reason:
- Next hypothesis:
```

### `docs/results.md`

Maintain a compact, current table:

```md
| ID | Dataset | Configuration | Ratio | Encode MB/s | Decode MB/s | Peak RSS MB | Peak USS MB | Post-run RSS MB | Mem/Input MB | Mem/Output MB | Growth Class | p95 Latency | Integrity | Security | Decision |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|---|---|
```

Each row represents one configuration on one dataset under a clearly specified workload.

---

## Output format after each loop

At the end of each completed iteration, after updating repository artifacts, output only:

```md
## Iteration <N>

**Hypothesis:** ...

**Change made:** ...

**Evidence:**
- Dataset: ...
- Baseline: ...
- Candidate: ...
- Delta: ...

**Validation:**
- Lossless integrity: pass/fail
- Regression tests: pass/fail
- Security tests: pass/fail
- Peak RSS within budget: pass/fail
- Post-run memory returned near baseline: pass/fail
- Reproducibility: pass/fail

**Decision:** keep / revise / reject

**Why:** ...

**Next action:** ...
```

Do not use vague statements such as “faster,” “better,” “significant,” or “optimized” without exact measurements and a named baseline.

---

## Final deliverable criteria

Do not consider the project complete until it includes:

1. A threat model for compression and decompression inputs.
2. A reproducible benchmark harness.
3. Representative datasets and documented provenance.
4. At least one baseline and at least three candidate strategies.
5. Correctness tests, fuzz/property tests where feasible, and hostile-input tests.
6. A codec/configuration decision matrix by workload.
7. Evidence-backed recommended defaults.
8. Explicit non-recommendations, including cases where compression should be skipped.
9. A rollback and compatibility plan for persisted compressed data.
10. A short executive summary with measured gains, trade-offs, limitations, and next experiments.

Start by inspecting the repository and producing the first hypothesis. Do not implement broad changes until a baseline benchmark and dataset catalog exist.

---

## Memory footprint profiling

Memory profiling is mandatory for every benchmark run. Do not accept a compression change based only on ratio or throughput.

For each encode and decode benchmark, measure and record:

- Peak RSS
- Peak USS if available
- Average RSS during steady-state execution
- Allocation growth during the run
- Memory per worker/thread
- Memory per MB of input
- Memory per MB of compressed output
- Temporary scratch-buffer size
- Decoder working-set size for random-access reads
- Memory behavior under concurrency
- Memory behavior on worst-case inputs, including incompressible and hostile-expansion cases

Use at least one process-level metric and one language/runtime-level metric when possible.

### Required memory metrics

```text
peak_rss_mb          = peak_resident_set_bytes / 1048576
peak_uss_mb          = peak_unique_set_bytes   / 1048576
memory_per_input_mb  = peak_resident_set_bytes / original_input_bytes   * 1048576
memory_per_output_mb = peak_resident_set_bytes / compressed_output_bytes * 1048576
memory_amplification = peak_resident_set_bytes / max(original_input_bytes, compressed_output_bytes)
```

Record memory separately for:

- Compression
- Decompression
- Streaming compression
- Streaming decompression
- Random-access decode
- Parallel encode/decode
- Dictionary training
- Dictionary-assisted compression/decompression

### Profiling rules

- Measure cold and warm runs separately when cache effects can change memory behavior.
- Sample memory frequently enough to catch short-lived spikes; do not rely only on start/end snapshots.
- If the runtime uses GC, record whether a collection occurred during the run.
- If the runtime uses arenas, pools, or mmap, note that explicitly.
- Report whether memory returns to baseline after the task completes.
- Track file-backed mappings separately from anonymous memory where tooling allows.
- For multi-process workers, record parent and child memory independently and as a total.

### Python-specific guidance

If the benchmark harness is in Python, collect:

- `resource.getrusage()` or platform equivalent for max RSS.
- `psutil.Process().memory_info()` and `memory_full_info()` where available.
- `tracemalloc` snapshots for Python-heap attribution.
- Optional line-level profiling with `memory_profiler` for hotspots.
- GC stats before and after the benchmark.

Example instrumentation checklist:

- Start process sampler before the benchmark begins.
- Capture baseline idle RSS.
- Start `tracemalloc` before the operation.
- Run benchmark.
- Stop sampler and collect peak RSS/USS.
- Take end snapshot and compare top allocators.
- Force GC once after the run and measure post-run retained memory.
- Save metrics to structured output.

### Memory regression gates

Reject a change if any of these occur unless the hypothesis explicitly allows the tradeoff:

- Peak RSS increases more than the declared budget.
- Peak RSS exceeds deploy target or container limit.
- Memory amplification is materially worse than baseline.
- Memory grows superlinearly with input size.
- Random-access decode requires loading the entire object.
- Parallel decompression causes per-worker memory blowup.
- Memory is not released after task completion.
- Hostile or malformed input triggers unbounded allocation.

### Required scaling tests

Run each accepted candidate on at least three input scales, for example:

- Small
- Medium
- Large

Then classify memory growth as:

- Sublinear
- Linear
- Superlinear

Any unexplained superlinear memory growth is a blocker.