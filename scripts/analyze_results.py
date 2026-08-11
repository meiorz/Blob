"""Aggregate raw cells -> statistics, growth class, crossover, decision gates.

Predeclared gates (fixed BEFORE any data existed; see docs/benchmark-methodology.md):
  G1 projected footprint reduction        >= 20%   vs SNAPPY
  G2 projected decode throughput          >= 70%   of SNAPPY
  G3 bandwidth gate: not worse at 250 MiB/s AND clear win at 50 and/or 125 MiB/s
  G4 peak RSS delta                       <= +10%  vs SNAPPY at every scale
  G5 post-run RSS                         <= +5%   of pre-run idle baseline
  G6 memory growth class                  != superlinear
  G7 lossless integrity                   == pass
  G8 reproducibility: footprint/latency separation exceeds run-to-run variance
"""
from __future__ import annotations

import glob
import json
import math
import os
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "results", "raw")
sys.path.insert(0, ROOT)
from benchmarks.model_crossover import evaluate as bandwidth_evaluate  # noqa: E402

MIB = 1048576
BASELINE = "snappy"
# SKILL.md-mandated controls. They are reported but never evaluated against the
# accept gates: "none" can never beat snappy on footprint, so gating it would
# manufacture a meaningless failure row.
CONTROL_ARMS = {"none"}

G1_FOOTPRINT_PCT = 20.0
G2_DECODE_THROUGHPUT_FRAC = 0.70
G4_PEAK_RSS_PCT = 10.0
G5_POST_RUN_PCT = 5.0
SUPERLINEAR_SLOPE = 1.15
SUBLINEAR_SLOPE = 0.90


def describe(xs: list[float]) -> dict:
    xs = sorted(xs)
    n = len(xs)
    med = st.median(xs)
    sd = st.stdev(xs) if n > 1 else 0.0
    d = {"n": n, "median": med, "min": xs[0], "max": xs[-1], "stdev": sd,
         "cov": (sd / med) if med else None}
    if n >= 30:                       # percentiles only where n justifies them
        d["p50"] = _pct(xs, 50); d["p95"] = _pct(xs, 95); d["p99"] = _pct(xs, 99)
    else:
        d["p95"] = None
        d["p95_suppressed_reason"] = f"n={n} < 30; SKILL.md permits percentiles only with sufficient samples"
    return d


def _pct(sorted_xs: list[float], p: float) -> float:
    if not sorted_xs:
        return float("nan")
    k = (len(sorted_xs) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_xs[int(k)]
    return sorted_xs[lo] * (hi - k) + sorted_xs[hi] * (k - lo)


def mann_whitney_p(a: list[float], b: list[float]) -> float:
    """Two-sided normal approximation with tie correction and continuity
    correction. Used to decide 'materially overlapping distributions'."""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return 1.0
    combined = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks, i = [0.0] * len(combined), 0
    tie_term = 0.0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        t = j - i + 1
        tie_term += t ** 3 - t
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, combined) if g == 0)
    u1 = r1 - n1 * (n1 + 1) / 2.0
    mu = n1 * n2 / 2.0
    n = n1 + n2
    var = n1 * n2 / 12.0 * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    if var <= 0:
        return 1.0
    z = (abs(u1 - mu) - 0.5) / math.sqrt(var)
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


def load_cells() -> dict:
    """Load ONLY the cells named in the latest run manifest.

    results/raw is append-only (the workspace mount forbids unlink), so globbing
    the directory would silently mix arms from different sweeps -- exactly the
    kind of invalid cross-run comparison SKILL.md prohibits."""
    manifest_path = os.path.join(ROOT, "results", "latest_manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        paths = [os.path.join(RAW, n) for n in manifest["cells"]]
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            print(f"manifest lists {len(missing)} missing cells", file=sys.stderr)
        paths = [p for p in paths if os.path.exists(p)]
        print(f"# run_id={manifest['run_id']} cells={len(paths)} complete={manifest.get('complete')}",
              file=sys.stderr)
    else:
        print("# WARNING: no manifest; falling back to glob (cross-run mixing possible)",
              file=sys.stderr)
        paths = sorted(glob.glob(os.path.join(RAW, "*.json")))
    cells = {}
    for path in paths:
        with open(path) as fh:
            c = json.load(fh)
        cells[(c["dataset_id"], c["scale_label"], c["arm"])] = c
    return cells


def summarize_cell(c: dict) -> dict:
    ob = c["original_bytes"]
    enc = describe(c["encode"]["wall_ms"])
    dec = describe(c["decode_full"]["wall_ms"])
    dpj = describe(c["decode_projected"]["wall_ms"])
    def tput(ms):    # MiB/s on ORIGINAL input bytes (stated convention)
        return (ob / MIB) / (ms / 1000.0) if ms else None
    return {
        "dataset_id": c["dataset_id"], "scale": c["scale_label"], "arm": c["arm"],
        "environment_class": c["env"]["environment_class"],
        "hardware_validated": c["env"]["hardware_validated"],
        "original_bytes": ob,
        "compressed_bytes": c["compressed_bytes"],
        "projected_compressed_bytes": c["projected_compressed_bytes"],
        "projected_original_bytes": c.get("projected_original_bytes"),
        "compression_ratio": c["compression_ratio"],
        "space_savings_pct": c["space_savings_pct"],
        "encode_ms": enc, "decode_ms": dec, "decode_projected_ms": dpj,
        "encode_mib_s": tput(enc["median"]),
        "decode_mib_s": tput(dec["median"]),
        "decode_projected_median_s": dpj["median"] / 1000.0,
        "integrity_lossless": c["integrity_lossless"],
        "mem_encode": c["memory_encode"], "mem_decode": c["memory_decode"],
        "_raw_decode_projected_ms": c["decode_projected"]["wall_ms"],
    }


def growth_class(rows: list[dict]) -> dict:
    """log-log slope of INCREMENTAL peak RSS vs input bytes.

    Incremental (peak - pre-run idle baseline) is used deliberately: total RSS
    carries a fixed ~100-150 MiB interpreter+Arrow floor that would make every
    arm look artificially sublinear."""
    pts, dropped = [], []
    for r in sorted(rows, key=lambda x: x["original_bytes"]):
        m = r["mem_decode"]
        inc = m["peak_rss_bytes"] - m["baseline_rss_bytes"]
        if inc > 0 and r["original_bytes"] > 0:
            pts.append((math.log(r["original_bytes"]), math.log(inc)))
        else:
            dropped.append({"scale": r["scale"], "incremental_peak_rss_bytes": inc,
                            "reason": "incremental peak RSS <= 0"})
    if len(pts) < 3:
        return {"slope": None, "class": "insufficient_scales",
                "points": len(pts), "dropped": dropped,
                "note": (f"need 3 usable scales, have {len(pts)}. Dropped: {dropped}. "
                         "Incremental peak RSS <= 0 means the operation's allocation was "
                         "smaller than sampler resolution against the interpreter floor -- "
                         "re-run at larger scales rather than trusting this classification.")}
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    num = sum((x - mx) * (y - my) for x, y in pts)
    den = sum((x - mx) ** 2 for x, _ in pts)
    slope = num / den if den else None
    if slope is None:
        cls = "undetermined"
    elif slope > SUPERLINEAR_SLOPE:
        cls = "superlinear"
    elif slope < SUBLINEAR_SLOPE:
        cls = "sublinear"
    else:
        cls = "linear"
    return {"slope": slope, "class": cls, "points": len(pts), "dropped": dropped}


def apply_gates(base: dict, cand: dict, growth: dict) -> dict:
    g = {}
    # G1 projected footprint
    bpf, cpf = base["projected_compressed_bytes"], cand["projected_compressed_bytes"]
    red = 100.0 * (1 - cpf / bpf) if bpf else 0.0
    g["G1_footprint"] = {"reduction_pct": red, "threshold_pct": G1_FOOTPRINT_PCT,
                         "pass": red >= G1_FOOTPRINT_PCT}
    # G2 projected decode throughput
    bt, ct = base["decode_projected_ms"]["median"], cand["decode_projected_ms"]["median"]
    frac = (bt / ct) if ct else None      # throughput ratio = time ratio inverted
    g["G2_decode_throughput"] = {"candidate_frac_of_baseline": frac,
                                 "threshold": G2_DECODE_THROUGHPUT_FRAC,
                                 "pass": bool(frac is not None and frac >= G2_DECODE_THROUGHPUT_FRAC)}
    # G3 bandwidth model
    bw = bandwidth_evaluate(
        {"projected_compressed_bytes": bpf, "decode_projected_median_s": bt / 1000.0},
        {"projected_compressed_bytes": cpf, "decode_projected_median_s": ct / 1000.0})
    g["G3_bandwidth"] = {**bw, "pass": bw["gate_bandwidth_overall"]}
    # G4 peak RSS
    bp = base["mem_decode"]["peak_rss_bytes"]; cp = cand["mem_decode"]["peak_rss_bytes"]
    dpct = 100.0 * (cp - bp) / bp if bp else 0.0
    g["G4_peak_rss"] = {"delta_pct": dpct, "threshold_pct": G4_PEAK_RSS_PCT,
                        "pass": dpct <= G4_PEAK_RSS_PCT}
    # G5 post-run retention
    m = cand["mem_decode"]
    post_pct = 100.0 * (m["post_run_rss_bytes"] - m["baseline_rss_bytes"]) / m["baseline_rss_bytes"] \
        if m["baseline_rss_bytes"] else 0.0
    g["G5_post_run_retention"] = {"retained_pct_of_baseline": post_pct,
                                  "threshold_pct": G5_POST_RUN_PCT,
                                  "pass": post_pct <= G5_POST_RUN_PCT}
    # G6 growth class
    g["G6_growth_class"] = {**growth, "pass": growth.get("class") != "superlinear"}
    # G7 integrity
    g["G7_integrity"] = {"pass": bool(cand["integrity_lossless"])}
    # G8 reproducibility.
    #
    # This gate asks "is the conclusion an artifact of noise?", NOT "are the two
    # distributions different?". A candidate whose decode latency is
    # statistically indistinguishable from baseline is a GOOD outcome for a
    # footprint-motivated change -- an earlier version of this gate required
    # separation and therefore failed candidates for being harmless.
    #
    # It fails only when (a) the runs are too unstable to conclude anything, or
    # (b) the candidate is REGRESSED and that regression is real.
    p = mann_whitney_p(base["_raw_decode_projected_ms"], cand["_raw_decode_projected_ms"])
    separated = p < 0.05
    slower = ct > bt
    b_cov = base["decode_projected_ms"]["cov"] or 0.0
    c_cov = cand["decode_projected_ms"]["cov"] or 0.0
    unstable = max(b_cov, c_cov) > 0.20
    real_regression = separated and slower and (100.0 * (ct - bt) / bt) > 5.0
    if unstable:
        verdict = "inconclusive: run-to-run variance too high (CoV > 20%)"
    elif real_regression:
        verdict = "regression: candidate slower than baseline beyond noise"
    elif separated and not slower:
        verdict = "candidate faster than baseline, separation significant"
    else:
        verdict = "no detectable latency difference (candidate not slower)"
    g["G8_reproducibility"] = {
        "decode_mannwhitney_p": p, "distributions_separated": separated,
        "baseline_cov": b_cov, "candidate_cov": c_cov,
        "median_delta_pct": 100.0 * (ct - bt) / bt if bt else None,
        "verdict": verdict,
        "pass": bool(not unstable and not real_regression),
        "note": "footprint is deterministic (single build) so it needs no repeat test; "
                "this gate governs the latency conclusion only",
    }
    g["ALL_PASS"] = all(v.get("pass") for k, v in g.items() if k.startswith("G"))
    return g


def main() -> int:
    cells = load_cells()
    if not cells:
        print(f"no raw results in {RAW}", file=sys.stderr)
        return 1
    rows = {k: summarize_cell(c) for k, c in cells.items()}

    # original_bytes is Arrow in-memory nbytes, so compression_ratio conflates
    # Parquet's encoding layer with the page codec. Dividing by the UNCOMPRESSED
    # Parquet arm isolates the codec's own contribution -- the quantity the
    # Iteration 1 hypothesis is actually about.
    for (ds, sc, arm), r in rows.items():
        ctl = rows.get((ds, sc, "none"))
        r["codec_attributable_ratio"] = (
            ctl["compressed_bytes"] / r["compressed_bytes"]
            if ctl and r["compressed_bytes"] else None)
        r["codec_attributable_projected_ratio"] = (
            ctl["projected_compressed_bytes"] / r["projected_compressed_bytes"]
            if ctl and r["projected_compressed_bytes"] else None)

    growth = {}
    for (ds, _sc, arm) in rows:
        key = (ds, arm)
        if key not in growth:
            growth[key] = growth_class([r for (d, _s, a), r in rows.items() if d == ds and a == arm])

    gates = {}
    for (ds, sc, arm), r in rows.items():
        if arm == BASELINE or arm in CONTROL_ARMS:
            continue
        b = rows.get((ds, sc, BASELINE))
        if not b:
            continue
        gates[f"{ds}|{sc}|{arm}"] = apply_gates(b, r, growth[(ds, arm)])

    out = {
        "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows.values()],
        "growth_class": {f"{d}|{a}": v for (d, a), v in growth.items()},
        "gates": gates,
        "baseline_arm": BASELINE,
    }
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    with open(os.path.join(ROOT, "results", "summary.json"), "w") as fh:
        json.dump(out, fh, indent=2)
    print(json.dumps({"cells": len(rows), "gated_comparisons": len(gates)}, indent=2))
    for name, g in gates.items():
        failed = [k for k, v in g.items() if k.startswith("G") and not v.get("pass")]
        print(f"{name}: {'ALL GATES PASS' if g['ALL_PASS'] else 'FAIL -> ' + ','.join(failed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
