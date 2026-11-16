"""CLI contract tests: outputs, rejection exit codes, immutability."""

from __future__ import annotations

import csv
import json
import os

from helpers import run_build, run_cli, write_package

W, F = 3, 2


def _package(root):
    events = [
        dict(person="P", event_id="e10", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="e20", revision=0, day=20, kind="encounter"),
        dict(person="P", event_id="q11", revision=0, day=11, kind="qualifying"),
        dict(person="P", event_id="q21", revision=0, day=21, kind="qualifying"),
        dict(person="Q", event_id="e5", revision=0, day=5, kind="encounter"),
        dict(person="Q", event_id="x1", revision=0, day=1, kind="exclusion"),
    ]
    cov = {
        "A": {"P": [(0, 7), (9, 20)], "Q": [(0, 10)]},
        "B": {"P": [(0, 20)], "Q": [(0, 10)]},
    }
    return write_package(root, events, cov, {"A": "exclusion", "B": "qualifying"}, W, F)


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_cli_build_writes_all_outputs(tmp_path):
    plan = _package(str(tmp_path / "pkg"))
    out = str(tmp_path / "out")
    res = run_cli(plan, out)
    assert res.returncode == 0, res.stderr
    for name in [
        "checks_plan_sources.csv",
        "queue.csv",
        "pending.csv",
        "candidates.csv",
        "explain_candidates.csv",
        "explain_persons.csv",
        "run_report.json",
    ]:
        assert os.path.exists(os.path.join(out, name)), name

    queue = {r["person"]: r for r in _read_csv(os.path.join(out, "queue.csv"))}
    assert queue["P"]["status"] == "pending"
    assert queue["P"]["earliest_pending_day"] == "10"
    assert queue["Q"]["status"] == "no_index"

    pending = _read_csv(os.path.join(out, "pending.csv"))
    assert len(pending) == 1
    assert pending[0]["source_id"] == "A"
    assert pending[0]["gap_start"] == "8"
    assert pending[0]["gap_end"] == "8"
    assert json.loads(pending[0]["later_true_days_json"]) == [20]

    report = json.load(open(os.path.join(out, "run_report.json")))
    assert report["status"] == "ok"
    assert report["counts"]["pending"] == 1
    assert report["inputs"]["events"]["sha256"]
    assert set(report["inputs"]["coverage"]) == {"A", "B"}


def test_cli_refuses_to_overwrite_existing_results(tmp_path):
    plan = _package(str(tmp_path / "pkg"))
    out = str(tmp_path / "out")
    assert run_cli(plan, out).returncode == 0
    res = run_cli(plan, out)
    assert res.returncode == 3
    assert json.loads(res.stderr)["status"] == "refused"


def test_cli_force_archives_previous_results(tmp_path):
    plan = _package(str(tmp_path / "pkg"))
    out = str(tmp_path / "out")
    assert run_cli(plan, out).returncode == 0
    res = run_cli(plan, out, "--force")
    assert res.returncode == 0, res.stderr
    assert os.path.isdir(out + ".prev")


def test_cli_rejects_bad_input_with_exit_code_2(tmp_path):
    plan = _package(str(tmp_path / "pkg"))
    with open(os.path.join(str(tmp_path / "pkg"), "events.csv"), "a") as fh:
        fh.write("P,oops,0,not-a-day,encounter\n")
    out = str(tmp_path / "out2")
    res = run_cli(plan, out)
    assert res.returncode == 2
    doc = json.loads(res.stderr)
    assert doc["status"] == "rejected"
    assert any(i["code"] == "event_row_invalid" for i in doc["issues"])
    report = json.load(open(os.path.join(out, "run_report.json")))
    assert report["status"] == "rejected"


def test_cli_plan_missing(tmp_path):
    out = str(tmp_path / "out")
    res = run_cli(str(tmp_path / "nope.json"), out)
    assert res.returncode == 2
    doc = json.loads(res.stderr)
    assert doc["issues"][0]["code"] == "plan_missing"


def test_explanation_lists_actual_events_and_gaps(tmp_path):
    plan = _package(str(tmp_path / "pkg"))
    con = run_build(plan)
    row = con.execute(
        "SELECT explanation_json FROM candidate_explain WHERE person='P' AND cand_day=10"
    ).fetchone()[0]
    doc = json.loads(row)
    lb = doc["lookback"]
    assert lb["coverage_complete"] is False
    assert lb["sources"][0]["gaps"] == [{"start": 8, "stop": 8}]
    assert lb["sources"][0]["covered"] == [
        {"start": 7, "stop": 7},
        {"start": 9, "stop": 9},
    ]
    assert doc["confirm"]["qualifiers"][0]["day"] == 11
    # a bare boolean is not enough: every source piece/gap is exposed
    assert doc["confirm"]["sources"][0]["complete"] is True
    assert doc["confirm"]["sources"][0]["gaps"] == []


def test_checks_plan_sources_reports_per_source_span(tmp_path):
    plan = _package(str(tmp_path / "pkg"))
    out = str(tmp_path / "out")
    run_cli(plan, out)
    rows = {r["source_id"]: r for r in _read_csv(os.path.join(out, "checks_plan_sources.csv"))}
    assert rows["A"]["source_kind"] == "exclusion"
    assert rows["A"]["covered_persons"] == "2"
    assert rows["A"]["min_day"] == "0"
    assert rows["A"]["max_day"] == "20"
    assert rows["B"]["source_kind"] == "qualifying"


def test_json_and_parquet_inputs(tmp_path):
    root = str(tmp_path / "pkg")
    os.makedirs(root, exist_ok=True)
    jsonl = os.path.join(root, "events.jsonl")
    with open(jsonl, "w", encoding="utf-8") as fh:
        fh.write('{"person":"P","event_id":"e1","revision":0,"day":10,"kind":"encounter"}\n')
        fh.write('{"person":"P","event_id":"q","revision":0,"day":11,"kind":"qualifying"}\n')
    with open(os.path.join(root, "covA.json"), "w", encoding="utf-8") as fh:
        json.dump([{"person": "P", "start_day": 7, "end_day": 9}], fh)
    with open(os.path.join(root, "covQ.json"), "w", encoding="utf-8") as fh:
        json.dump([{"person": "P", "start_day": 10, "end_day": 12}], fh)
    plan = os.path.join(root, "plan.json")
    with open(plan, "w", encoding="utf-8") as fh:
        json.dump({
            "plan_id": "j", "lookback_days": W, "confirm_days": F,
            "events_file": "events.jsonl",
            "sources": [
                {"source_id": "A", "kind": "exclusion", "coverage_file": "covA.json"},
                {"source_id": "Q", "kind": "qualifying", "coverage_file": "covQ.json"},
            ],
        }, fh)
    con = run_build(plan)
    assert con.execute("SELECT decision FROM candidate_decisions").fetchall() == [("true",)]

    # Same package in Parquet.
    con.execute("COPY (SELECT * EXCLUDE (src_line) FROM raw_events) TO ? (FORMAT parquet)",
                [os.path.join(root, "events.parquet")])
    con.execute("COPY (SELECT * EXCLUDE (src_line) FROM raw_cov_0) TO ? (FORMAT parquet)",
                [os.path.join(root, "covA.parquet")])
    con.execute("COPY (SELECT * EXCLUDE (src_line) FROM raw_cov_1) TO ? (FORMAT parquet)",
                [os.path.join(root, "covQ.parquet")])
    with open(plan, "w", encoding="utf-8") as fh:
        json.dump({
            "plan_id": "p", "lookback_days": W, "confirm_days": F,
            "events_file": "events.parquet",
            "sources": [
                {"source_id": "A", "kind": "exclusion", "coverage_file": "covA.parquet"},
                {"source_id": "Q", "kind": "qualifying", "coverage_file": "covQ.parquet"},
            ],
        }, fh)
    con2 = run_build(plan)
    assert con2.execute("SELECT decision FROM candidate_decisions").fetchall() == [("true",)]


def test_case_insensitive_and_padded_headers(tmp_path):
    root = str(tmp_path / "pkg")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "events.csv"), "w", encoding="utf-8") as fh:
        fh.write(" Person ,Event_ID,Revision,DAY,Kind\n")
        fh.write("P,e1,0,10,encounter\nP,q,0,11,qualifying\n")
    for name, rows in (("covA.csv", "P,7,9\n"), ("covQ.csv", "P,10,12\n")):
        with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
            fh.write("person,start_day,end_day\n" + rows)
    plan = os.path.join(root, "plan.json")
    with open(plan, "w", encoding="utf-8") as fh:
        json.dump({
            "plan_id": "c", "lookback_days": W, "confirm_days": F,
            "events_file": "events.csv",
            "sources": [
                {"source_id": "A", "kind": "exclusion", "coverage_file": "covA.csv"},
                {"source_id": "Q", "kind": "qualifying", "coverage_file": "covQ.csv"},
            ],
        }, fh)
    con = run_build(plan)
    assert con.execute("SELECT decision FROM candidate_decisions").fetchall() == [("true",)]
