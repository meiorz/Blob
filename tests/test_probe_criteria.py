"""Regression tests for scripts/probe_reader_support.py's four PASS criteria.

Plain pytest asserts, so pytest collects and reports these directly (unlike the
record()-style suites, which tests/conftest.py excludes and tests/test_suites.py
wraps -- see the comment in conftest.py for why).

What these lock down: that each criterion FAILS on the thing it exists to catch,
and that a failure is attributed to the right criterion. A checker that returns
4/4 on good files proves nothing on its own -- the original defect was exactly a
--read that passed every file because it only counted rows.
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from scripts import probe_reader_support as P  # noqa: E402

N = P.N_ROWS


# --------------------------------------------------------------------------- #
# the values docs/compatibility-matrix.md pins
# --------------------------------------------------------------------------- #

def test_pinned_constants_match_the_document():
    """These were verified against a real probe file on 2026-08-11.

    docs/compatibility-matrix.md quotes them literally, and the four SQL
    statements in --help quote them again. If this test fails, either the probe
    data changed (forbidden) or the document is now lying.
    """
    assert P.N_ROWS == 20000
    assert P.EXPECTED_FILTER_ROWS == 10000
    assert P.EXPECTED_FILTER_SUM_F == 224992500.0
    assert P.STRING_PAGE_I == 19999
    assert P.EXPECTED_STRING_PAGE == "https://example.invalid/p/19999?q=139993"
    assert P.EXPECTED_SCHEMA == [
        ("i", "int64"), ("ts", "timestamp[ms]"), ("low_card", "string"),
        ("high_card", "string"), ("f", "double"),
    ]
    assert P.CHECKS == ("row_count", "schema", "filter_agg", "string_page")


def test_expected_sum_is_exactly_representable():
    """The sum is compared exactly, not with a tolerance; justify that.

    f = i * 1.5, so every value and every partial sum is a multiple of 0.5 well
    below 2**53 and therefore exact in float64 for any summation order.
    """
    vals = [i * 1.5 for i in range(P.FILTER_I_MIN, N)]
    assert sum(vals) == P.EXPECTED_FILTER_SUM_F
    assert sum(reversed(vals)) == P.EXPECTED_FILTER_SUM_F


def test_generated_table_matches_the_pinned_values():
    assert P._verify_generated(P.make_table()) == []


def test_write_refuses_when_probe_data_drifts(tmp_path, monkeypatch):
    """The frozen-data guard must refuse to emit probes, not warn and continue."""
    original = P.make_table          # capture before patching, or tampered() recurses

    def tampered():
        t = original()
        return t.set_column(t.schema.get_field_index("f"), "f",
                            pa.array([i * 2.0 for i in range(N)], pa.float64()))

    monkeypatch.setattr(P, "make_table", tampered)
    out = tmp_path / "drifted"
    assert P.write(str(out)) == 2
    assert not list(out.glob("*.parquet"))


# --------------------------------------------------------------------------- #
# adversarial files: each must fail exactly the criteria it should
# --------------------------------------------------------------------------- #

def _cols(**over):
    d = {
        "i": pa.array(range(N)),
        "ts": pa.array([1700000000 + i for i in range(N)], pa.timestamp("ms")),
        "low_card": pa.array([f"dim-{i % 12}" for i in range(N)]),
        "high_card": pa.array([f"https://example.invalid/p/{i}?q={i*7}" for i in range(N)]),
        "f": pa.array([i * 1.5 for i in range(N)], pa.float64()),
    }
    d.update(over)
    return d


def _corrupt_column_pages(src, dst, colname):
    """Overwrite bytes inside one column chunk, leaving the footer intact.

    This is the case criterion 4 exists for: metadata parses, pages do not.
    Pass dst == src to corrupt in place.
    """
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)
    md = pq.read_metadata(dst)
    ch = md.row_group(0).column(md.schema.names.index(colname))
    with open(dst, "r+b") as fh:
        fh.seek(ch.data_page_offset)
        fh.write(b"\xde\xad\xbe\xef" * 64)
    return dst


@pytest.fixture(scope="module")
def variants(tmp_path_factory):
    """Build every adversarial file once; the checks themselves are cheap."""
    d = tmp_path_factory.mktemp("probe_variants")

    def w(name, table, **kw):
        path = str(d / name)
        pq.write_table(table, path, version="2.6", data_page_size=1 << 20,
                       use_dictionary=True, **kw)
        return path

    v = {}
    v["clean"] = w("clean.parquet", pa.table(_cols()),
                   compression="zstd", compression_level=3)
    v["ts_us"] = w("ts_us.parquet", pa.table(_cols(
        ts=pa.array([1700000000 + i for i in range(N)], pa.timestamp("us")))))
    v["ts_tz"] = w("ts_tz.parquet", pa.table(_cols(
        ts=pa.array([1700000000 + i for i in range(N)], pa.timestamp("ms", tz="UTC")))))
    v["i_int32"] = w("i_int32.parquet", pa.table(_cols(
        i=pa.array(range(N), pa.int32()))))
    v["f_float32"] = w("f_float32.parquet", pa.table(_cols(
        f=pa.array([i * 1.5 for i in range(N)], pa.float32()))))

    c = _cols()
    v["reordered"] = w("reordered.parquet", pa.table(
        {k: c[k] for k in ["ts", "i", "low_card", "high_card", "f"]}))
    v["extra_col"] = w("extra.parquet", pa.table(_cols(extra=pa.array(range(N)))))
    short_cols = _cols()
    short_cols.pop("low_card")
    v["missing_col"] = w("missing.parquet", pa.table(short_cols))

    v["short"] = w("short.parquet", pa.table(_cols()).slice(0, N - 1))
    v["bad_sum"] = w("bad_sum.parquet", pa.table(_cols(
        f=pa.array([i * 1.5 + (0.5 if i == 15000 else 0.0) for i in range(N)],
                   pa.float64()))))
    v["bad_url"] = w("bad_url.parquet", pa.table(_cols(
        high_card=pa.array(
            [f"https://example.invalid/p/{i}?q={i*7}" if i != N - 1 else "TAMPERED"
             for i in range(N)]))))

    v["corrupt_string_pages"] = _corrupt_column_pages(
        v["clean"], str(d / "corrupt_str.parquet"), "high_card")
    v["corrupt_numeric_pages"] = _corrupt_column_pages(
        v["clean"], str(d / "corrupt_f.parquet"), "f")
    v["absent"] = str(d / "does_not_exist.parquet")
    return v


# (variant, row_count, schema, filter_agg, string_page)
CASES = [
    ("clean",                 True,  True,  True,  True),
    ("ts_us",                 True,  False, True,  True),
    ("ts_tz",                 True,  False, True,  True),
    ("i_int32",               True,  False, True,  True),
    ("f_float32",             True,  False, True,  True),
    ("reordered",             True,  False, True,  True),
    ("extra_col",             True,  False, True,  True),
    ("missing_col",           True,  False, True,  True),
    # Truncation also removes row i=19999 and one row from the filter range, so
    # three criteria legitimately fail; only the schema survives.
    ("short",                 False, True,  False, False),
    ("bad_sum",               True,  True,  False, True),
    ("bad_url",               True,  True,  True,  False),
    # Footer parses; only the corrupted column's pages fail. This is the
    # criterion-3-vs-4 separation the document relies on.
    ("corrupt_string_pages",  True,  True,  True,  False),
    ("corrupt_numeric_pages", True,  True,  False, True),
    ("absent",                False, False, False, False),
]


@pytest.mark.parametrize("variant,row_count,schema,filter_agg,string_page", CASES,
                         ids=[c[0] for c in CASES])
def test_criteria_fail_on_the_right_thing(variants, variant, row_count, schema,
                                          filter_agg, string_page):
    checks, errors = P._run_checks(pq, variants[variant])
    want = {"row_count": row_count, "schema": schema,
            "filter_agg": filter_agg, "string_page": string_page}
    assert checks == want, f"{variant}: errors were {errors}"
    # A failure must carry error text; "FAIL" with no reason is not actionable for
    # an operator filling in the matrix. An unopenable file reports one error for
    # all four criteria, so this is a floor of one, not one per criterion.
    if all(want.values()):
        assert errors == []
    else:
        assert errors, f"{variant} failed a criterion but recorded no error text"


def test_a_file_passes_only_if_all_four_pass(variants):
    for variant, *flags in CASES:
        checks, _ = P._run_checks(pq, variants[variant])
        assert all(checks.values()) == all(flags), variant


@pytest.mark.parametrize("bad_type", ["large_string", "string_view",
                                      "decimal128(38, 9)", "int32",
                                      "timestamp[us]", "timestamp[ms, tz=UTC]"])
def test_schema_diff_rejects_widening_and_coercion(bad_type):
    """Reader-side widenings a Parquet round-trip normalises away.

    They cannot be produced by writing a file, so check the comparison directly.
    """
    got = [(n, bad_type if n == "ts" else t) for n, t in P.EXPECTED_SCHEMA]
    diff = P._schema_diff(got, P.EXPECTED_SCHEMA)
    assert diff, f"{bad_type} was accepted as compatible"
    assert "ts" in diff[0]


def test_schema_diff_accepts_the_exact_schema():
    assert P._schema_diff(list(P.EXPECTED_SCHEMA), P.EXPECTED_SCHEMA) == []


# --------------------------------------------------------------------------- #
# end to end: --write then --read then --json
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def probe_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("probes")
    assert P.write(str(d)) == 0
    return d


def test_write_emits_every_arm(probe_dir):
    made = sorted(p.name for p in probe_dir.glob("*.parquet"))
    assert len(made) == len(P.ARMS) * len(P.VERSIONS) == 8
    assert (probe_dir / "expected.json").is_file()


def test_read_passes_all_arms_and_emits_valid_json(probe_dir):
    out = probe_dir / "result.json"
    rc = P.read(str(probe_dir), json_path=str(out),
                engine="pyarrow", engine_version=pa.__version__)
    assert rc == 0

    import json
    payload = json.loads(out.read_text())
    assert set(payload) == {"engine", "engine_version", "probes"}
    assert payload["engine"] == "pyarrow"
    assert len(payload["probes"]) == 8
    for key, probe in payload["probes"].items():
        codec, sep, ver = key.partition("|")
        assert sep == "|" and codec and ver, key
        assert set(probe) == {"pass", "checks", "error"}
        assert list(probe["checks"]) == list(P.CHECKS)
        assert probe["pass"] is True
        assert probe["pass"] == all(probe["checks"].values())
        assert probe["error"] is None


def test_read_returns_1_when_an_arm_fails(probe_dir, tmp_path):
    """Exit code must distinguish "probes failed" from "could not run"."""
    d = tmp_path / "mixed"
    shutil.copytree(probe_dir, d)
    _corrupt_column_pages(str(d / "probe_zstd_v26.parquet"),
                          str(d / "probe_zstd_v26.parquet"), "high_card")
    assert P.read(str(d)) == 1


def test_read_returns_2_on_a_directory_that_is_not_a_probe_dir(tmp_path):
    assert P.read(str(tmp_path)) == 2


def test_engine_fields_default_to_null(probe_dir, tmp_path):
    out = tmp_path / "anon.json"
    assert P.read(str(probe_dir), json_path=str(out)) == 0
    import json
    payload = json.loads(out.read_text())
    assert payload["engine"] is None
    assert payload["engine_version"] is None


def test_probe_list_falls_back_to_filenames():
    """A probe dir written by an older revision has no "probes" key."""
    legacy = {"files": [P._probe_name(c, v) for c, _ in P.ARMS for v in P.VERSIONS],
              "num_rows": N, "columns": [n for n, _ in P.EXPECTED_SCHEMA]}
    got = P._probe_list(legacy)
    assert [g["key"] for g in got] == [f"{c}|{v}" for c, _ in P.ARMS
                                       for v in P.VERSIONS]


def test_emitted_commands_are_shell_safe(probe_dir, capsys):
    """A backslash in a printed command is an escape in bash and breaks it."""
    P.write(str(probe_dir))
    out = capsys.readouterr().out
    command_lines = [ln for ln in out.splitlines()
                     if "--read" in ln or "--write" in ln]
    assert command_lines
    for line in command_lines:
        assert "\\" not in line, line
