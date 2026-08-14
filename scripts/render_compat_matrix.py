#!/usr/bin/env python3
"""Render the generated block of docs/compatibility-matrix.md from compat_matrix.json.

JSON is the source of truth for reader/codec compatibility data. This script
produces the Markdown view humans read; analysis code should read the JSON
directly and decide "compat-blocked" from it rather than parsing Markdown.

**This renderer owns a delimited region, not the whole file.** Everything between
the BEGIN/END markers is regenerated; everything outside them is hand-written and
preserved untouched. That is deliberate: docs/compatibility-matrix.md also carries
the four PASS criteria with expected values pinned against a real probe file on
2026-08-11, the probe procedure, the known support floors and the rollback plan.
A whole-file renderer would delete them, and the pinned values are exactly what
scripts/probe_reader_support.py implements -- they must live in one place.

Usage:
    python3 scripts/render_compat_matrix.py            # rewrite the block
    python3 scripts/render_compat_matrix.py --check    # exit 1 if stale (for CI)
    python3 scripts/render_compat_matrix.py --stdout   # print, do not write
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPAT_JSON = os.path.join(ROOT, "data", "metadata", "compat_matrix.json")
OUTPUT_MD = os.path.join(ROOT, "docs", "compatibility-matrix.md")

def begin_marker(region: str) -> str:
    return (f"<!-- BEGIN GENERATED: {region} -- "
            f"edit data/metadata/compat_matrix.json, not this block -->")


def end_marker(region: str) -> str:
    return f"<!-- END GENERATED: {region} -->"


# Each region is spliced independently, so hand-written prose can sit between
# them: the Results table needs its critique immediately after it, and the
# migration bullets belong under their own h2 further down.
REGIONS = ("results", "fallback")

SCHEMA_VERSION = 1

# Column order of the rendered table. Codec labels match the arm labels emitted
# by scripts/probe_reader_support.py; page keys carry the "v" prefix used by the
# JSON schema, and are normalised on the way in.
CODECS = ["none", "snappy", "zstd-3", "gzip-6"]
PAGE_VERSIONS = ["v1.0", "v2.6"]

# The four criteria, in the order probe_reader_support.py reports them.
CRITERIA = ("row_count", "schema", "filter_agg", "string_page")

# Accepted aliases for a per-check flag, so a `checks` entry can be either the
# probe script's native --json shape or the flatter *_ok shape.
CRITERION_ALIASES = {
    "row_count": ("row_count", "count_ok", "row_count_ok"),
    "schema": ("schema", "schema_ok"),
    "filter_agg": ("filter_agg", "aggregate_ok", "filter_agg_ok"),
    "string_page": ("string_page", "high_card_ok", "string_page_ok"),
}


class CompatError(Exception):
    """A problem in compat_matrix.json that must not be rendered past."""


# --------------------------------------------------------------------------- #
# load + validate
# --------------------------------------------------------------------------- #

def load_compat(path: str = COMPAT_JSON) -> dict:
    if not os.path.isfile(path):
        raise CompatError(f"{path} does not exist")
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as e:
            raise CompatError(f"{path} is not valid JSON: {e}") from e

    got = data.get("schema_version")
    if got != SCHEMA_VERSION:
        raise CompatError(
            f"schema_version={got!r}, this renderer understands {SCHEMA_VERSION}. "
            f"Update the renderer deliberately rather than rendering a shape it "
            f"was not written for.")
    if not isinstance(data.get("engines"), list):
        raise CompatError("'engines' must be a list")
    return data


def _tri(value) -> bool | None:
    """Normalise a support flag to True / False / None (= not tested).

    Anything absent or null is *unknown*, never a failure. Rendering unknown as
    FAIL would show untested engines as failing and make the gate look decided
    on data that does not exist.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("pass", "true", "yes", "ok"):
            return True
        if v in ("fail", "false", "no"):
            return False
        if v in ("", "tbd", "unknown", "untested", "n/a"):
            return None
    raise CompatError(f"cannot interpret support flag {value!r}; "
                      f"use true, false or null")


def _norm_page(key: str) -> str:
    """Accept '1.0' or 'v1.0'; the probe script's JSON keys omit the 'v'."""
    k = str(key).strip()
    return k if k.startswith("v") else f"v{k}"


def _criterion(entry: dict, name: str):
    """Pull one criterion flag out of a check entry, in either accepted shape."""
    nested = entry.get("checks")
    if isinstance(nested, dict):
        for alias in CRITERION_ALIASES[name]:
            if alias in nested:
                return _tri(nested[alias])
    for alias in CRITERION_ALIASES[name]:
        if alias in entry:
            return _tri(entry[alias])
    return None


def _check_verdict(entry: dict) -> bool | None:
    """A check entry's overall verdict.

    Derived from the four criteria when they are present, because a file passes
    only if all four pass. An explicit "pass" is cross-checked against that and
    a contradiction is an error, not a silent preference for one field.
    """
    flags = [_criterion(entry, c) for c in CRITERIA]
    derived = None
    if any(f is not None for f in flags):
        derived = all(f is True for f in flags)
    declared = _tri(entry.get("pass"))
    if derived is not None and declared is not None and derived != declared:
        raise CompatError(
            f"check entry {entry.get('codec')}/{entry.get('page_version')} "
            f"declares pass={declared} but its four criteria imply {derived}. "
            f"A file passes only if all four criteria pass.")
    return derived if derived is not None else declared


def _errors_of(entry: dict) -> list[str]:
    """Failure text, accepting either `errors: [...]` or `error: "..."`."""
    out = []
    errs = entry.get("errors")
    if isinstance(errs, list):
        out.extend(str(e) for e in errs if e)
    elif isinstance(errs, str) and errs:
        out.append(errs)
    single = entry.get("error")
    if isinstance(single, str) and single:
        out.append(single)
    return out


def summarise(engine: dict) -> dict:
    """Per-codec and per-page verdicts for one engine.

    Detailed `checks` entries win when present: they are the measured facts,
    while codec_support/page_support are a hand-maintained summary of them. When
    both exist and disagree, that is a data defect and it raises.
    """
    name = engine.get("display_name") or engine.get("name") or "?"

    declared_codec = {c: _tri(engine.get("codec_support", {}).get(c))
                      for c in CODECS}
    declared_page = {p: _tri(engine.get("page_support", {}).get(p))
                     for p in PAGE_VERSIONS}

    by_codec: dict[str, list[bool | None]] = {c: [] for c in CODECS}
    by_page: dict[str, list[bool | None]] = {p: [] for p in PAGE_VERSIONS}
    failures: list[str] = []

    for entry in engine.get("checks") or []:
        codec = str(entry.get("codec", "")).strip()
        page = _norm_page(entry.get("page_version", ""))
        verdict = _check_verdict(entry)
        if codec in by_codec:
            by_codec[codec].append(verdict)
        if page in by_page:
            by_page[page].append(verdict)
        if verdict is False:
            errs = _errors_of(entry) or ["no error text recorded"]
            failures.append(f"{codec} / {page}: " + "; ".join(errs))

    def collapse(votes: list[bool | None], declared: bool | None, kind: str,
                 label: str) -> str | None:
        """Collapse a set of (codec, page_version) verdicts into one cell.

        Symmetric on both axes, and four-state rather than two. Any 2-state
        projection is lossy in one direction or the other: ALL reports a codec
        as FAIL when it works on one page version, ANY reports a page version as
        PASS when some codec fails on it. Both produce a cell that contradicts
        the `checks` underneath it, which in a gating document is worse than
        saying "mixed, go look".

        PARTIAL is deliberately not a pass. Gate logic requires an explicit PASS.
        """
        known = [v for v in votes if v is not None]
        if not known:
            return _state(declared)
        if all(known):
            measured = "PASS"
        elif not any(known):
            measured = "FAIL"
        else:
            measured = "PARTIAL"

        declared_state = _state(declared)
        if declared_state is not None and declared_state != measured:
            if measured == "PARTIAL":
                raise CompatError(
                    f"{name}: {kind}[{label!r}]={declared} but the detailed "
                    f"checks are mixed for {label!r} -- some combinations pass "
                    f"and some fail. A boolean cannot express that. Set it to "
                    f"null and let `checks` carry the detail.")
            raise CompatError(
                f"{name}: {kind}[{label!r}] says {declared} but the detailed "
                f"checks measure {measured}. Fix whichever is wrong; the "
                f"renderer will not pick one for you.")
        return measured

    combos = {}
    for entry in engine.get("checks") or []:
        codec = str(entry.get("codec", "")).strip()
        page = _norm_page(entry.get("page_version", ""))
        if codec in by_codec and page in by_page:
            combos[f"{codec}|{page}"] = _check_verdict(entry)

    return {
        "display_name": name,
        "version": engine.get("version"),
        "role": engine.get("role"),
        "codecs": {c: collapse(by_codec[c], declared_codec[c], "codec_support", c)
                   for c in CODECS},
        "pages": {p: collapse(by_page[p], declared_page[p], "page_support", p)
                  for p in PAGE_VERSIONS},
        "combos": combos,
        "notes": (engine.get("notes") or "").replace("\n", " ").strip(),
        "unsupported_reasons": engine.get("unsupported_reasons") or [],
        "failures": failures,
    }


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #

def _state(flag: bool | None) -> str | None:
    """Map a declared boolean support flag onto a cell state."""
    if flag is True:
        return "PASS"
    if flag is False:
        return "FAIL"
    return None


def _cell(state: str | None) -> str:
    return state if state in ("PASS", "FAIL", "PARTIAL") else "*TBD*"


def _md_escape(text: str) -> str:
    """A stray pipe in a note would silently add a column."""
    return text.replace("|", "\\|")


def render_table(rows: list[dict]) -> list[str]:
    head = ["Reader", "Version"] + CODECS + [f"{p} pages" for p in PAGE_VERSIONS] + ["Notes"]
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join(["---"] * len(head)) + "|"]
    for r in rows:
        note_bits = list(r["unsupported_reasons"])
        if r["notes"]:
            note_bits.insert(0, r["notes"])
        cells = ([r["display_name"], r["version"] or "*TBD*"]
                 + [_cell(r["codecs"][c]) for c in CODECS]
                 + [_cell(r["pages"][p]) for p in PAGE_VERSIONS]
                 + [_md_escape(" ".join(note_bits))])
        out.append("| " + " | ".join(cells) + " |")
    return out


def _partial_detail(row: dict) -> list[str]:
    """For each PARTIAL cell, name the combinations that pass and that fail.

    This is the actionable half. "gzip-6: PARTIAL" alone would leave an operator
    unable to tell whether the compatibility fallback is available; "passes v1.0,
    fails v2.6" says it is, on V1 pages.
    """
    out = []
    for codec in CODECS:
        if row["codecs"].get(codec) != "PARTIAL":
            continue
        ok = [p for p in PAGE_VERSIONS if row["combos"].get(f"{codec}|{p}") is True]
        bad = [p for p in PAGE_VERSIONS if row["combos"].get(f"{codec}|{p}") is False]
        bits = []
        if ok:
            bits.append(f"passes {', '.join(ok)}")
        if bad:
            bits.append(f"fails {', '.join(bad)}")
        out.append(f"`{codec}` {' — '.join(bits)}")
    for page in PAGE_VERSIONS:
        if row["pages"].get(page) != "PARTIAL":
            continue
        ok = [c for c in CODECS if row["combos"].get(f"{c}|{page}") is True]
        bad = [c for c in CODECS if row["combos"].get(f"{c}|{page}") is False]
        bits = []
        if ok:
            bits.append(f"passes with {', '.join(ok)}")
        if bad:
            bits.append(f"fails with {', '.join(bad)}")
        out.append(f"`{page} pages` {' — '.join(bits)}")
    return out


def render_status(rows: list[dict]) -> list[str]:
    """State the gate's position explicitly, computed rather than asserted."""
    def states(r):
        return list(r["codecs"].values()) + list(r["pages"].values())

    untested = [r for r in rows if all(s is None for s in states(r))]
    failed = [r for r in rows if "FAIL" in states(r)]
    partial = [r for r in rows if "PARTIAL" in states(r) and "FAIL" not in states(r)]
    # A pass requires an explicit PASS. PARTIAL and *TBD* are both "not verified".
    zstd_ok = [r for r in rows if r["codecs"].get("zstd-3") == "PASS"]

    out = []
    blockers = []
    if untested:
        blockers.append(f"{len(untested)} of {len(rows)} readers untested "
                        f"({', '.join(r['display_name'] for r in untested)})")
    if failed:
        blockers.append(f"{len(failed)} with recorded failures "
                        f"({', '.join(r['display_name'] for r in failed)})")
    if partial:
        blockers.append(f"{len(partial)} partial — some combinations pass, some fail "
                        f"({', '.join(r['display_name'] for r in partial)})")

    if blockers:
        out.append(f"**Gate: BLOCKED — " + "; ".join(blockers) + ".** No codec change "
                   f"may be recommended while any required reader is unverified, "
                   f"regardless of how favourable the benchmark numbers are. "
                   f"**PARTIAL is not a pass**: adoption needs an explicit PASS for "
                   f"the exact (codec, page version) a partition would be written with.")
    else:
        out.append("**Gate: every listed reader reports PASS on every arm.** Confirm "
                   "the list itself is complete before treating this as cleared — an "
                   "unenumerated reader cannot fail a probe it never ran.")

    detail = []
    for r in rows:
        bits = _partial_detail(r)
        if bits:
            detail.append(f"- **{r['display_name']}**: " + "; ".join(bits))
    if detail:
        out.append("")
        out.append("Where PARTIAL comes from — read `checks` in the JSON for the error "
                   "text, and note which arms still work; a codec that fails only on "
                   "v2.6 pages remains available on v1.0:")
        out.append("")
        out.extend(detail)

    if zstd_ok:
        out.append("")
        out.append(f"ZSTD-3 fully verified on: "
                   f"{', '.join(r['display_name'] for r in zstd_ok)}.")
    return out


def render_fallback(fallback: dict) -> list[str]:
    codec = (fallback.get("preferred_fallback_codec") or "gzip-6").upper()
    out = [f"- **{codec} is the fallback.**"]
    for note in fallback.get("fallback_notes") or []:
        out.append(f"- {note}")
    return out


def _wrap(region: str, body: list[str], data: dict) -> str:
    stamp = (f"_Rendered from `data/metadata/compat_matrix.json` (last_updated "
             f"{data.get('last_updated') or 'unknown'}) by "
             f"`scripts/render_compat_matrix.py`._")
    return "\n".join([begin_marker(region), ""] + body + ["", stamp,
                                                          end_marker(region)])


def render_regions(data: dict) -> dict[str, str]:
    rows = [summarise(e) for e in data["engines"]]
    results = render_status(rows) + [""] + render_table(rows)
    return {
        "results": _wrap("results", results, data),
        "fallback": _wrap("fallback", render_fallback(data.get("fallback") or {}), data),
    }


# --------------------------------------------------------------------------- #
# splice
# --------------------------------------------------------------------------- #

def splice(doc: str, blocks: dict[str, str]) -> str:
    """Replace each delimited region, leaving all hand-written prose intact."""
    for region in REGIONS:
        begin, end = begin_marker(region), end_marker(region)
        i, j = doc.find(begin), doc.find(end)
        if i == -1 or j == -1:
            raise CompatError(
                f"docs/compatibility-matrix.md has no '{region}' generated region. "
                f"Add these two markers around the part this script should own, "
                f"then re-run:\n  {begin}\n  {end}")
        if j < i:
            raise CompatError(f"'{region}': END marker precedes BEGIN marker")
        if doc.find(begin, i + 1) != -1:
            raise CompatError(f"'{region}': BEGIN marker appears more than once")
        doc = doc[:i] + blocks[region] + doc[j + len(end):]
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Render the generated block of docs/compatibility-matrix.md "
                    "from data/metadata/compat_matrix.json.")
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit 1 if the doc is out of date (for CI)")
    ap.add_argument("--stdout", action="store_true",
                    help="print the generated block and exit without writing")
    a = ap.parse_args()

    try:
        data = load_compat()
        blocks = render_regions(data)
        if a.stdout:
            print("\n\n".join(blocks[r] for r in REGIONS))
            return 0
        with open(OUTPUT_MD, "r", encoding="utf-8") as fh:
            current = fh.read()
        updated = splice(current, blocks)
    except CompatError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    rel = os.path.relpath(OUTPUT_MD, ROOT).replace("\\", "/")
    if a.check:
        if updated == current:
            print(f"{rel} is up to date with compat_matrix.json")
            return 0
        print(f"{rel} is STALE relative to data/metadata/compat_matrix.json",
              file=sys.stderr)
        diff = difflib.unified_diff(current.splitlines(True), updated.splitlines(True),
                                    fromfile=f"{rel} (committed)",
                                    tofile=f"{rel} (rendered)")
        sys.stderr.writelines(diff)
        print("\nrun: python3 scripts/render_compat_matrix.py", file=sys.stderr)
        return 1

    if updated == current:
        print(f"{rel} already current; nothing written")
        return 0
    with open(OUTPUT_MD, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(updated)
    print(f"rendered the generated block of {rel} "
          f"from {os.path.relpath(COMPAT_JSON, ROOT)}".replace("\\", "/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
