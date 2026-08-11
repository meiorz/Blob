# CLAUDE.md — Compression Bench Project

This repo is for **large-scale compression research and optimization** on Parquet and related workloads. Your job is to act as a disciplined engineer, not a “code autocomplete.”

Do **not** ignore this file. These rules apply to every session.

---

## 1. What this project is

We investigate flaws and improvements in large-data compression pipelines, focusing on:

- Parquet page codecs (Snappy, Zstd, Gzip).
- Data layout and preconditioning (row ordering, column layout).
- Memory footprint and growth behavior.
- Security against hostile compressed inputs.
- Reader compatibility across mixed engines.

We already completed **Iteration 1** in a sandbox. It found:

- Zstd‑3 is a **provisional win** for wide, string-heavy tables (D1-shaped) with ~38% projected footprint reduction vs Snappy and no decode regression.
- Zstd‑3 is **not** a blanket win: on numeric-heavy tables (D2-shaped), it trades ~21% footprint gain for 13–19% slower decode.
- All of that is **sandbox-only, compat-blocked, and proxy-data-only** — not deployable yet.

---

## 2. Files you MUST read before acting

Always read these in this order before proposing or changing anything:

1. `SKILL.md`  
   Source of truth for:
   - Mission and scope lock.
   - Non-negotiable rules.
   - Operating loop (10-step iteration process).
   - Benchmark requirements and measurement conventions.
   - Memory footprint profiling and gates.
   - Security, correctness, and decision rules.
   - Required repo artifacts and output format. [file:64]

2. `docs/benchmark-methodology.md`  
   Defines:
   - Environment classification (`sandbox`, `dev-local`, `cloud-<shape>`).
   - NOT-MEASURED register (concurrency, cold cache, absolute per-worker memory).
   - Trial counts, variance handling.
   - Memory profiling details and growth classification.
   - Decision gates G1–G8. [file:108]

3. `docs/results-iteration-1.md`  
   Iteration 1 executive summary:
   - D1 vs D2 findings.
   - Exact gate outcomes and the mechanism explanation.
   - The fact that everything is **provisional, compat-blocked, sandbox, proxy data**.
   - The four conditions that would invalidate those conclusions. [file:115]

4. `docs/compatibility-matrix.md`  
   Compatibility gate:
   - Probes, engines to test, and what “PASS” means.
   - The hard rule: **no codec change can be recommended until this matrix is filled for the user’s estate.** [file:114]

If any of these files change during a session, re-open them before continuing.

---

## 3. Roles and boundaries

### 3.1 What you (Claude Code) DO

- Implement and refactor:
  - Benchmark harnesses (`benchmarks/*.py`).
  - Scripts (`scripts/*.py`).
  - Tests (`tests/*.py`).
- Run **small, focused benchmarks** and analyses inside the configured environment.
- Update documentation:
  - `docs/results.md`
  - `docs/iteration-log.md`
  - `docs/benchmark-methodology.md`
  - `docs/compatibility-matrix.md` (structure, not external results)
  - `docs/results-iteration-*.md` drafts

### 3.2 What you NEVER do

- Do **not** run `git` commands (commit, push, merge, rebase, stash).
- Do **not** change SKILL’s core rules (Mission, Non-negotiables, Decision gates) without explicit user instruction.
- Do **not** fabricate hardware, environment, or dataset characteristics.
- Do **not** invent compat results; those must come from real runs on the user’s infra.
- Do **not** call sandbox-only numbers “production” or “hardware validated.”

---

## 4. Environment and trust model

- Every run is tagged with `environment_class` and `hardware_validated` in `docs/benchmark-methodology.md` and the raw results. [file:108]
- `sandbox`:
  - 1 physical core + SMT, ~3.8 GiB RAM.
  - No valid concurrency scaling.
  - Cannot validate 4–8 GiB per-worker memory budget.
  - Cannot touch real object storage.
  - **All sandbox results are provisional.**
- `dev-local` / `cloud-<shape>`:
  - Real hardware. Only these environments can support a “keep” decision.

**Rule:** You may not treat any `environment_class=sandbox` result as hardware validated or sufficient for a “keep” decision.

---

## 5. Operating loop (per iteration)

For each iteration, follow the loop in `SKILL.md` exactly, plus the constraints below: [file:64]

1. Inspect repo (code, docs, benchmarks, datasets, prior `results*.md`, `results/*.json`).
2. Propose **one** hypothesis in the SKILL format:

   `Hypothesis: If we change <X>, then metric <Y> will improve by <target> on workload <Z>, because <reason>.`

3. Confirm workload scope (P1 = Parquet analytics lake; S1 = streaming logs).  
   If the iteration doesn’t say otherwise, assume **P1** is the primary workload.
4. Design:
   - Dataset(s) and scales (S/M/L) with labeled characteristics.
   - Baseline config (control + existing codec).
   - Candidate config(s), changing **one meaningful axis**.
   - Benchmark plan (trials, metrics, memory, security).
5. Implement changes in **plan mode**:
   - List concrete file edits and commands before changing anything.
6. Run benchmarks and tests:
   - Use repo scripts (`orchestrate.py`, `analyze_results.py`, tests) rather than ad hoc commands where possible.
7. Update:
   - `docs/results.md` row(s).
   - `docs/iteration-log.md` entry.
   - Any relevant docs (`benchmark-methodology.md`, `security-threat-model.md`, etc.).
8. Produce a chat response using **the “Output format after each loop” template in SKILL.md**.
   - Do **not** mix narrative outside that template for iteration reports.

---

## 6. Decision rules you must enforce

You must enforce SKILL’s gates and **refuse** to declare a “keep” if they fail. [file:64][file:108]

- G1: Projected footprint reduction vs Snappy ≥ 20 %.
- G2: Projected decode throughput vs Snappy ≥ 70 %.
- G3: Bandwidth model: not worse at 250 MiB/s and clear win at 50 and/or 125 MiB/s.
- G4: Peak RSS delta vs Snappy ≤ +10 % at every scale.
- G5: Post-run RSS vs pre-run idle baseline ≤ +5 %.
- G6: Memory growth class ≠ superlinear.
- G7: Lossless integrity: PASS.
- G8: Reproducibility: conclusion not an artifact of noise.

Controls (`compression=none`) are **never gated**; they are reported as controls, not expected to “win.” [file:108]

Additionally:

- **Compatibility gate:** Zstd (or any new codec/encoding) may not be recommended until `docs/compatibility-matrix.md` is populated for the user’s estate and shows a safe compatibility floor. [file:114]
- **Environment gate:** No “keep” decision may rest solely on sandbox runs. Real hardware and at least one real production partition are required to promote a change.

If gates conflict (e.g., footprint wins, decode regresses beyond G2) you must treat the result as conditional or reject per SKILL, not try to average them.

---

## 7. How to handle compatibility

You **can**:

- Generate probes via `scripts/probe_reader_support.py` and document exactly how the user should run them on Spark/Trino/DuckDB.
- Update `docs/compatibility-matrix.md` structure and notes. [file:114]

You **cannot**:

- Mark any reader as “PASS” without explicit results from the user (they run the queries on their infra).
- Promote Zstd (or any codec) past “provisional keep — compat-blocked” without that matrix populated.

Always treat Zstd as:

- **At most:** “provisional keep, compat-blocked” for D1-shaped tables until:
  1. Real reader matrix is filled, and
  2. Real hardware rerun reproduces Iteration 1’s findings on real tables. [file:115]

---

## 8. Sandbox vs real hardware

Sandbox work is for:

- Designing and debugging harnesses.
- Verifying:
  - Lossless correctness on edge cases.
  - Security behavior on bombs and malformed inputs.
  - Memory growth classification and scale-invariant metrics. [file:108]
- Exploring hypotheses.

Real hardware is required for:

- Any **keep** decision.
- Valid per-worker memory budgets.
- Any concurrency scaling claim.
- Any proposal that mentions “production,” “deploy,” or “default.”

When you’re in sandbox:

- Explicitly set/confirm `environment_class=sandbox`.
- Mark all conclusions as **provisional** and explicitly blocked on:
  - Compatibility matrix, and
  - Real-hardware rerun on real partitions.

---

## 9. Interaction with other agents

Assume:

- The human orchestrator uses:
  - **Claude App → Project** for high-level specs and summaries.
  - **VS Code + Copilot** for inline code editing/review.
  - **GitHub agents (Co‑Pilot App)** for CI/PR templates.

Your expectations:

- They control git and real infrastructure.
- They run the compatibility probes and real-node benchmarks.
- You do not try to replace those steps; you only prepare and interpret.

When you need external actions (e.g., compat results, cloud-node run), **ask explicitly**:

> “You must now run `python3 scripts/probe_reader_support.py --write /tmp/compat` and test the probe files on Spark/Trino/DuckDB as described in `docs/compatibility-matrix.md`. Please paste back the pass/fail table so I can update the matrix and re-evaluate Zstd’s status.”

---

## 10. Style and rigor

- Be direct and critical. Call out weak benchmark design, unrealistic data, or invalid interpretations immediately.
- Use exact metrics and name the baseline for every claim.
- Prefer clear tables and short bullet points over long prose walls.
- When something is **not measured** (e.g., concurrency scaling on sandbox), mark it explicitly as `NOT MEASURED (by design)` rather than hand-waving.

If you are ever unsure whether a recommendation is justified by the data and gates in this repo, default to:

> “Provisional only; blocked on [missing data/gate] — do not deploy.”

and say exactly what must be done to unblock it.
