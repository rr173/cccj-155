"""Test helpers: build a package on disk and run the CLI/engine."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import duckdb

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE not in sys.path:
    sys.path.insert(0, WORKSPACE)

from cohort_builder.config import load_plan  # noqa: E402
from cohort_builder.engine import build  # noqa: E402


def write_package(
    root: str,
    events: list[dict],
    coverage: dict[str, dict[str, list[tuple[int, int]]]],
    sources: dict[str, str],
    W: int,
    F: int,
    plan_extra: dict | None = None,
) -> str:
    """Write events.csv, one coverage csv per source and plan.json.

    coverage: source_id -> {person: [(start, end), ...]}
    sources:  source_id -> kind
    """

    os.makedirs(root, exist_ok=True)
    events_path = os.path.join(root, "events.csv")
    with open(events_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("person,event_id,revision,day,kind\n")
        for ev in events:
            day = "" if ev.get("day") is None else ev["day"]
            kind = "" if ev.get("kind") is None else ev["kind"]
            fh.write(f"{ev['person']},{ev['event_id']},{ev['revision']},{day},{kind}\n")

    source_decls = []
    for sid, kind in sorted(sources.items()):
        fname = f"coverage_{sid}.csv"
        path = os.path.join(root, fname)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("person,start_day,end_day\n")
            for person, intervals in sorted(coverage.get(sid, {}).items()):
                for s, e in intervals:
                    fh.write(f"{person},{s},{e}\n")
        source_decls.append({"source_id": sid, "kind": kind, "coverage_file": fname})

    plan = {
        "plan_id": "test",
        "lookback_days": W,
        "confirm_days": F,
        "events_file": "events.csv",
        "sources": source_decls,
    }
    if plan_extra:
        plan.update(plan_extra)
    plan_path = os.path.join(root, "plan.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=2)
    return plan_path


def run_build(plan_path: str) -> duckdb.DuckDBPyConnection:
    plan, issues = load_plan(plan_path)
    assert not issues, [i.as_dict() for i in issues]
    con = duckdb.connect()
    build(con, plan)
    return con


def run_cli(plan_path: str, out_dir: str, *extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = WORKSPACE
    return subprocess.run(
        [sys.executable, "-m", "cohort_builder", "build", "--plan", plan_path, "--out", out_dir, *extra],
        capture_output=True,
        text=True,
        env=env,
        cwd=WORKSPACE,
    )
