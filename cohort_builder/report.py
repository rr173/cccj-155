"""Export tables and the run report."""

from __future__ import annotations

import hashlib
import os
import shutil
from typing import Any

import duckdb

from .config import Plan
from .engine import BuildStats

# (file name, SQL)
EXPORTS: list[tuple[str, str]] = [
    (
        "checks_plan_sources.csv",
        """
        SELECT ds.source_kind, ds.source_id, ds.coverage_file,
               coalesce(s.rows, 0)            AS coverage_rows,
               coalesce(s.persons, 0)         AS covered_persons,
               coalesce(s.merged_segments, 0) AS merged_segments,
               s.min_day, s.max_day,
               'OK' AS status
        FROM declared_sources ds
        LEFT JOIN source_stats_json s USING (source_id)
        ORDER BY ds.source_kind, ds.source_id
        """,
    ),
    (
        "queue.csv",
        """
        SELECT ps.person, ps.status, ps.index_day, ps.earliest_pending_day,
               ps.candidate_count, ps.first_candidate_day, ps.first_nonfalse_day,
               ps.first_nonfalse_decision, ps.true_candidates,
               ps.unknown_candidates, ps.false_candidates,
               coalesce(lt.later_true_count, 0) AS later_true_count,
               lt.later_true_days_json AS later_true_days
        FROM person_status ps
        LEFT JOIN later_true lt USING (person)
        ORDER BY ps.person
        """,
    ),
    (
        "pending.csv",
        """
        SELECT person, earliest_pending_day, window_name, source_id, source_kind,
               gap_start, gap_end, missing_days, window_gap_days, later_true_days_json
        FROM pending_detail
        ORDER BY person, window_name, source_id, gap_start
        """,
    ),
    (
        "candidates.csv",
        """
        SELECT person, cand_day, decision, primary_reason,
               lookback_start, lookback_end, confirm_start, confirm_end,
               exclusion_count, qualifying_count,
               lookback_complete, confirm_complete,
               incomplete_lookback_sources, incomplete_confirm_sources
        FROM candidate_decisions
        ORDER BY person, cand_day
        """,
    ),
    (
        "explain_candidates.csv",
        """
        SELECT person, cand_day, decision, primary_reason, explanation_json
        FROM candidate_explain
        ORDER BY person, cand_day
        """,
    ),
    (
        "explain_persons.csv",
        """
        SELECT person, status, index_day, earliest_pending_day,
               later_true_count, person_json
        FROM person_explain
        ORDER BY person
        """,
    ),
]


def file_sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def fingerprint_inputs(plan: Plan) -> dict[str, Any]:
    out: dict[str, Any] = {
        "package_dir": plan.package_dir,
        "events": {
            "path": plan.events_abs_path,
            "bytes": os.path.getsize(plan.events_abs_path),
            "sha256": file_sha256(plan.events_abs_path),
        },
        "coverage": {},
    }
    for src in sorted(plan.sources, key=lambda s: s.source_id):
        out["coverage"][src.source_id] = {
            "path": src.abs_path,
            "kind": src.kind,
            "bytes": os.path.getsize(src.abs_path),
            "sha256": file_sha256(src.abs_path),
        }
    return out


def _stats_rows(con: duckdb.DuckDBPyConnection, stats: BuildStats) -> None:
    con.execute(
        "CREATE OR REPLACE TABLE source_stats_json "
        "(source_id VARCHAR, kind VARCHAR, coverage_file VARCHAR, rows BIGINT, "
        " persons BIGINT, merged_segments BIGINT, min_day HUGEINT, max_day HUGEINT)"
    )
    for row in (stats.staging.source_stats if stats.staging else []):
        con.execute(
            "INSERT INTO source_stats_json VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                row["source_id"],
                row["kind"],
                row["coverage_file"],
                row["rows"],
                row["persons"],
                row["merged_segments"],
                row["min_day"],
                row["max_day"],
            ],
        )


def export_tables(con: duckdb.DuckDBPyConnection, out_dir: str, stats: BuildStats) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    _stats_rows(con, stats)
    written: list[str] = []
    for name, sql in EXPORTS:
        path = os.path.join(out_dir, name)
        con.execute(f"COPY ({sql}) TO ? (HEADER, DELIMITER ',')", [path])
        written.append(name)
    return written


class OutputExists(Exception):
    pass


def prepare_out_dir(out_dir: str, force: bool = False) -> None:
    """Refuse to overwrite an earlier build unless explicitly forced.

    Previously built results are immutable; a new plan or new input versions
    must be written to a new directory (or the caller passes --force to archive
    the old directory instead of deleting it).
    """

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
        return
    existing = [n for n in os.listdir(out_dir) if n.endswith(".csv") or n == "run_report.json"]
    if not existing:
        return
    if not force:
        raise OutputExists(
            f"输出目录已有结果（{len(existing)} 个文件）。旧结果不可覆盖；"
            f"请换新目录，或显式使用 --force 将旧结果归档后重建。"
        )
    archive = out_dir.rstrip(os.sep) + ".prev"
    if os.path.isdir(archive):
        raise OutputExists(f"归档目录已存在: {archive}")
    os.makedirs(os.path.dirname(os.path.abspath(out_dir)) or ".", exist_ok=True)
    shutil.move(out_dir, archive)
    os.makedirs(out_dir, exist_ok=True)


def report_dict(
    plan: Plan,
    stats: BuildStats,
    out_dir: str,
    written: list[str],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    staging = stats.staging
    return {
        "status": "ok",
        "plan": plan.as_public_dict(),
        "inputs": inputs,
        "outputs": {"directory": out_dir, "files": written},
        "counts": {
            "persons": stats.persons,
            "candidates": stats.candidates,
            "indexed": stats.indexed,
            "pending": stats.pending,
            "no_index": stats.no_index,
            "no_candidate": stats.no_candidate,
        },
        "events": {
            "raw_rows": staging.raw_event_rows if staging else None,
            "valid_rows": staging.valid_event_rows if staging else None,
            "retracted_facts": staging.retraction_rows if staging else None,
            "effective_rows": staging.effective_event_rows if staging else None,
            "by_kind": staging.event_counts if staging else None,
        },
        "coverage": {
            "rows": staging.coverage_rows if staging else None,
            "persons": staging.coverage_persons if staging else None,
            "sources": staging.source_stats if staging else None,
        },
        "pending_gap_rows": stats.pending_gap_rows,
        "timings_seconds": stats.timings,
        "warnings": [w.as_dict() for w in plan.warnings],
        "algorithm": {
            "day_enumeration": False,
            "window_cost": "O(events + candidates*sources + segments + capped explanations), "
            "independent of window length",
            "caps": {
                "evidence_events_per_window": 200,
                "coverage_pieces_per_window": 200,
                "coverage_gaps_per_window": 200,
                "candidates_inlined_per_person": 400,
            },
            "authoritative": {
                "all_candidates": "explain_candidates.csv",
                "all_pending_gaps": "pending.csv",
                "all_events_per_window": "explain_candidates.csv (capped with *_truncated flags)",
            },
        },
    }
