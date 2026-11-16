"""Scale tests: window length must not drive cost; no per-day enumeration."""

from __future__ import annotations

import time

from helpers import run_build, write_package


def test_billion_day_windows_are_fast(tmp_path):
    big_w, big_f = 10**9, 10**9
    events = [
        dict(person="P", event_id="e1", revision=0, day=0, kind="encounter"),
        dict(person="P", event_id="q", revision=0, day=big_f, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), events,
        {"A": {"P": [(-big_w, -1)]}, "Q": {"P": [(0, big_f)]}},
        {"A": "exclusion", "Q": "qualifying"}, big_w, big_f,
    )
    started = time.perf_counter()
    con = run_build(plan)
    elapsed = time.perf_counter() - started
    assert elapsed < 10.0
    assert con.execute("SELECT decision FROM candidate_decisions").fetchall() == [("true",)]
    # one covered piece per window, zero gaps: no day was ever enumerated
    assert con.execute("SELECT count(*) FROM piece_rows").fetchone()[0] == 2
    assert con.execute(
        "SELECT window_name, piece_start, piece_end FROM piece_rows ORDER BY 1"
    ).fetchall() == [("confirm", 0, big_f), ("lookback", -big_w, -1)]
    assert con.execute("SELECT count(*) FROM gap_rows").fetchone()[0] == 0


def test_no_day_table_is_created(tmp_path):
    events = [dict(person="P", event_id=f"e{i}", revision=0, day=i * 10, kind="encounter")
              for i in range(20)]
    plan = write_package(
        str(tmp_path), events,
        {"A": {"P": [(0, 200)]}, "Q": {"P": [(0, 200)]}},
        {"A": "exclusion", "Q": "qualifying"}, 30, 15,
    )
    con = run_build(plan)
    names = {r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables()").fetchall()}
    assert not any("day" in n and n not in {"effective_daily"} for n in names)
    # candidate count equals distinct encounter days, not day span
    assert con.execute("SELECT count(*) FROM candidates").fetchone()[0] == 20


def test_many_persons_without_blowup(tmp_path):
    events = []
    for p in range(200):
        for i in range(10):
            events.append(dict(person=f"P{p}", event_id=f"e{p}_{i}", revision=0,
                               day=i * 20, kind="encounter"))
    plan = write_package(
        str(tmp_path), events,
        {"A": {f"P{p}": [(0, 200)] for p in range(200)},
         "Q": {f"P{p}": [(0, 200)] for p in range(200)}},
        {"A": "exclusion", "Q": "qualifying"}, 5, 5,
    )
    started = time.perf_counter()
    con = run_build(plan)
    assert time.perf_counter() - started < 30
    assert con.execute("SELECT count(*) FROM person_status").fetchone()[0] == 200
