"""Object-store cost model.

Measured on local buffers a better compression ratio buys nothing, so bytes and
CPU are measured separately and composed:

    scan_time(B) = projected_compressed_bytes / B  +  projected_decode_seconds

B = per-worker effective object-store bandwidth (bytes/s). Solving baseline vs
candidate for equality gives the crossover bandwidth:

    Cs/B + Ds = Cz/B + Dz   ->   B* = (Cs - Cz) / (Dz - Ds)

The candidate wins for B < B* when it is smaller but slower (Cz<Cs, Dz>Ds).
If the candidate is both smaller and not slower it dominates at every B.

Evaluated at B in {50, 125, 250} MiB/s per worker core, per approved gate:
the candidate must not be materially worse at 250 and must win clearly at
50 and/or 125.
"""
from __future__ import annotations

MIB = 1048576
BANDWIDTH_POINTS_MIB_S = (50, 125, 250)
HIGH_BANDWIDTH_TOLERANCE_PCT = 5.0   # "not materially worse" at the high point
LOW_BANDWIDTH_WIN_PCT = 10.0         # "clear win" at a low point


def scan_time_s(compressed_bytes: int, decode_s: float, bandwidth_mib_s: float) -> float:
    return compressed_bytes / (bandwidth_mib_s * MIB) + decode_s


def crossover_bandwidth_mib_s(cs: int, ds: float, cz: int, dz: float) -> float | None:
    """B* in MiB/s. None => candidate dominates or is dominated at all B."""
    dbytes = cs - cz          # bytes saved by candidate
    dtime = dz - ds           # extra decode seconds paid by candidate
    if dtime <= 0:
        return None           # candidate not slower -> dominates if also smaller
    if dbytes <= 0:
        return None           # candidate not smaller and slower -> dominated
    return (dbytes / dtime) / MIB


def evaluate(baseline: dict, candidate: dict) -> dict:
    """baseline/candidate: {'projected_compressed_bytes', 'decode_projected_median_s'}"""
    cs = baseline["projected_compressed_bytes"]
    ds = baseline["decode_projected_median_s"]
    cz = candidate["projected_compressed_bytes"]
    dz = candidate["decode_projected_median_s"]

    points = {}
    for b in BANDWIDTH_POINTS_MIB_S:
        base_t = scan_time_s(cs, ds, b)
        cand_t = scan_time_s(cz, dz, b)
        points[b] = {
            "baseline_scan_s": base_t,
            "candidate_scan_s": cand_t,
            "delta_pct": 100.0 * (cand_t - base_t) / base_t if base_t else None,
            "candidate_faster": cand_t < base_t,
        }

    bstar = crossover_bandwidth_mib_s(cs, ds, cz, dz)
    high = points[max(BANDWIDTH_POINTS_MIB_S)]
    lows = [points[b] for b in BANDWIDTH_POINTS_MIB_S if b != max(BANDWIDTH_POINTS_MIB_S)]

    not_worse_at_high = (high["delta_pct"] or 0) <= HIGH_BANDWIDTH_TOLERANCE_PCT
    clear_win_at_low = any((p["delta_pct"] or 0) <= -LOW_BANDWIDTH_WIN_PCT for p in lows)

    return {
        "crossover_bandwidth_mib_s": bstar,
        "crossover_interpretation": (
            "candidate dominates at all bandwidths (smaller and not slower)"
            if bstar is None and cz < cs and dz <= ds else
            "candidate dominated at all bandwidths (not smaller, and slower)"
            if bstar is None else
            f"candidate wins for effective bandwidth below {bstar:.0f} MiB/s per worker core"
        ),
        "points_mib_s": points,
        "gate_not_worse_at_high_bandwidth": not_worse_at_high,
        "gate_clear_win_at_low_bandwidth": clear_win_at_low,
        "gate_bandwidth_overall": bool(not_worse_at_high and clear_win_at_low),
    }
