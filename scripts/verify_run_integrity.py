#!/usr/bin/env python3
"""Verify a recorded run is internally coherent BEFORE anything is concluded from it.

`analyze_results.py` answers "what do these cells say?". This answers the prior
question: "are these cells a valid population to say anything about?" They are
separate because the analyser is run to get numbers, and a check that only fires
inside it is a check nobody runs when they are merely *looking* at results/raw.

Every failure here is a category error, not a bad result:

  1. Manifest integrity  - every cell the manifest names exists and parses.
  2. Single host         - all cells came from ONE machine. environment_class is a
                           label that defaults to "sandbox" everywhere, so it does
                           not establish this; env.os/cpu_model/mem_total_bytes do.
  3. Single environment  - one environment_class, and it agrees with the cells.
  4. Memory measured     - no cell carries a zeroed memory block. Those pass
                           G4/G5/G6 by arithmetic accident.
  5. Single dataset id   - one dataset_sha256 per dataset_id across the run.
  6. Append-only         - no tracked cell under results/raw has been MODIFIED.
  7. Declared arms       - the cells present match the manifest's arms/scales.

Exit 0 = the run is a coherent population. Exit 1 = it is not; do not compare
these cells with each other.

    python3 scripts/verify_run_integrity.py
    python3 scripts/verify_run_integrity.py --manifest results/manifest_<id>.json
    python3 scripts/verify_run_integrity.py --all-manifests
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
RAW = RESULTS / "raw"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benchmarks"))
from benchmarks.env_capture import mixed_host_report  # noqa: E402
from benchmarks.memory_profiler import cell_memory_problems  # noqa: E402

problems: list[str] = []
checks_run = 0


def check(ok: bool, name: str, detail: str = "") -> bool:
    global checks_run
    checks_run += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        problems.append(name)
        if detail:
            for line in detail.strip().splitlines():
                print(f"         {line}")
    return ok


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def digest_conflict(values: set) -> tuple[str | None, str | None]:
    """Do these dataset_sha256 values identify the same file? -> (conflict, note)

    Cells from run 20260811T072000Z-g5fix carry a 32-char PREFIX of the digest --
    the hand-written catalog supplied it and run_cell.py preferred a supplied
    value over recomputing. docs/benchmark-methodology.md records the correction
    and confirms each prefix matches the full digest. A matching prefix is
    therefore the SAME dataset measured before the fix, not a different one, and
    must not be reported as a conflict. A non-matching value must.
    """
    vals = sorted({v for v in values if v}, key=len, reverse=True)
    if len(vals) <= 1:
        return None, None
    longest = vals[0]
    mismatched = [v for v in vals[1:] if not longest.startswith(v)]
    if mismatched:
        return (f"different datasets recorded under one id: "
                f"{sorted(map(str, vals))}"), None
    return None, (f"{len(vals)} digest lengths for the same file "
                  f"({', '.join(str(len(v)) for v in vals)} chars) -- pre-correction "
                  "prefix, documented in docs/benchmark-methodology.md; identity confirmed")


def display(path: Path) -> str:
    """Repo-relative when possible; the caller may pass any path, absolute or not."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def verify(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    run_id = manifest.get("run_id", "?")
    names = manifest.get("cells") or []
    print(f"\n=== {display(manifest_path)}  run_id={run_id}  "
          f"cells={len(names)} complete={manifest.get('complete')} ===\n")

    # -- 1. every named cell exists and parses -------------------------------
    cells: dict[str, dict] = {}
    missing, unparseable = [], []
    for n in names:
        p = RAW / n
        if not p.exists():
            missing.append(n)
            continue
        try:
            cells[n] = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            unparseable.append(f"{n}: {e}")
    check(not missing and not unparseable, "manifest names only cells that exist and parse",
          "missing:\n  " + "\n  ".join(missing) if missing else ""
          + ("\nunparseable:\n  " + "\n  ".join(unparseable) if unparseable else ""))
    if not cells:
        check(False, "run has at least one readable cell", "nothing to verify")
        return

    # -- 2. one host ---------------------------------------------------------
    # The check this file exists for. See docs/benchmark-methodology.md.
    report = mixed_host_report({n: c.get("env") for n, c in cells.items()})
    check(report is None, "all cells were measured on ONE host", report or "")

    # -- 3. one environment_class, consistent --------------------------------
    classes = sorted({(c.get("env") or {}).get("environment_class") for c in cells.values()})
    check(len(classes) == 1, "all cells share one environment_class",
          f"found {classes}; SKILL.md forbids comparing across machines")

    # -- 4. memory actually measured ----------------------------------------
    unmeasured = {n: cell_memory_problems(c) for n, c in cells.items()}
    unmeasured = {n: p for n, p in unmeasured.items() if p}
    check(not unmeasured, "every cell carries a real memory measurement",
          "\n".join(f"{n}:\n  " + "\n  ".join(p) for n, p in sorted(unmeasured.items()))
          + ("\nThese cells pass G4/G5/G6 by arithmetic accident on a zeroed block; "
             "the analyser now reports them UNEVALUABLE." if unmeasured else ""))

    # -- 5. dataset identity is stable --------------------------------------
    digests: dict[str, set] = {}
    for n, c in cells.items():
        digests.setdefault(c.get("dataset_id", "?"), set()).add(c.get("dataset_sha256"))
    conflicting, prefixed = {}, {}
    for d, s in digests.items():
        conflict, note = digest_conflict(s)
        if conflict:
            conflicting[d] = conflict
        elif note:
            prefixed[d] = note
    check(not conflicting, "one dataset_sha256 per dataset_id",
          "\n".join(f"{d}: {c}" for d, c in conflicting.items()))
    for d, note in sorted(prefixed.items()):
        print(f"         note: {d}: {note}")

    # -- 6. results/raw is append-only ---------------------------------------
    p = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", "results/raw"],
                       cwd=str(ROOT), capture_output=True, text=True)
    modified = [ln for ln in p.stdout.splitlines() if ln.strip()] if p.returncode == 0 else []
    check(not modified, "no tracked cell under results/raw has been modified",
          "MODIFIED recorded evidence:\n  " + "\n  ".join(modified)
          + "\nRecorded cells are append-only. Restore with:\n  git checkout HEAD -- "
          + " ".join(modified) if modified else "")

    # -- 7. cells match the declared design ----------------------------------
    arms = set(manifest.get("arms") or [])
    scales = set(manifest.get("scales") or [])
    dsets = set(manifest.get("datasets") or [])
    stray = []
    for n, c in cells.items():
        if arms and c.get("arm") not in arms:
            stray.append(f"{n}: arm {c.get('arm')!r} not in manifest arms {sorted(arms)}")
        if scales and c.get("scale_label") not in scales:
            stray.append(f"{n}: scale {c.get('scale_label')!r} not in {sorted(scales)}")
        if dsets and c.get("dataset_id") not in dsets:
            stray.append(f"{n}: dataset {c.get('dataset_id')!r} not in {sorted(dsets)}")
    check(not stray, "every cell matches the manifest's declared design",
          "\n".join(stray))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=str(RESULTS / "latest_manifest.json"))
    ap.add_argument("--all-manifests", action="store_true",
                    help="verify every results/manifest_*.json")
    args = ap.parse_args()

    if args.all_manifests:
        paths = sorted(RESULTS.glob("manifest_*.json"))
    else:
        paths = [Path(args.manifest)]
    if not paths:
        print("no manifests found", file=sys.stderr)
        return 1
    for p in paths:
        if not p.exists():
            print(f"manifest not found: {p}", file=sys.stderr)
            return 1
        verify(p)

    print(f"\n{'-' * 66}")
    if problems:
        print(f"RUN INTEGRITY FAILED -- {len(problems)} of {checks_run} check(s):")
        for n in problems:
            print(f"  - {n}")
        print("\nDo not compare these cells with each other until this is resolved.\n")
        return 1
    print(f"Run integrity OK -- {checks_run} check(s) passed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
