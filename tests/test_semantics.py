"""End-to-end semantics tests, hand-computed for W=3, F=2.

Convention: lookback = [d-W, d) (end exclusive), confirm = [d, d+F] (both
endpoints inclusive).  With W=3, F=2 candidate 10 gives lookback 7..9 and
confirm 10..12.
"""

from __future__ import annotations

import json
import os

from helpers import run_build, write_package
from reference import reference_build

W, F = 3, 2


def _main_package(root, coverage_a, coverage_b=None, extra_events=(), removal=(), drop_q21=False):
    events = [
        dict(person="P", event_id="e10", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="e20", revision=0, day=20, kind="encounter"),
        dict(person="P", event_id="q11", revision=0, day=11, kind="qualifying"),
    ]
    if not drop_q21:
        events.append(dict(person="P", event_id="q21", revision=0, day=21, kind="qualifying"))
    events.extend(extra_events)
    cov = {
        "A": {"P": coverage_a},
        "B": {"P": coverage_b if coverage_b is not None else [(0, 20)]},
    }
    return write_package(root, events, cov, {"A": "exclusion", "B": "qualifying"}, W, F)


def _status(con, person="P"):
    return con.execute(
        "SELECT status, index_day, earliest_pending_day FROM person_status WHERE person = ?",
        [person],
    ).fetchone()


def _decisions(con, person="P"):
    return {
        row[0]: row[1]
        for row in con.execute(
            "SELECT cand_day, decision FROM candidate_decisions WHERE person = ? ORDER BY cand_day",
            [person],
        ).fetchall()
    }


# ---------------------------------------------------------------------------
# The main worked example from the specification
# ---------------------------------------------------------------------------


def test_main_example_day8_missing_is_unknown_and_person_pending(tmp_path):
    plan = _main_package(str(tmp_path), coverage_a=[(0, 7), (9, 20)])
    con = run_build(plan)
    assert _decisions(con) == {10: "unknown", 20: "true"}
    # The person is pending on day 10 and must NOT jump to the later day 20.
    assert _status(con) == ("pending", None, 10)
    later = con.execute("SELECT later_true_days_json FROM later_true").fetchall()
    assert json.loads(later[0][0]) == [20]


def test_main_example_backfill_day8_gives_index_10(tmp_path):
    plan = _main_package(str(tmp_path), coverage_a=[(0, 7), (8, 20)])
    con = run_build(plan)
    assert _decisions(con) == {10: "true", 20: "true"}
    assert _status(con) == ("indexed", 10, None)


def test_main_example_exclusion_at_8_flips_to_index_20(tmp_path):
    plan = _main_package(
        str(tmp_path),
        coverage_a=[(0, 7), (9, 20)],  # coverage still has the day-8 hole
        extra_events=[dict(person="P", event_id="x8", revision=0, day=8, kind="exclusion")],
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "false", 20: "true"}
    assert _status(con) == ("indexed", 20, None)


def test_main_example_day20_confirm_incomplete_and_q21_removed_is_unknown(tmp_path):
    plan = _main_package(
        str(tmp_path),
        coverage_a=[(0, 7), (8, 20)],
        coverage_b=[(0, 20)],  # confirm window 20..22 not covered past 20
        drop_q21=True,
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "true", 20: "unknown"}
    # 10 is already true for the first candidate, so the person stays indexed.
    assert _status(con) == ("indexed", 10, None)


# ---------------------------------------------------------------------------
# Window endpoint inclusion rules
# ---------------------------------------------------------------------------


def test_confirmation_endpoint_is_inclusive(tmp_path):
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="q", revision=0, day=12, kind="qualifying"),  # d+F
    ]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(7, 9)]}, "B": {"P": [(10, 10)]}}, {"A": "exclusion", "B": "qualifying"}, W, F
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "true"}


def test_confirmation_after_endpoint_does_not_count(tmp_path):
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="q", revision=0, day=13, kind="qualifying"),  # d+F+1
    ]
    plan = write_package(
        str(tmp_path),
        ev,
        {"A": {"P": []}, "B": {"P": [(10, 12)]}},
        {"A": "exclusion", "B": "qualifying"},
        W,
        F,
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "false"}


def test_lookback_endpoint_is_exclusive(tmp_path):
    # exclusion exactly on d is inside *confirm*, not lookback; it does not
    # disqualify the candidate (the spec only excludes [d-W, d)).
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="x", revision=0, day=10, kind="exclusion"),
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(7, 9)]}, "B": {"P": [(10, 12)]}}, {"A": "exclusion", "B": "qualifying"}, W, F
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "true"}


def test_lookback_start_is_inclusive(tmp_path):
    # exclusion on exactly d-W disqualifies.
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="x", revision=0, day=7, kind="exclusion"),
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(7, 9)]}, "B": {"P": [(10, 12)]}}, {"A": "exclusion", "B": "qualifying"}, W, F
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "false"}


def test_day_before_lookback_start_is_outside(tmp_path):
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="x", revision=0, day=6, kind="exclusion"),  # d-W-1
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(6, 9)]}, "B": {"P": [(10, 12)]}}, {"A": "exclusion", "B": "qualifying"}, W, F
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "true"}


# ---------------------------------------------------------------------------
# Coverage semantics
# ---------------------------------------------------------------------------


def test_source_a_coverage_cannot_vouch_for_source_b(tmp_path):
    # Two exclusion sources.  A covers the lookback completely, B does not.
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path),
        ev,
        {"A": {"P": [(7, 9)]}, "B": {"P": [(7, 8)]}, "Q": {"P": [(10, 12)]}},
        {"A": "exclusion", "B": "exclusion", "Q": "qualifying"},
        W,
        F,
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "unknown"}
    incomplete = con.execute(
        "SELECT source_id FROM window_source_final "
        "WHERE window_name='lookback' AND NOT complete ORDER BY source_id"
    ).fetchall()
    assert [r[0] for r in incomplete] == ["B"]
    gaps = con.execute(
        "SELECT source_id, gap_start, gap_end FROM gap_rows WHERE window_name='lookback'"
    ).fetchall()
    assert gaps == [("B", 9, 9)]


def test_two_adjacent_intervals_merge_into_one_covered_window(tmp_path):
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    # [7,8] and [9,9] touch -> merge to [7,9]; the whole lookback is covered.
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(7, 8), (9, 9)]}, "Q": {"P": [(10, 12)]}},
        {"A": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    merged = con.execute("SELECT seg_start, seg_end FROM coverage_merged WHERE source_id='A'").fetchall()
    assert merged == [(7, 9)]
    assert _decisions(con) == {10: "true"}


def test_single_day_hole_at_lookback_start_is_unknown(tmp_path):
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(8, 9)]}, "Q": {"P": [(10, 12)]}},
        {"A": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "unknown"}
    gaps = con.execute("SELECT gap_start, gap_end FROM gap_rows WHERE window_name='lookback'").fetchall()
    assert gaps == [(7, 7)]


def test_qualifying_outside_coverage_is_still_real_evidence(tmp_path):
    # Qualifying event at day 11 but the source only declares coverage of day
    # 10.  The event is outside declared coverage yet must still be used.
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(7, 9)]}, "Q": {"P": [(10, 10)]}},
        {"A": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "true"}
    ev_json = json.loads(
        con.execute(
            "SELECT explanation_json FROM candidate_explain WHERE cand_day=10"
        ).fetchone()[0]
    )
    qualifiers = ev_json["confirm"]["qualifiers"]
    assert [(q["day"], q["event_id"], q["revision"]) for q in qualifiers] == [
        (11, "q", 0)
    ]
    assert ev_json["confirm"]["coverage_complete"] is False


def test_exclusion_outside_coverage_still_disqualifies(tmp_path):
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="x", revision=0, day=8, kind="exclusion"),
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": []}, "Q": {"P": [(10, 12)]}},
        {"A": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "false"}


def test_empty_lookback_window_when_w_is_zero(tmp_path):
    ev = [dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
          dict(person="P", event_id="q", revision=0, day=10, kind="qualifying")]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": []}, "Q": {"P": [(10, 10)]}},
        {"A": "exclusion", "Q": "qualifying"}, 0, 0,
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "true"}


# ---------------------------------------------------------------------------
# Three-valued composition details
# ---------------------------------------------------------------------------


def test_lookback_unknown_and_confirm_false_is_false(tmp_path):
    # No qualifying and the confirm window is fully covered -> false wins over
    # an unknown lookback.
    ev = [dict(person="P", event_id="e1", revision=0, day=10, kind="encounter")]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(7, 8)]}, "Q": {"P": [(10, 12)]}},
        {"A": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "false"}
    assert _status(con) == ("no_index", None, None)


def test_confirmation_unknown_and_lookback_true_is_unknown(tmp_path):
    ev = [dict(person="P", event_id="e1", revision=0, day=10, kind="encounter")]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(7, 9)]}, "Q": {"P": [(10, 11)]}},
        {"A": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "unknown"}
    assert _status(con) == ("pending", None, 10)


# ---------------------------------------------------------------------------
# Revision / ordering semantics
# ---------------------------------------------------------------------------


def test_latest_revision_wins_regardless_of_file_order(tmp_path):
    events = [
        dict(person="P", event_id="e1", revision=5, day=10, kind="encounter"),
        dict(person="P", event_id="e1", revision=2, day=99, kind="encounter"),  # stale, later in file
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), events, {"A": {"P": [(7, 9)]}, "Q": {"P": [(10, 12)]}},
        {"A": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "true"}
    assert con.execute("SELECT DISTINCT cand_day FROM candidates").fetchall() == [(10,)]


def test_retraction_by_higher_revision_removes_event(tmp_path):
    events = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="e1", revision=1, day=None, kind=None),  # tombstone
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), events, {"A": {"P": [(7, 9)]}, "Q": {"P": [(10, 12)]}},
        {"A": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    assert _status(con) == ("no_candidate", None, None)
    assert "e1" not in [
        r[0] for r in con.execute("SELECT event_id FROM effective_events").fetchall()
    ]


def test_optional_retracted_column(tmp_path):
    root = str(tmp_path)
    events = os.path.join(root, "events.csv")
    with open(events, "w", encoding="utf-8", newline="") as fh:
        fh.write("person,event_id,revision,day,kind,retracted\n")
        fh.write("P,e1,0,10,encounter,\n")
        fh.write("P,q1,0,11,qualifying,\n")
        fh.write("P,q1,1,,,true\n")  # retract the qualifying
    cov = os.path.join(root, "covA.csv")
    with open(cov, "w", encoding="utf-8") as fh:
        fh.write("person,start_day,end_day\nP,7,9\n")
    covq = os.path.join(root, "covQ.csv")
    with open(covq, "w", encoding="utf-8") as fh:
        fh.write("person,start_day,end_day\nP,10,12\n")
    plan = os.path.join(root, "plan.json")
    with open(plan, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "plan_id": "t",
                "lookback_days": W,
                "confirm_days": F,
                "events_file": "events.csv",
                "sources": [
                    {"source_id": "A", "kind": "exclusion", "coverage_file": "covA.csv"},
                    {"source_id": "Q", "kind": "qualifying", "coverage_file": "covQ.csv"},
                ],
            },
            fh,
        )
    con = run_build(plan)
    assert _decisions(con) == {10: "false"}  # retracted qualifying -> confirm false


def test_same_day_multiple_events(tmp_path):
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="e2", revision=0, day=10, kind="encounter"),  # same day duplicate candidate
        dict(person="P", event_id="x1", revision=0, day=8, kind="exclusion"),
        dict(person="P", event_id="x2", revision=0, day=8, kind="exclusion"),
        dict(person="P", event_id="q1", revision=0, day=11, kind="qualifying"),
        dict(person="P", event_id="q2", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(7, 9)]}, "Q": {"P": [(10, 12)]}},
        {"A": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    assert con.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
    assert _decisions(con) == {10: "false"}
    assert con.execute(
        "SELECT exclusion_count FROM candidate_decisions WHERE cand_day=10"
    ).fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Person-level statuses
# ---------------------------------------------------------------------------


def test_no_candidate_person_from_coverage_only(tmp_path):
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(0, 20)], "Z": [(0, 20)]}, "Q": {"P": [(0, 20)]}},
        {"A": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    assert _status(con, "Z") == ("no_candidate", None, None)
    assert _status(con, "P") == ("indexed", 10, None)


def test_all_candidates_false_is_no_index(tmp_path):
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="e2", revision=0, day=20, kind="encounter"),
        dict(person="P", event_id="x1", revision=0, day=8, kind="exclusion"),
        dict(person="P", event_id="x2", revision=0, day=19, kind="exclusion"),
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), ev, {"A": {"P": [(0, 30)]}, "Q": {"P": [(0, 30)]}},
        {"A": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    assert _status(con) == ("no_index", None, None)


def test_split_coverage_cannot_bridge_gap_across_sources_of_same_kind(tmp_path):
    # Source A covers [7,8], source B covers [9,9] for exclusion.  Each source
    # must individually cover the full window, so the lookback is incomplete.
    ev = [
        dict(person="P", event_id="e1", revision=0, day=10, kind="encounter"),
        dict(person="P", event_id="q", revision=0, day=11, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), ev,
        {"A": {"P": [(7, 8)]}, "B": {"P": [(9, 9)]}, "Q": {"P": [(10, 12)]}},
        {"A": "exclusion", "B": "exclusion", "Q": "qualifying"}, W, F,
    )
    con = run_build(plan)
    assert _decisions(con) == {10: "unknown"}


def test_long_span_windows_do_not_iterate_days(tmp_path):
    # A window of 10^9 days with a handful of intervals.
    big_w = 10**9
    big_f = 10**9
    ev = [
        dict(person="P", event_id="e1", revision=0, day=0, kind="encounter"),
        dict(person="P", event_id="q", revision=0, day=big_f, kind="qualifying"),
    ]
    plan = write_package(
        str(tmp_path), ev,
        {"A": {"P": [(-big_w, -1)]}, "Q": {"P": [(0, big_f)]}},
        {"A": "exclusion", "Q": "qualifying"}, big_w, big_f,
    )
    con = run_build(plan)
    assert _decisions(con) == {0: "true"}
    pieces = con.execute(
        "SELECT piece_start, piece_end FROM piece_rows WHERE window_name='lookback'"
    ).fetchall()
    assert pieces == [(-big_w, -1)]
    gaps = con.execute(
        "SELECT count(*) FROM gap_rows WHERE window_name='lookback'"
    ).fetchone()[0]
    assert gaps == 0


def test_reference_matches_on_randomized_small_domain(tmp_path):
    import random

    rng = random.Random(20241116)
    for trial in range(120):
        persons = ["P1", "P2"]
        sources = {"A": "exclusion", "B": "exclusion", "Q": "qualifying", "R": "qualifying"}
        events = []
        coverage: dict[str, dict[str, list[tuple[int, int]]]] = {s: {} for s in sources}
        persons_all = set()
        eid = 0
        for person in persons:
            for _ in range(rng.randint(0, 7)):
                eid += 1
                events.append(
                    dict(
                        person=person,
                        event_id=f"e{eid}",
                        revision=0,
                        day=rng.randint(-2, 18),
                        kind="encounter",
                    )
                )
            for kind in ("qualifying", "exclusion"):
                for _ in range(rng.randint(0, 5)):
                    eid += 1
                    events.append(
                        dict(
                            person=person,
                            event_id=f"e{eid}",
                            revision=0,
                            day=rng.randint(-2, 18),
                            kind=kind,
                        )
                    )
            for src in sources:
                intervals = []
                for _ in range(rng.randint(0, 4)):
                    a = rng.randint(-6, 16)
                    b = a + rng.randint(0, 5)
                    intervals.append((a, b))
                if rng.random() < 0.85:
                    coverage[src][person] = intervals
                persons_all.add(person)
            persons_all.add(person)
        W2 = rng.randint(0, 5)
        F2 = rng.randint(0, 5)
        plan = write_package(
            str(tmp_path / f"t{trial}"), events, coverage, sources, W2, F2
        )
        con = run_build(plan)
        eng = {
            row[0]: (row[1], row[2], row[3])
            for row in con.execute(
                "SELECT person, status, index_day, earliest_pending_day FROM person_status"
            ).fetchall()
        }
        eng_later = {
            row[0]: json.loads(row[1])
            for row in con.execute("SELECT person, later_true_days_json FROM later_true").fetchall()
        }
        eng_cands = {}
        for person, cand, dec in con.execute(
            "SELECT person, cand_day, decision FROM candidate_decisions"
        ).fetchall():
            eng_cands.setdefault(person, {})[cand] = dec

        ref = reference_build(
            events,
            {(s, p): iv for s, d in coverage.items() for p, iv in d.items()},
            sources,
            W2,
            F2,
            sorted(persons_all),
        )
        for person, r in ref.items():
            assert eng[person] == (r.status, r.index_day, r.earliest_pending_day), (
                trial, person, eng[person], r
            )
            if r.status == "pending":
                assert eng_later.get(person, []) == r.later_true
            ref_cands = {d: c["decision"] for d, c in r.candidates.items()}
            assert eng_cands.get(person, {}) == ref_cands, (trial, person, eng_cands.get(person), ref_cands)
            for d, c in r.candidates.items():
                assert c["decision"] in ("true", "false", "unknown")


# ---------------------------------------------------------------------------
# Randomized differential campaigns against the naive day-enumerating oracle
# ---------------------------------------------------------------------------


def test_differential_multiple_sources_and_tombstones(tmp_path):
    import random

    rng = random.Random(99)
    for trial in range(40):
        n_excl = rng.randint(1, 3)
        sources = {f"E{i}": "exclusion" for i in range(n_excl)}
        sources.update({f"Q{i}": "qualifying" for i in range(rng.randint(1, 2))})
        persons = [f"P{i}" for i in range(rng.randint(1, 4))]

        # Build per-identity revisions (including tombstones) and shuffle the
        # file order: business order must never depend on receive order.
        raw = []
        eid = 0
        for person in persons:
            for _ in range(rng.randint(1, 8)):
                eid += 1
                for rev in range(rng.randint(1, 3)):
                    if rng.random() < 0.3:
                        raw.append(dict(person=person, event_id=f"id{eid}", revision=rev,
                                        day=None, kind=None))
                    else:
                        raw.append(dict(person=person, event_id=f"id{eid}", revision=rev,
                                        day=rng.randint(-10, 25),
                                        kind=rng.choice(["encounter", "qualifying", "exclusion"])))
        rng.shuffle(raw)
        latest = {}
        for row in raw:
            key = (row["person"], row["event_id"])
            if key not in latest or row["revision"] > latest[key]["revision"]:
                latest[key] = row
        events = list(latest.values())

        coverage = {s: {} for s in sources}
        for person in persons:
            for src in sources:
                if rng.random() < 0.85:
                    coverage[src][person] = [
                        (a, a + rng.randint(0, 5))
                        for a in (rng.randint(-12, 24) for _ in range(rng.randint(0, 4)))
                    ]
        W2, F2 = rng.randint(0, 5), rng.randint(0, 5)
        plan = write_package(str(tmp_path / f"r{trial}"), raw, coverage, sources, W2, F2)
        con = run_build(plan)

        eng = {
            row[0]: (row[1], row[2], row[3])
            for row in con.execute(
                "SELECT person, status, index_day, earliest_pending_day FROM person_status"
            ).fetchall()
        }
        eng_later = {
            row[0]: json.loads(row[1])
            for row in con.execute(
                "SELECT person, later_true_days_json FROM later_true"
            ).fetchall()
        }
        ref = reference_build(
            events,
            {(s, p): iv for s, d in coverage.items() for p, iv in d.items()},
            sources, W2, F2, persons,
        )
        for person, r in ref.items():
            assert eng[person] == (r.status, r.index_day, r.earliest_pending_day), (
                trial, person, W2, F2, eng[person], r,
            )
            if r.status == "pending":
                assert eng_later.get(person, []) == r.later_true
