"""Package archive helper for tests and benchmarks.

Creates an in-memory package on disk with:
  * events.csv (csv, like the spec examples)
  * one coverage CSV per source
  * plan.json with relative paths
"""

from __future__ import annotations

import csv
import json
import os


def build_package(
    root: str,
    *,
    persons: list[str],
    encounters_per_person: int,
    W: int,
    F: int,
    events_format: str = "csv",
    event_rows: int | None = None,
    coverage_for: str | None = None,
) -> str:
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "events.csv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["person", "event_id", "revision", "day", "kind"])
        n = 0
        for p in persons:
            d = 0
            for i in range(encounters_per_person):
                d += 37
                n += 1
                w.writerow([p, f"e{n}", i, d, "encounter"])
                if i % 3 == 0:
                    n += 1
                    w.writerow([p, f"q{n}", 0, d + 1, "qualifying"])
        if event_rows is not None:
            while n < event_rows:
                n += 1
                w.writerow([f"X{n % 5000}", f"x{n}", 0, n % 10000, "qualifying"])

    cov_a = os.path.join(root, "cov_A.csv")
    with open(cov_a, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["person", "start_day", "end_day"])
        for p in persons:
            for k in range(encounters_per_person // 5 + 1):
                s = k * 1000
                w.writerow([p, s, s + 999])

    cov_q = os.path.join(root, "cov_Q.csv")
    with open(cov_q, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["person", "start_day", "end_day"])
        for p in persons:
            for k in range(encounters_per_person // 5 + 1):
                s = k * 1000
                w.writerow([p, s, s + 999])

    plan = {
        "plan_id": "bench",
        "lookback_days": W,
        "confirm_days": F,
        "events_file": "events.csv",
        "sources": [
            {"source_id": "A", "kind": "exclusion", "coverage_file": "cov_A.csv"},
            {"source_id": "Q", "kind": "qualifying", "coverage_file": "cov_Q.csv"},
        ],
    }
    plan_path = os.path.join(root, "plan.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh)
    return plan_path
