#!/usr/bin/env python3
"""Parquet reader / codec compatibility probe.

Writes probe files covering every codec x writer-version arm under test, then
verifies a reader against the four PASS criteria in docs/compatibility-matrix.md.

Run with --help for the full operator walkthrough; it is self-contained.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from typing import Sequence, TypedDict

# --------------------------------------------------------------------------- #
# Probe data definition -- FROZEN.
#
# docs/compatibility-matrix.md pins the expected values below. They were
# verified against a real probe_zstd_v10.parquet on 2026-08-11, not derived by
# hand. Editing make_table() or any EXPECTED_* constant silently invalidates
# that document's pass criteria, so don't: write() asserts the generated table
# still matches these values and refuses to emit probes if it does not.
# --------------------------------------------------------------------------- #
ARMS = [("none", None), ("snappy", None), ("zstd", 3), ("gzip", 6)]
VERSIONS = ["1.0", "2.6"]

N_ROWS = 20000

# (name, exact arrow type as str(pa.DataType)), in column order. Exact strings
# on purpose: the criteria call a coerced or widened column a FAIL, so
# timestamp[us], "timestamp[ms, tz=UTC]", large_string and int32 must all fail
# rather than being accepted as compatible casts.
EXPECTED_SCHEMA = [
    ("i", "int64"),
    ("ts", "timestamp[ms]"),
    ("low_card", "string"),
    ("high_card", "string"),
    ("f", "double"),
]

# Criterion 3: SELECT count(*), sum(f) FROM t WHERE i >= 10000
FILTER_I_MIN = 10000
EXPECTED_FILTER_ROWS = 10000
# f = i * 1.5, so every value and every partial sum is a multiple of 0.5 below
# 2**53 and therefore exact in float64 regardless of summation order. This is
# an exact comparison, not an approximate one.
EXPECTED_FILTER_SUM_F = 224992500.0

# Criterion 4: SELECT high_card FROM t WHERE i = 19999
STRING_PAGE_I = N_ROWS - 1
EXPECTED_STRING_PAGE = f"https://example.invalid/p/{STRING_PAGE_I}?q={STRING_PAGE_I * 7}"

# Reported independently, in this order, for every file.
CHECKS = ("row_count", "schema", "filter_agg", "string_page")


ProbeRecord = TypedDict(
    "ProbeRecord",
    {
        "file": str,
        "codec": str,
        "compression_level": int | None,
        "page_version": str,
        "key": str,
    },
)

ResultRecord = TypedDict(
    "ResultRecord",
    {
        "file": str,
        "codec": str,
        "compression_level": int | None,
        "page_version": str,
        "key": str,
        "checks": dict[str, bool],
        "errors": list[str],
        "pass": bool,
    },
)

HELP_EPILOG = """\
WHAT THIS IS
  A compatibility probe for Parquet page codecs. It writes small Parquet files
  covering every codec x writer-version arm under test, then checks whether a
  reader returns exactly the right data from each one.

  Why it exists: a page-codec change cannot be recommended until every reader in
  the estate is proven to read the new codec correctly. "It opened without an
  error" is not proof. A reader that opens the file but silently coerces a
  column, or parses the footer happily and then fails on page decompression, is
  a FAIL -- so each file is checked four independent ways and passes only if all
  four succeed.

MODE 1 -- GENERATE THE PROBE FILES
    probe_reader_support.py --write DIR

  Creates DIR if needed and writes 9 files into it:

    probe_none_v10.parquet     probe_none_v26.parquet
    probe_snappy_v10.parquet   probe_snappy_v26.parquet
    probe_zstd_v10.parquet     probe_zstd_v26.parquet
    probe_gzip_v10.parquet     probe_gzip_v26.parquet
    expected.json

  Every .parquet holds the same 20000 rows x 5 columns
  (i int64, ts timestamp[ms], low_card string, high_card string, f double);
  only the page codec and the Parquet writer version differ. v10 = writer
  version 1.0 (V1 data pages), v26 = writer version 2.6 (V2 data pages).
  Total size is a few MiB, so the directory is cheap to copy anywhere.

  expected.json lists the files that were written, the arm each one represents,
  and the expected values below -- so an operator on an engine this script
  cannot drive can read the targets straight out of the JSON.

MODE 2 -- VERIFY A READER
    probe_reader_support.py --read DIR [--json OUT.json]
                                      [--engine NAME] [--engine-version VER]

  Runs four checks per file, reports each one separately, and marks the file
  PASS only if all four pass:

    row_count    Footer reports exactly 20000 rows.
    schema       Exactly 5 columns, exact types, exact order. Widening or
                 coercion is a FAIL: timestamp[us], "timestamp[ms, tz=UTC]",
                 large_string and int32 all fail here.
    filter_agg   Filter i >= 10000 returns 10000 rows with
                 sum(f) = 224992500.0 (compared exactly).
    string_page  high_card where i = 19999 is
                 https://example.invalid/p/19999?q=139993

  row_count and schema read the footer only; filter_agg and string_page
  decompress real data pages. That split is deliberate -- it is what separates
  "this reader cannot parse the file at all" from "this reader parses the
  metadata and then cannot decompress the pages", which are different
  compatibility problems with different fixes.

  --read drives pyarrow. For an engine pyarrow cannot drive -- Spark, Trino,
  DuckDB, Hive, a vendor reader -- copy DIR to that engine and run the
  equivalent SQL by hand, once per .parquet file:

    1. SELECT count(*) FROM t;
         -> expect 20000
    2. DESCRIBE t;            (or whatever the engine calls it)
         -> expect exactly: i int64, ts timestamp[ms], low_card string,
                            high_card string, f double
            A widened or coerced type is a FAIL, not a pass.
    3. SELECT count(*), sum(f) FROM t WHERE i >= 10000;
         -> expect 10000 and 224992500.0
    4. SELECT high_card FROM t WHERE i = 19999;
         -> expect https://example.invalid/p/19999?q=139993

  Capture the exact error text on every failure. "Doesn't work" is not
  actionable: the error text is what distinguishes an unsupported codec from an
  unsupported page version from a dictionary-page problem.

WHAT TO DO WITH THE OUTPUT
  1. --read prints a per-file table of the four checks plus a ready-made
     Markdown row. Paste that row into the Results table in
     docs/compatibility-matrix.md, and paste the exact error text of any
     failure into the Notes column.
  2. With --json, the same verdicts are written as structured JSON. engine and
     engine_version are null unless you pass --engine / --engine-version; fill
     them in so the file records which reader and which version produced it.
     A verdict with no engine version attached cannot be audited later.
  3. Repeat on every reader in the estate, not just the convenient ones. The
     unenumerated legacy readers are the reason this gate exists; a matrix of
     pyarrow reading pyarrow's own output proves nothing.

EXIT CODES
  0  every probe file passed all four checks
  1  at least one check failed (details on stdout, and in --json if requested)
  2  usage error, or DIR does not exist / has no expected.json
"""


def make_table():
    import pyarrow as pa
    n = N_ROWS
    return pa.table({
        "i": pa.array(range(n)),
        "ts": pa.array([1700000000 + i for i in range(n)], pa.timestamp("ms")),
        "low_card": pa.array([f"dim-{i % 12}" for i in range(n)]),
        "high_card": pa.array([f"https://example.invalid/p/{i}?q={i*7}" for i in range(n)]),
        "f": pa.array([i * 1.5 for i in range(n)], pa.float64()),
    })


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _disp(path: str) -> str:
    """Render a path for a copy-pasteable command line.

    Forward slashes even on Windows. Every shell the operator might be standing
    in -- bash/Git Bash, PowerShell, cmd -- accepts them in an argument, whereas
    a backslash is an escape character in bash: "scripts\\probe_reader_support.py"
    silently collapses to "scriptsprobe_reader_support.py" and the command that
    this script advertises as the exact next step does not run.
    """
    return path.replace("\\", "/")


def _q(s: str) -> str:
    return f'"{s}"' if " " in s else s


def _interpreter() -> str:
    """A command name for the running interpreter, preferring a bare name.

    "python3" is both shorter and safer than an absolute C:\\...\\python3.exe;
    it is only used when it resolves on PATH to this very interpreter, so the
    printed command cannot silently select a different one.
    """
    exe = sys.executable or "python3"
    stem = os.path.splitext(os.path.basename(exe))[0]
    for name in (stem, "python3", "python"):
        if not name:
            continue
        found = shutil.which(name)
        if not found:
            continue
        try:
            if os.path.samefile(found, exe):
                return name
        except OSError:
            continue
    return _q(_disp(exe))


def _self_cmd() -> str:
    """This script's own invocation, exactly as the operator can re-run it."""
    script = os.path.abspath(__file__)
    try:
        rel = os.path.relpath(script, os.getcwd())
        if not rel.startswith(".."):
            script = rel
    except ValueError:                      # different drive on Windows
        pass
    return f"{_interpreter()} {_q(_disp(script))}"


def _arm_label(codec: str, level) -> str:
    """Column header used by docs/compatibility-matrix.md: none, zstd-3, gzip-6."""
    return codec if level is None else f"{codec}-{level}"


def _probe_key(codec: str, page_version: str) -> str:
    return f"{codec}|{page_version}"


def _probe_name(codec: str, page_version: str) -> str:
    return f"probe_{codec}_v{page_version.replace('.', '')}.parquet"


def _verify_generated(t) -> list[str]:
    """Guard the frozen probe data against accidental edits to make_table()."""
    problems = []
    if t.num_rows != N_ROWS:
        problems.append(f"num_rows={t.num_rows}, pinned {N_ROWS}")
    got = [(t.schema.field(i).name, str(t.schema.field(i).type))
           for i in range(len(t.schema))]
    if got != EXPECTED_SCHEMA:
        problems.append(f"schema={got}, pinned {EXPECTED_SCHEMA}")
    # Plain Python rather than pyarrow.compute: the table is generated in-process
    # and is 20k rows, so clarity beats vectorisation, and the guard stays usable
    # even if a column came back as a type compute would refuse.
    if "i" in t.schema.names and "f" in t.schema.names:
        kept = [f for i, f in zip(t.column("i").to_pylist(), t.column("f").to_pylist())
                if i >= FILTER_I_MIN]
        if len(kept) != EXPECTED_FILTER_ROWS:
            problems.append(f"filter rows={len(kept)}, pinned {EXPECTED_FILTER_ROWS}")
        total = sum(kept)
        if total != EXPECTED_FILTER_SUM_F:
            problems.append(f"sum(f)={total!r}, pinned {EXPECTED_FILTER_SUM_F!r}")
    if "high_card" in t.schema.names:
        high = t.column("high_card").to_pylist()
        # Report a short table rather than raising IndexError: a guard that
        # crashes instead of naming the drift is not a guard.
        if len(high) <= STRING_PAGE_I:
            problems.append(f"only {len(high)} rows, cannot check "
                            f"high_card[{STRING_PAGE_I}]")
        elif high[STRING_PAGE_I] != EXPECTED_STRING_PAGE:
            problems.append(f"high_card[{STRING_PAGE_I}]={high[STRING_PAGE_I]!r}, "
                            f"pinned {EXPECTED_STRING_PAGE!r}")
    return problems


# --------------------------------------------------------------------------- #
# --write
# --------------------------------------------------------------------------- #

def write(outdir: str) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    os.makedirs(outdir, exist_ok=True)
    t = make_table()

    problems = _verify_generated(t)
    if problems:
        print("refusing to write: generated probe data no longer matches the values "
              "pinned in docs/compatibility-matrix.md", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    made, probes = [], []
    for codec, lvl in ARMS:
        for ver in VERSIONS:
            name = _probe_name(codec, ver)
            path = os.path.join(outdir, name)
            try:
                pq.write_table(t, path, compression=codec, compression_level=lvl,
                               version=ver, data_page_size=1 << 20, use_dictionary=True)
                made.append(name)
                probes.append({"file": name, "codec": codec, "compression_level": lvl,
                               "page_version": ver, "arm": _arm_label(codec, lvl),
                               "key": _probe_key(codec, ver)})
            except Exception as e:
                print(f"  write FAILED {name}: {e}", file=sys.stderr)

    with open(os.path.join(outdir, "expected.json"), "w") as fh:
        json.dump({
            "files": made,
            "num_rows": t.num_rows,
            "columns": t.schema.names,
            "probes": probes,
            # Targets for engines this script cannot drive; see --help for the SQL.
            "expected": {
                "row_count": N_ROWS,
                "schema": [list(p) for p in EXPECTED_SCHEMA],
                "filter_agg": {"predicate": f"i >= {FILTER_I_MIN}",
                               "count": EXPECTED_FILTER_ROWS,
                               "sum_f": EXPECTED_FILTER_SUM_F},
                "string_page": {"predicate": f"i = {STRING_PAGE_I}",
                                "column": "high_card",
                                "value": EXPECTED_STRING_PAGE},
            },
            "written_by": f"pyarrow {pa.__version__}",
        }, fh, indent=2)

    print(f"wrote {len(made)}/{len(ARMS) * len(VERSIONS)} probe files + expected.json to {outdir}")
    print(f"  {N_ROWS} rows x {len(EXPECTED_SCHEMA)} columns per file: "
          + ", ".join(f"{n} {ty}" for n, ty in EXPECTED_SCHEMA))
    print()
    print("NEXT COMMAND")
    result_json = _disp(os.path.join(outdir, "result-pyarrow.json"))
    print(f"  {_self_cmd()} --read {_q(_disp(outdir))} --json {_q(result_json)}"
          f" --engine pyarrow --engine-version {pa.__version__}")
    print()
    print(f"Then copy {_disp(outdir)} to every other reader in the estate (Spark/parquet-mr, the")
    print("warehouse engine you serve dashboards from, DuckDB, Hive, any legacy reader) and")
    print("run the four checks there -- see --help for the SQL to run by hand. Record every")
    print("outcome, including the exact error text of any failure, in")
    print("docs/compatibility-matrix.md.")
    return 0


# --------------------------------------------------------------------------- #
# --read
# --------------------------------------------------------------------------- #

def _schema_diff(got: list[tuple[str, str]],
                 expected: list[tuple[str, str]]) -> list[str]:
    """Exact-match schema comparison. Widening and coercion are failures."""
    out = []
    if len(got) != len(expected):
        names = ", ".join(n for n, _ in got) or "(none)"
        out.append(f"expected {len(expected)} columns, got {len(got)} [{names}]")
    got_map, exp_map = dict(got), dict(expected)
    for name, ty in expected:
        if name not in got_map:
            out.append(f"missing column {name} ({ty})")
        elif got_map[name] != ty:
            out.append(f"{name}: expected {ty}, got {got_map[name]} "
                       f"-- coercion/widening is a FAIL")
    for name, ty in got:
        if name not in exp_map:
            out.append(f"unexpected column {name} ({ty})")
    if not out:
        got_order = [n for n, _ in got]
        exp_order = [n for n, _ in expected]
        if got_order != exp_order:
            out.append(f"column order differs: expected {exp_order}, got {got_order}")
    return out


def _run_checks(pq, path: str) -> tuple[dict[str, bool], list[str]]:
    """Four independent checks on one file.

    Every check runs regardless of what the others did, and each records its own
    error text: a file that parses its footer but cannot decompress its pages
    must report 2 ok / 2 FAIL rather than one opaque failure.
    """
    checks = {k: False for k in CHECKS}
    errors: list[str] = []

    if not os.path.isfile(path):
        errors.append(f"file not found: {path}")
        return checks, errors

    # (1) row_count -- footer only, no page decompression.
    try:
        n = pq.read_metadata(path).num_rows
        if n == N_ROWS:
            checks["row_count"] = True
        else:
            errors.append(f"row_count: got {n}, expected {N_ROWS}")
    except Exception as e:
        errors.append(f"row_count: {type(e).__name__}: {e}")

    # (2) schema -- footer only. Exact types, exact order.
    try:
        sch = pq.read_schema(path)
        got = [(sch.field(i).name, str(sch.field(i).type)) for i in range(len(sch))]
        diff = _schema_diff(got, EXPECTED_SCHEMA)
        if diff:
            errors.append("schema: " + "; ".join(diff))
        else:
            checks["schema"] = True
    except Exception as e:
        errors.append(f"schema: {type(e).__name__}: {e}")

    # (3) filter_agg -- pushes the predicate down, so it exercises the reader's
    # filter path and the numeric pages, not just a full scan.
    try:
        t = pq.read_table(path, columns=["i", "f"],
                          filters=[("i", ">=", FILTER_I_MIN)])
        bad = []
        if t.num_rows != EXPECTED_FILTER_ROWS:
            bad.append(f"count(*)={t.num_rows}, expected {EXPECTED_FILTER_ROWS}")
        total = sum(t.column("f").to_pylist())
        if total != EXPECTED_FILTER_SUM_F:
            bad.append(f"sum(f)={total!r}, expected {EXPECTED_FILTER_SUM_F!r}")
        if bad:
            errors.append("filter_agg: " + "; ".join(bad))
        else:
            checks["filter_agg"] = True
    except Exception as e:
        errors.append(f"filter_agg: {type(e).__name__}: {e}")

    # (4) string_page -- reads a compressed high-cardinality string page, which
    # is where a reader that parsed the footer fine still falls over.
    try:
        t = pq.read_table(path, columns=["i", "high_card"],
                          filters=[("i", "==", STRING_PAGE_I)])
        vals = t.column("high_card").to_pylist()
        if len(vals) != 1:
            errors.append(f"string_page: expected 1 row at i={STRING_PAGE_I}, "
                          f"got {len(vals)}")
        elif vals[0] != EXPECTED_STRING_PAGE:
            errors.append(f"string_page: got {vals[0]!r}, "
                          f"expected {EXPECTED_STRING_PAGE!r}")
        else:
            checks["string_page"] = True
    except Exception as e:
        errors.append(f"string_page: {type(e).__name__}: {e}")

    return checks, errors


def _probe_list(exp: dict) -> list[ProbeRecord]:
    """Arms to check, from expected.json.

    Prefers the "probes" block; falls back to parsing the filenames so a probe
    directory generated by an older revision of this script still reads.
    """
    probes = exp.get("probes")
    if isinstance(probes, list) and probes:
        out: list[ProbeRecord] = []
        for p in probes:
            codec = p.get("codec", "?")
            ver = p.get("page_version", "?")
            out.append({"file": p.get("file", _probe_name(codec, ver)),
                        "codec": codec, "compression_level": p.get("compression_level"),
                        "page_version": ver,
                        "key": p.get("key") or _probe_key(codec, ver)})
        return out

    levels = dict(ARMS)
    out: list[ProbeRecord] = []
    for name in exp.get("files", []):
        m = re.match(r"^probe_(?P<codec>.+)_v(?P<v>\d+)\.parquet$", name)
        if m:
            codec = m.group("codec")
            digits = m.group("v")
            ver = f"{digits[:-1]}.{digits[-1]}" if len(digits) > 1 else digits
        else:
            codec, ver = name, "?"
        out.append({"file": name, "codec": codec,
                    "compression_level": levels.get(codec), "page_version": ver,
                    "key": _probe_key(codec, ver)})
    return out


def _markdown_row(results: Sequence[ResultRecord], engine, engine_version) -> str:
    """A row shaped for the Results table in docs/compatibility-matrix.md."""
    def verdict(pred) -> str:
        sel = [r for r in results if pred(r)]
        if not sel:
            return "n/a"
        return "PASS" if all(r["pass"] for r in sel) else "FAIL"

    cells = [engine or "*FILL IN*", engine_version or "*FILL IN*"]
    for codec, lvl in ARMS:
        cells.append(verdict(lambda r, c=codec: r["codec"] == c))
    for ver in VERSIONS:
        cells.append(verdict(lambda r, v=ver: r["page_version"] == v))
    fails = [r for r in results if not r["pass"]]
    cells.append("*paste the exact error text of any FAIL here*" if fails
                 else "*record date and how the files were transferred*")
    return "| " + " | ".join(cells) + " |"


def read(indir: str, json_path=None, engine=None, engine_version=None) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    exp_path = os.path.join(indir, "expected.json")
    if not os.path.isfile(exp_path):
        print(f"error: no expected.json in {_disp(indir)}", file=sys.stderr)
        print(f"       {_disp(indir)} is not a probe directory. Generate one first:",
              file=sys.stderr)
        print(f"         {_self_cmd()} --write {_q(_disp(indir))}", file=sys.stderr)
        return 2
    try:
        with open(exp_path) as fh:
            exp = json.load(fh)
    except Exception as e:
        print(f"error: cannot parse {exp_path}: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    probes = _probe_list(exp)
    if not probes:
        print(f"error: {exp_path} lists no probe files", file=sys.stderr)
        return 2

    print(f"probe dir : {_disp(os.path.abspath(indir))}")
    print(f"reader    : pyarrow {pa.__version__} (parquet-cpp)")
    if engine:
        print(f"engine    : {engine} {engine_version or '(version not supplied)'}")
    print(f"expecting : {N_ROWS} rows; "
          + ", ".join(f"{n} {ty}" for n, ty in EXPECTED_SCHEMA))
    print(f"            i >= {FILTER_I_MIN} -> {EXPECTED_FILTER_ROWS} rows, "
          f"sum(f) = {EXPECTED_FILTER_SUM_F}")
    print(f"            high_card @ i = {STRING_PAGE_I} -> {EXPECTED_STRING_PAGE}")

    stale = exp.get("num_rows")
    if stale is not None and stale != N_ROWS:
        print(f"WARNING   : expected.json says num_rows={stale} but this script is "
              f"pinned to {N_ROWS}; the probe directory was written by a different "
              f"revision. Regenerate it with --write.")
    print()

    print(f"  {'codec':<8}{'pages':<7}{'row_count':<11}{'schema':<8}"
          f"{'filter_agg':<12}{'string_page':<13}RESULT")
    results: list[ResultRecord] = []
    failures: list[tuple[str, list[str]]] = []
    for p in probes:
        checks, errors = _run_checks(pq, os.path.join(indir, p["file"]))
        passed = all(checks[k] for k in CHECKS)
        cells = ["ok" if checks[k] else "FAIL" for k in CHECKS]
        print(f"  {p['codec']:<8}{p['page_version']:<7}{cells[0]:<11}{cells[1]:<8}"
              f"{cells[2]:<12}{cells[3]:<13}{'PASS' if passed else 'FAIL'}")
        rec: ResultRecord = {**p, "checks": checks, "errors": errors, "pass": passed}
        results.append(rec)
        if errors:
            failures.append((p["file"], errors))

    n_pass = sum(1 for r in results if r["pass"])
    n_checks_pass = sum(1 for r in results for k in CHECKS if r["checks"][k])
    n_checks = len(results) * len(CHECKS)
    print()
    if failures:
        print("FAILURE DETAIL (paste this verbatim into docs/compatibility-matrix.md)")
        for name, errs in failures:
            for e in errs:
                print(f"  {name}: {e}")
        print()
    print(f"{n_pass}/{len(results)} files PASS "
          f"({n_checks_pass}/{n_checks} individual checks passed)")

    print()
    print("MARKDOWN ROW for the Results table in docs/compatibility-matrix.md")
    print("  | Reader | Version | none | snappy | zstd-3 | gzip-6 "
          "| v1.0 pages | v2.6 pages | Notes |")
    print("  " + _markdown_row(results, engine, engine_version))

    if json_path:
        payload = {
            "engine": engine,
            "engine_version": engine_version,
            "probes": {r["key"]: {"pass": r["pass"],
                                  "checks": {k: r["checks"][k] for k in CHECKS},
                                  "error": "; ".join(r["errors"]) if r["errors"] else None}
                       for r in results},
        }
        parent = os.path.dirname(os.path.abspath(json_path))
        os.makedirs(parent, exist_ok=True)
        with open(json_path, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        print()
        print(f"wrote {_disp(json_path)}")
        if engine is None or engine_version is None:
            print("  engine / engine_version are null -- fill them in (or re-run with")
            print("  --engine / --engine-version) so the verdict records which reader")
            print("  and which version produced it.")

    print()
    print("NEXT COMMAND")
    if not json_path:
        out = _disp(os.path.join(indir, f"result-{engine or 'pyarrow'}.json"))
        print(f"  {_self_cmd()} --read {_q(_disp(indir))} --json {_q(out)}"
              f" --engine {engine or 'pyarrow'}"
              f" --engine-version {engine_version or pa.__version__}")
        print()
        print("  (same checks, plus a structured JSON verdict to attach to the matrix)")
    else:
        print(f"  # copy {_disp(os.path.abspath(indir))} to the next reader in the "
              f"estate, then run there:")
        print(f"  {_self_cmd()} --read <probe-dir-on-that-host>"
              f" --json <probe-dir-on-that-host>/result-<engine>.json"
              f" --engine <engine> --engine-version <version>")
        print()
        print("  For Spark/parquet-mr, Trino, Hive or any engine pyarrow cannot drive, run")
        print("  the four SQL checks by hand instead -- see --help. Then update the Results")
        print("  table in docs/compatibility-matrix.md. The gate lifts only when the readers")
        print("  that actually serve your estate are in that table, not just pyarrow.")

    return 0 if n_pass == len(results) else 1


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="probe_reader_support.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Parquet reader / codec compatibility probe: generate probe files, "
                    "then verify a reader against the four PASS criteria in "
                    "docs/compatibility-matrix.md.",
        epilog=HELP_EPILOG)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", metavar="DIR",
                      help="generate the 8 probe files + expected.json into DIR")
    mode.add_argument("--read", metavar="DIR",
                      help="verify every probe file in DIR against all four criteria")
    ap.add_argument("--json", metavar="OUT.json",
                    help="with --read: also write the verdicts as structured JSON")
    ap.add_argument("--engine", metavar="NAME",
                    help="with --read: reader under test (e.g. pyarrow, duckdb); "
                         "recorded in --json, null if omitted")
    ap.add_argument("--engine-version", metavar="VER",
                    help="with --read: version of that reader; recorded in --json, "
                         "null if omitted")
    a = ap.parse_args()

    if a.write:
        if a.json:
            ap.error("--json applies to --read, not --write")
        return write(a.write)
    if a.read:
        return read(a.read, a.json, a.engine, a.engine_version)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
