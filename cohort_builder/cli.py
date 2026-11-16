"""Command line entry point.

    python -m cohort_builder build  --plan plan.json --out out/
    python -m cohort_builder check  --plan plan.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import duckdb

from .config import load_plan
from .engine import build
from .errors import InputRejected
from .report import OutputExists, export_tables, fingerprint_inputs, prepare_out_dir, report_dict


def _connect(
    db_target: str | None,
    temp_dir: str | None,
    threads: int | None,
    memory_limit: str | None = None,
) -> duckdb.DuckDBPyConnection:
    if db_target:
        os.makedirs(os.path.dirname(os.path.abspath(db_target)) or ".", exist_ok=True)
        con = duckdb.connect(db_target)
    else:
        con = duckdb.connect()
    if temp_dir:
        os.makedirs(temp_dir, exist_ok=True)
        con.execute("SET temp_directory = ?", [temp_dir])
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    if memory_limit:
        con.execute("SET memory_limit = ?", [memory_limit])
    con.execute("SET preserve_insertion_order = true")
    return con


def _write_json(path: str, doc: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def cmd_check(args: argparse.Namespace) -> int:
    plan, issues = load_plan(args.plan)
    if issues:
        doc = {
            "status": "rejected",
            "total_issues": len(issues),
            "issues": [i.as_dict() for i in issues],
        }
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 2
    assert plan is not None
    if not args.emit_staged:
        doc = {
            "status": "ok",
            "plan": plan.as_public_dict(),
            "warnings": [w.as_dict() for w in plan.warnings],
        }
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0

    # Full structural check of the data files (no derivation).
    from .ingest import stage_package

    con = _connect(None, args.temp_dir, args.threads, args.memory_limit)
    try:
        stats = stage_package(con, plan)
    except InputRejected as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2))
        return 2
    doc = {
        "status": "ok",
        "plan": plan.as_public_dict(),
        "events": {
            "raw_rows": stats.raw_event_rows,
            "valid_rows": stats.valid_event_rows,
            "retracted_facts": stats.retraction_rows,
            "effective_rows": stats.effective_event_rows,
            "by_kind": stats.event_counts,
        },
        "coverage": {
            "rows": stats.coverage_rows,
            "persons": stats.coverage_persons,
            "sources": stats.source_stats,
        },
        "warnings": [w.as_dict() for w in plan.warnings],
    }
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    try:
        prepare_out_dir(args.out, force=args.force)
    except OutputExists as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 3

    plan, issues = load_plan(args.plan)
    if issues:
        doc = {
            "status": "rejected",
            "phase": "plan",
            "total_issues": len(issues),
            "issues": [i.as_dict() for i in issues],
        }
        _write_json(os.path.join(args.out, "run_report.json"), doc)
        print(json.dumps(doc, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    assert plan is not None

    con = _connect(args.db, args.temp_dir, args.threads, args.memory_limit)
    try:
        try:
            stats = build(con, plan)
        except InputRejected as exc:
            doc = exc.as_dict()
            doc["phase"] = "ingest"
            _write_json(os.path.join(args.out, "run_report.json"), doc)
            print(json.dumps(doc, ensure_ascii=False, indent=2), file=sys.stderr)
            return 2

        written = export_tables(con, args.out, stats)
        inputs = fingerprint_inputs(plan)
        inputs["plan"] = {
            "path": os.path.abspath(args.plan),
            "sha256": __import__("hashlib").sha256(
                open(args.plan, "rb").read()).hexdigest(),
        }
        doc = report_dict(plan, stats, os.path.abspath(args.out), written, inputs)
        doc["wall_seconds"] = round(time.perf_counter() - started, 3)
        _write_json(os.path.join(args.out, "run_report.json"), doc)
        summary = {
            "status": "ok",
            "counts": doc["counts"],
            "outputs": doc["outputs"],
            "timings_seconds": doc["timings_seconds"],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cohort_builder",
        description="纵向就诊流水 → 分析队列索引日（三值判定，含来源覆盖证据）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--plan", required=True, help="方案 JSON 路径")
        p.add_argument("--out", default="./out", help="输出目录（默认 ./out）")
        p.add_argument("--db", default=None, help="可选 DuckDB 数据库文件，用于落盘/复查")
        p.add_argument("--temp-dir", default=None, help="DuckDB 溢出临时目录")
        p.add_argument("--threads", type=int, default=None, help="DuckDB 线程数")
        p.add_argument("--memory-limit", default=None, help="DuckDB 内存上限，如 2GB")

    p_build = sub.add_parser("build", help="构建队列")
    common(p_build)
    p_build.add_argument(
        "--force", action="store_true",
        help="输出目录已有结果时，将旧结果移到 <out>.prev 后重建（默认拒绝覆盖）",
    )
    p_build.set_defaults(func=cmd_build)

    p_check = sub.add_parser("check", help="校验方案与来源声明")
    common(p_check)
    p_check.add_argument(
        "--emit-staged", action="store_true", help="同时完整解析事件/覆盖文件做结构校验"
    )
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
