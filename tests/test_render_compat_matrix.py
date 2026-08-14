"""Regression tests for scripts/render_compat_matrix.py.

Plain pytest asserts, so pytest collects these directly.

Two things are being protected. First, that a collapsed column never contradicts
the per-(codec, page_version) `checks` underneath it: in a document that gates
codec adoption a false FAIL can retire a working migration path just as a false
PASS can approve a broken one. Second, that contradictory or unreadable JSON is
refused rather than rendered.

Fixtures are built inline rather than mutating data/metadata/compat_matrix.json,
so these tests neither depend on nor touch the committed engine data. One test
does render the committed file, read-only, as a guard on the real data.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import render_compat_matrix as R  # noqa: E402

CODECS = R.CODECS
PAGES = R.PAGE_VERSIONS


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def engine(display_name="Test Reader", version="1.0", passes=None, notes="",
           codec_support=None, page_support=None, checks=None):
    """An engine whose `checks` are generated from passes(codec, page) -> bool.

    codec_support / page_support default to all-null so the measured checks speak
    for themselves; pass explicit dicts to exercise the contradiction guard.
    """
    if checks is None:
        checks = []
        if passes is not None:
            for c in CODECS:
                for p in PAGES:
                    ok = passes(c, p)
                    checks.append({
                        "codec": c, "page_version": p, "pass": ok,
                        "checks": {k: ok for k in R.CRITERIA},
                        "error": None if ok else "probe failed",
                    })
    return {
        "name": "test", "display_name": display_name, "version": version,
        "role": "legacy",
        "codec_support": codec_support if codec_support is not None
        else {c: None for c in CODECS},
        "page_support": page_support if page_support is not None
        else {p: None for p in PAGES},
        "checks": checks, "unsupported_reasons": [], "notes": notes,
    }


def doc(engines, fallback=None):
    return {"schema_version": R.SCHEMA_VERSION, "last_updated": "2026-08-12T00:00:00Z",
            "engines": engines,
            "fallback": fallback or {"preferred_fallback_codec": "gzip-6",
                                     "fallback_notes": ["note"]}}


def row_of(e):
    """The rendered data row for one engine (header occupies indices 0 and 1)."""
    return R.render_table([R.summarise(e)])[2]


# --------------------------------------------------------------------------- #
# the two diagnostic scenarios -- exact rendered rows
# --------------------------------------------------------------------------- #

def test_codec_unsupported_renders_pure_fail_on_that_codec_only():
    """ZSTD unsupported on both page versions.

    zstd-3 is the only pure FAIL; the page columns are PARTIAL because some
    codec fails at each page version. Nothing claims a codec works when it does
    not, and nothing claims a page version is wholly unreadable.
    """
    e = engine("Reader A", "9.9", passes=lambda c, p: c != "zstd-3",
               notes="zstd unsupported")
    assert row_of(e) == ("| Reader A | 9.9 | PASS | PASS | FAIL | PASS "
                         "| PARTIAL | PARTIAL | zstd unsupported |")


def test_page_version_unsupported_does_not_render_working_codecs_as_fail():
    """V2 data pages unsupported; every codec works on v1.0.

    Regression test for the codec-column smearing defect: collapsing with ALL
    rendered `snappy = FAIL` here, contradicting a check that says snappy passes
    on v1.0, and reporting the Gzip compatibility fallback as unavailable when it
    is available on V1 pages.
    """
    e = engine("parquet-mr (Spark)", "3.5.0", passes=lambda c, p: p == "v1.0",
               notes="v2 pages unsupported")
    assert row_of(e) == ("| parquet-mr (Spark) | 3.5.0 | PARTIAL | PARTIAL "
                         "| PARTIAL | PARTIAL | PASS | FAIL "
                         "| v2 pages unsupported |")

    summary = R.summarise(e)
    for codec in CODECS:
        assert summary["codecs"][codec] == "PARTIAL"
        assert summary["combos"][f"{codec}|v1.0"] is True
        assert summary["combos"][f"{codec}|v2.6"] is False


def test_partial_detail_names_the_arms_that_still_work():
    """The fallback question: is gzip-6 usable at all on this reader?"""
    e = engine(passes=lambda c, p: p == "v1.0")
    detail = R._partial_detail(R.summarise(e))
    gzip_line = next(d for d in detail if d.startswith("`gzip-6`"))
    assert "passes v1.0" in gzip_line
    assert "fails v2.6" in gzip_line


def test_all_pass_and_all_fail_collapse_to_pure_states():
    allp = R.summarise(engine(passes=lambda c, p: True))
    assert set(allp["codecs"].values()) == {"PASS"}
    assert set(allp["pages"].values()) == {"PASS"}
    allf = R.summarise(engine(passes=lambda c, p: False))
    assert set(allf["codecs"].values()) == {"FAIL"}
    assert set(allf["pages"].values()) == {"FAIL"}


def test_collapse_is_symmetric_across_the_two_axes():
    """Transposing the failure pattern must transpose the rendered states."""
    codec_fault = R.summarise(engine(passes=lambda c, p: c != "zstd-3"))
    page_fault = R.summarise(engine(passes=lambda c, p: p != "v2.6"))
    assert codec_fault["codecs"]["zstd-3"] == "FAIL"
    assert set(codec_fault["pages"].values()) == {"PARTIAL"}
    assert page_fault["pages"]["v2.6"] == "FAIL"
    assert set(page_fault["codecs"].values()) == {"PARTIAL"}


# --------------------------------------------------------------------------- #
# PARTIAL is not a pass
# --------------------------------------------------------------------------- #

def test_partial_blocks_the_gate():
    status = "\n".join(R.render_status(
        [R.summarise(engine(passes=lambda c, p: p == "v1.0"))]))
    assert "BLOCKED" in status
    assert "PARTIAL is not a pass" in status


def test_partial_zstd_is_not_counted_as_verified():
    status = "\n".join(R.render_status(
        [R.summarise(engine(passes=lambda c, p: p == "v1.0"))]))
    assert "fully verified" not in status


def test_only_explicit_pass_clears_the_gate():
    status = "\n".join(R.render_status(
        [R.summarise(engine(passes=lambda c, p: True))]))
    assert "BLOCKED" not in status
    assert "fully verified" in status


def test_untested_engine_blocks_the_gate_and_renders_tbd():
    e = engine("Untested", None)
    assert R.summarise(e)["codecs"]["zstd-3"] is None
    assert "*TBD*" in row_of(e)
    assert "BLOCKED" in "\n".join(R.render_status([R.summarise(e)]))


# --------------------------------------------------------------------------- #
# tri-state: unknown must never render as FAIL
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    (True, "PASS"), (False, "FAIL"), (None, None),
    ("PASS", "PASS"), ("pass", "PASS"), ("FAIL", "FAIL"),
    ("tbd", None), ("unknown", None), ("", None),
])
def test_support_flags_map_to_states(value, expected):
    e = engine(codec_support={c: value for c in CODECS})
    assert R.summarise(e)["codecs"]["zstd-3"] == expected


def test_absent_support_key_is_tbd_not_fail():
    e = engine(codec_support={})
    assert R.summarise(e)["codecs"]["zstd-3"] is None
    assert R._cell(None) == "*TBD*"


def test_uninterpretable_flag_is_refused():
    e = engine(codec_support={c: "probably?" for c in CODECS})
    with pytest.raises(R.CompatError, match="cannot interpret support flag"):
        R.summarise(e)


# --------------------------------------------------------------------------- #
# contradictions are refused, not rendered
# --------------------------------------------------------------------------- #

def test_summary_contradicting_measured_checks_is_refused():
    e = engine(passes=lambda c, p: True,
               codec_support={c: (False if c == "zstd-3" else None) for c in CODECS})
    with pytest.raises(R.CompatError, match=r"codec_support\['zstd-3'\]"):
        R.summarise(e)


def test_boolean_cannot_express_partial():
    e = engine(passes=lambda c, p: p == "v1.0",
               codec_support={c: True for c in CODECS})
    with pytest.raises(R.CompatError, match="A boolean cannot express that"):
        R.summarise(e)


def test_declared_pass_must_agree_with_the_four_criteria():
    e = engine(checks=[{"codec": "zstd-3", "page_version": "v1.0", "pass": True,
                        "checks": {"row_count": True, "schema": False,
                                   "filter_agg": True, "string_page": True}}])
    with pytest.raises(R.CompatError, match="declares pass=True but its four criteria"):
        R.summarise(e)


def test_page_support_contradiction_is_named_as_page_support():
    e = engine(passes=lambda c, p: True, page_support={p: False for p in PAGES})
    with pytest.raises(R.CompatError, match=r"page_support\['v1\.0'\]"):
        R.summarise(e)


# --------------------------------------------------------------------------- #
# accepted input shapes
# --------------------------------------------------------------------------- #

def test_probe_script_json_shape_is_accepted():
    """probe_reader_support.py --json emits keys without the 'v' prefix."""
    e = engine(checks=[
        {"codec": c, "page_version": v,       # "1.0", as the probe JSON keys are
         "pass": True,
         "checks": {k: True for k in R.CRITERIA}, "error": None}
        for c in CODECS for v in ("1.0", "2.6")])
    assert set(R.summarise(e)["codecs"].values()) == {"PASS"}


def test_flat_ok_check_shape_is_accepted():
    e = engine(checks=[
        {"codec": c, "page_version": p, "count_ok": True, "schema_ok": True,
         "aggregate_ok": True, "high_card_ok": True, "errors": []}
        for c in CODECS for p in PAGES])
    assert set(R.summarise(e)["codecs"].values()) == {"PASS"}


def test_failure_text_is_surfaced_from_either_error_field():
    for entry in ({"errors": ["boom"]}, {"error": "boom"}):
        e = engine(checks=[dict(
            {"codec": "zstd-3", "page_version": "v1.0", "pass": False,
             "checks": {k: False for k in R.CRITERIA}}, **entry)])
        assert R.summarise(e)["failures"] == ["zstd-3 / v1.0: boom"]


def test_pipe_in_a_note_is_escaped():
    e = engine(notes="a | b")
    assert r"a \| b" in row_of(e)


# --------------------------------------------------------------------------- #
# load-time validation
# --------------------------------------------------------------------------- #

def test_wrong_schema_version_is_refused(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"schema_version": 999, "engines": []}))
    with pytest.raises(R.CompatError, match="schema_version"):
        R.load_compat(str(p))


def test_malformed_json_is_refused(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    with pytest.raises(R.CompatError, match="not valid JSON"):
        R.load_compat(str(p))


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(R.CompatError, match="does not exist"):
        R.load_compat(str(tmp_path / "absent.json"))


def test_engines_must_be_a_list(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"schema_version": R.SCHEMA_VERSION, "engines": {}}))
    with pytest.raises(R.CompatError, match="must be a list"):
        R.load_compat(str(p))


# --------------------------------------------------------------------------- #
# splicing: hand-written prose must survive
# --------------------------------------------------------------------------- #

def _skeleton():
    parts = ["# Title", "", "Hand-written intro that must survive."]
    for region in R.REGIONS:
        parts += ["", R.begin_marker(region), "stale content", R.end_marker(region),
                  "", f"Hand-written prose after {region}."]
    return "\n".join(parts) + "\n"


def test_splice_replaces_only_the_regions():
    out = R.splice(_skeleton(), R.render_regions(doc([engine(passes=lambda c, p: True)])))
    assert "Hand-written intro that must survive." in out
    assert "stale content" not in out
    for region in R.REGIONS:
        assert f"Hand-written prose after {region}." in out


def test_splice_is_idempotent():
    blocks = R.render_regions(doc([engine(passes=lambda c, p: True)]))
    once = R.splice(_skeleton(), blocks)
    assert R.splice(once, blocks) == once


def test_missing_region_is_refused_with_the_markers_to_add():
    skeleton = _skeleton().replace(R.begin_marker("fallback"), "")
    with pytest.raises(R.CompatError) as exc:
        R.splice(skeleton, R.render_regions(doc([engine()])))
    assert R.begin_marker("fallback") in str(exc.value)


def test_duplicate_begin_marker_is_refused():
    skeleton = _skeleton() + "\n" + R.begin_marker("results") + "\n"
    with pytest.raises(R.CompatError, match="more than once"):
        R.splice(skeleton, R.render_regions(doc([engine()])))


# --------------------------------------------------------------------------- #
# the committed artifacts (read-only)
# --------------------------------------------------------------------------- #

def test_committed_json_loads_and_renders():
    data = R.load_compat()
    blocks = R.render_regions(data)
    assert set(blocks) == set(R.REGIONS)


def test_committed_doc_is_in_sync_with_committed_json():
    """The same invariant CI's `--check` step enforces.

    Keeping it here too means `pytest tests/` catches doc drift even when the
    renderer is run outside CI.
    """
    with open(R.OUTPUT_MD, encoding="utf-8") as fh:
        current = fh.read()
    assert R.splice(current, R.render_regions(R.load_compat())) == current, (
        "docs/compatibility-matrix.md is stale; "
        "run python3 scripts/render_compat_matrix.py")


def test_committed_json_records_no_unverified_pass():
    """Guard against a fabricated PASS reappearing in the source of truth."""
    data = R.load_compat()
    for e in data["engines"]:
        states = [R._tri(v) for v in e.get("codec_support", {}).values()]
        if any(s is not None for s in states):
            assert e.get("version"), (
                f"{e.get('name')} records a verdict but no reader version; "
                f"a verdict with no version attached cannot be audited")
