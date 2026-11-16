"""Input rejection, conflict detection and plan/source checks."""

from __future__ import annotations

import json
import os

import pytest

from helpers import run_build, write_package

W, F = 3, 2


def _base_events():
    return [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]


def _base_sources(tmp_path):
    return {"A": {"P": [(7, 9)]}, "Q": {"P": [(10, 12)]}}, {"A": "exclusion", "Q": "qualifying"}


def _expect_rejected(plan, code):
    from cohort_builder.config import load_plan
    from cohort_builder.errors import InputRejected
    from cohort_builder.engine import build
    import duckdb

    p, issues = load_plan(plan)
    if issues:
        assert any(i.code == code for i in issues), [i.as_dict() for i in issues]
        return
    con = duckdb.connect()
    with pytest.raises(InputRejected) as ei:
        build(con, p)
    codes = [i.code for i in ei.value.issues]
    assert code in codes, codes


def test_reject_duplicate_source_id(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(plan) as fh:
        doc = json.load(fh)
    doc["sources"].append(dict(doc["sources"][0]))
    with open(plan, "w") as fh:
        json.dump(doc, fh)
    _expect_rejected(plan, "source_duplicate_id")


def test_reject_source_kind(tmp_path):
    cov, src = _base_sources(tmp_path)
    src = dict(src)
    src["A"] = "candidate"
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    _expect_rejected(plan, "source_bad_kind")


def test_reject_empty_source_set_for_a_kind(tmp_path):
    cov, _ = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, {"A": "exclusion"}, W, F)
    _expect_rejected(plan, "plan_no_sources_for_kind")


@pytest.mark.parametrize("bad", [-1, "3", 3.5, True, None])
def test_reject_bad_windows(tmp_path, bad):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(plan) as fh:
        doc = json.load(fh)
    doc["lookback_days"] = bad
    with open(plan, "w") as fh:
        json.dump(doc, fh)
    _expect_rejected(plan, "plan_bad_window")


def test_reject_missing_coverage_file(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    os.remove(os.path.join(str(tmp_path), "coverage_A.csv"))
    _expect_rejected(plan, "coverage_file_missing")


def test_reject_missing_coverage_column(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(os.path.join(str(tmp_path), "coverage_A.csv"), "w") as fh:
        fh.write("person,start_day\nP,7\n")
    _expect_rejected(plan, "missing_columns")


def test_reject_non_integer_day(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(os.path.join(str(tmp_path), "events.csv"), "a") as fh:
        fh.write("P,e2,0,2019-01-01,encounter\n")
    _expect_rejected(plan, "event_row_invalid")


def test_reject_null_person(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(os.path.join(str(tmp_path), "events.csv"), "a") as fh:
        fh.write(",e2,0,5,encounter\n")
    _expect_rejected(plan, "event_row_invalid")


def test_reject_null_day_with_kind(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(os.path.join(str(tmp_path), "events.csv"), "a") as fh:
        fh.write("P,e2,0,,encounter\n")
    _expect_rejected(plan, "event_row_invalid")


def test_reject_bad_kind(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(os.path.join(str(tmp_path), "events.csv"), "a") as fh:
        fh.write("P,e2,0,5,visit\n")
    _expect_rejected(plan, "event_row_invalid")


def test_reject_non_integer_revision(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(os.path.join(str(tmp_path), "events.csv"), "a") as fh:
        fh.write("P,e2,abc,5,encounter\n")
    _expect_rejected(plan, "event_row_invalid")


def test_reject_inverted_coverage_interval(tmp_path):
    cov, src = _base_sources(tmp_path)
    cov["A"]["P"] = [(9, 7)]
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    _expect_rejected(plan, "coverage_inverted_interval")


def test_reject_null_coverage_endpoint(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(os.path.join(str(tmp_path), "coverage_A.csv"), "a") as fh:
        fh.write("P,5,\n")
    _expect_rejected(plan, "coverage_row_invalid")


def test_reject_identity_conflict_same_revision(tmp_path):
    events = _base_events() + [
        dict(person="P", event_id="e1", revision=0, day=99, kind="encounter")
    ]
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), events, cov, src, W, F)
    _expect_rejected(plan, "event_identity_conflict")


def test_accept_exact_duplicate_rows(tmp_path):
    events = _base_events() + [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter")
    ]
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), events, cov, src, W, F)
    con = run_build(plan)
    assert con.execute("SELECT count(*) FROM effective_events").fetchone()[0] == 2


def test_reject_duplicate_csv_header(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(os.path.join(str(tmp_path), "events.csv"), "w") as fh:
        fh.write("person,event_id,revision,day,kind,day\n")
        fh.write("P,e1,0,10,encounter,11\n")
    _expect_rejected(plan, "duplicate_column")


def test_reject_negative_window_and_revision_out_of_range(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(os.path.join(str(tmp_path), "events.csv"), "a") as fh:
        fh.write("P,e9," + "9" * 40 + ",5,encounter\n")
    _expect_rejected(plan, "event_value_out_of_range")


def test_partial_tombstone_rejected(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(os.path.join(str(tmp_path), "events.csv"), "a") as fh:
        fh.write("P,e9,1,,encounter\n")
    _expect_rejected(plan, "event_row_invalid")


def test_unsupported_file_extension(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(plan) as fh:
        doc = json.load(fh)
    doc["events_file"] = "events.txt"
    with open(os.path.join(str(tmp_path), "events.txt"), "w") as fh:
        fh.write("person,event_id,revision,day,kind\nP,e,0,1,encounter\n")
    with open(plan, "w") as fh:
        json.dump(doc, fh)
    _expect_rejected(plan, "unsupported_file_type")


def test_header_only_events_file_is_accepted(tmp_path):
    cov, src = _base_sources(tmp_path)
    plan = write_package(str(tmp_path), _base_events(), cov, src, W, F)
    with open(os.path.join(str(tmp_path), "events.csv"), "w") as fh:
        fh.write("person,event_id,revision,day,kind\n")
    con = run_build(plan)
    # only coverage-declared persons remain, all with no encounter
    assert con.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
    assert con.execute(
        "SELECT count(*) FROM person_status WHERE status = 'no_candidate'"
    ).fetchone()[0] == 1


def test_plan_json_malformed(tmp_path):
    _expect_rejected(str(tmp_path / "nope.json"), "plan_missing")
