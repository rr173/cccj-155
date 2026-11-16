"""Typed ingestion of the event package into DuckDB.

CSV / JSON / Parquet files are read exclusively by DuckDB.  Every value enters
as VARCHAR and is validated with explicit regexes and casts.  Nothing from the
input package is imported or executed as Python.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field

import duckdb

from .config import EVENT_COLUMNS, MAX_ABS_DAY, Plan
from .errors import InputRejected, Issue, IssueCollector, reject

_INT_RE = r"^[+-]?[0-9]+$"
_STR_FORBIDDEN_RE = r"[[:space:][:cntrl:]]"
_TRUE_FLAGS = ("true", "1", "t", "yes", "y")
_FALSE_FLAGS = ("false", "0", "f", "no", "n")

RESERVED_COLUMN = "src_line"
_TABLE_FUNCS = {
    ".csv": "csv",
    ".tsv": "csv",
    ".parquet": "parquet",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
}
SUPPORTED_EXT_TEXT = ".csv/.tsv/.parquet/.json/.jsonl"


@dataclass
class StagingStats:
    raw_event_rows: int = 0
    valid_event_rows: int = 0
    retraction_rows: int = 0
    effective_event_rows: int = 0
    coverage_rows: int = 0
    coverage_persons: int = 0
    sources: list[str] = field(default_factory=list)
    source_stats: list[dict] = field(default_factory=list)
    event_counts: dict[str, int] = field(default_factory=dict)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _normalize_col(name: str) -> str:
    return name.strip().lower()


def _check_csv_header(path: str) -> list[Issue]:
    """Detect duplicate / empty / reserved header names that DuckDB would rename."""

    issues: list[Issue] = []
    sep = "\t" if path.lower().endswith(".tsv") else ","
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            first = fh.readline()
    except (OSError, UnicodeDecodeError) as exc:
        return [Issue("file_unreadable", f"文件无法按文本读取: {exc}", file=path)]
    if not first.strip():
        return issues  # empty file: parsed column list will be empty
    try:
        header = next(csv.reader([first], delimiter=sep, quotechar='"'))
    except csv.Error as exc:
        return [Issue("csv_header_unreadable", f"CSV 表头无法解析: {exc}", file=path)]

    seen: dict[str, int] = {}
    for idx, raw in enumerate(header, start=1):
        name = _normalize_col(raw)
        if name == "":
            issues.append(
                Issue("empty_column_name", "CSV 表头存在空列名", file=path, field=f"column {idx}")
            )
            continue
        if name == RESERVED_COLUMN:
            issues.append(
                Issue("reserved_column_name", f"列名 {name!r} 为保留名", file=path, field=name)
            )
            continue
        if name in seen:
            issues.append(
                Issue(
                    "duplicate_column",
                    f"表头列名 {name!r} 重复（第 {seen[name]} 与第 {idx} 列）",
                    file=path,
                    field=name,
                    details={"first_column": seen[name], "second_column": idx},
                )
            )
        else:
            seen[name] = idx
    return issues


def _describe_source(con: duckdb.DuckDBPyConnection, path: str) -> list[str]:
    ext = os.path.splitext(path)[1].lower()
    kind = _TABLE_FUNCS.get(ext)
    if kind is None:
        reject(
            "unsupported_file_type",
            f"不支持的文件扩展名 {ext or '(无)'}；支持 {SUPPORTED_EXT_TEXT}",
            file=path,
        )
    if kind == "csv":
        sep = "\\t" if ext == ".tsv" else ","
        rows = con.execute(
            "DESCRIBE SELECT * FROM read_csv(?, header=true, all_varchar=true, "
            "nullstr='', delim=?, quote='\"', escape='\"', strict_mode=false) LIMIT 0",
            [path, sep],
        ).fetchall()
    elif kind == "json":
        rows = con.execute(
            "DESCRIBE SELECT COLUMNS(*)::VARCHAR FROM read_json(?, format='auto') LIMIT 0",
            [path],
        ).fetchall()
    else:
        rows = con.execute(
            "DESCRIBE SELECT COLUMNS(*)::VARCHAR FROM read_parquet(?) LIMIT 0",
            [path],
        ).fetchall()
    return [row[0] for row in rows]


def _raw_relation(path: str) -> tuple[str, list]:
    ext = os.path.splitext(path)[1].lower()
    kind = _TABLE_FUNCS.get(ext)
    if kind == "csv":
        sep = "\\t" if ext == ".tsv" else ","
        sql = (
            "SELECT * FROM read_csv(?, header=true, all_varchar=true, nullstr='', "
            "delim=?, quote='\"', escape='\"', strict_mode=false)"
        )
        return sql, [path, sep]
    if kind == "json":
        return "SELECT COLUMNS(*)::VARCHAR FROM read_json(?, format='auto')", [path]
    if kind == "parquet":
        return "SELECT COLUMNS(*)::VARCHAR FROM read_parquet(?)", [path]
    reject(
        "unsupported_file_type",
        f"不支持的文件扩展名 {ext or '(无)'}；支持 {SUPPORTED_EXT_TEXT}",
        file=path,
    )
    raise AssertionError("unreachable")


def stage_table(
    con: duckdb.DuckDBPyConnection,
    table: str,
    path: str,
    required: tuple[str, ...],
) -> dict[str, str]:
    """Materialize an external file as VARCHAR columns + 1-based ``src_line``.

    Column names are lower-cased/trimmed to normalized aliases.  Structural
    problems raise InputRejected.  Returns normalized -> original headers.
    """

    problems = IssueCollector()
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".tsv"):
        problems.add_many(_check_csv_header(path))

    try:
        columns = _describe_source(con, path)
    except duckdb.Error as exc:
        problems.add(Issue("file_read_error", f"文件无法读取或格式错误: {exc}", file=path))
        problems.raise_if_any()
        raise AssertionError("unreachable")

    mapping: dict[str, str] = {}
    for original in columns:
        norm = _normalize_col(original)
        if norm == "":
            problems.add(Issue("empty_column_name", "文件存在空列名", file=path))
        elif norm == RESERVED_COLUMN:
            problems.add(
                Issue("reserved_column_name", f"列名 {norm!r} 为保留名", file=path, field=norm)
            )
        elif norm in mapping:
            problems.add(
                Issue("duplicate_column", f"列名 {norm!r} 重复", file=path, field=norm)
            )
        else:
            mapping[norm] = original

    missing = [c for c in required if c not in mapping]
    if missing:
        problems.add(
            Issue(
                "missing_columns",
                "文件缺少必需列: " + ", ".join(missing),
                file=path,
                details={"missing": missing, "found": sorted(mapping)},
            )
        )
    problems.raise_if_any()

    select_list = ", ".join(
        f"{_quote_ident(original)} AS {_quote_ident(norm)}"
        for norm, original in mapping.items()
    )
    raw_sql, params = _raw_relation(path)
    try:
        con.execute(
            f"CREATE OR REPLACE TABLE {table} AS "
            f"SELECT row_number() OVER () AS {RESERVED_COLUMN}, {select_list} "
            f"FROM ({raw_sql}) ORDER BY {RESERVED_COLUMN}",
            params,
        )
    except duckdb.Error as exc:
        problems.add(Issue("file_read_error", f"文件读取失败: {exc}", file=path))
        problems.raise_if_any()
        raise AssertionError("unreachable")
    return mapping


def _try_stage(
    con: duckdb.DuckDBPyConnection,
    problems: IssueCollector,
    table: str,
    path: str,
    required: tuple[str, ...],
) -> bool:
    try:
        stage_table(con, table, path, required)
        return True
    except InputRejected as exc:
        problems.add_many(exc.issues)
        return False
    except duckdb.Error as exc:
        problems.add(Issue("file_read_error", f"文件读取失败: {exc}", file=path))
        return False


def _event_checks(con: duckdb.DuckDBPyConnection, plan: Plan, problems: IssueCollector) -> None:
    has_retracted = (
        con.execute(
            "SELECT count(*) FROM duckdb_columns() "
            "WHERE table_name = 'raw_events' AND column_name = 'retracted'"
        ).fetchone()[0]
        > 0
    )
    retracted_expr = "lower(trim(retracted))" if has_retracted else "NULL"

    if has_retracted:
        bad_flags = con.execute(
            f"""
            SELECT src_line, retracted FROM raw_events
            WHERE retracted IS NOT NULL
              AND lower(trim(retracted)) NOT IN ({",".join("'" + v + "'" for v in _TRUE_FLAGS + _FALSE_FLAGS)})
            ORDER BY src_line
            """
        ).fetchall()
        for line_no, val in bad_flags:
            problems.add(
                Issue(
                    "event_bad_retracted_flag",
                    "retracted 列只允许 true/false/1/0/yes/no/空",
                    file=plan.events_abs_path,
                    row=line_no,
                    field="retracted",
                    value=val,
                )
            )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE events_coded AS
        SELECT src_line, person, event_id, revision, day, kind,
          {retracted_expr} AS retracted_flag,
          CASE WHEN person IS NULL OR regexp_matches(person, '{_STR_FORBIDDEN_RE}')
               THEN 'person_invalid' END AS e_person,
          CASE WHEN event_id IS NULL OR regexp_matches(event_id, '{_STR_FORBIDDEN_RE}')
               THEN 'event_id_invalid' END AS e_event_id,
          CASE WHEN revision IS NULL OR NOT regexp_full_match(revision, '{_INT_RE}')
               THEN 'revision_invalid' END AS e_revision,
          CASE WHEN day IS NOT NULL AND NOT regexp_full_match(day, '{_INT_RE}')
               THEN 'day_invalid' END AS e_day,
          CASE WHEN kind IS NOT NULL AND kind NOT IN ('encounter','qualifying','exclusion')
               THEN 'kind_invalid' END AS e_kind,
          CASE WHEN (day IS NULL) <> (kind IS NULL)
               THEN 'partial_fact' END AS e_partial,
          CASE WHEN {retracted_expr} IN ({",".join("'" + v + "'" for v in _FALSE_FLAGS)})
                 AND day IS NULL AND kind IS NULL
               THEN 'explicit_live_without_fact' END AS e_live_without_fact,
          CASE WHEN {retracted_expr} IN ({",".join("'" + v + "'" for v in _TRUE_FLAGS)})
                 AND (day IS NOT NULL OR kind IS NOT NULL)
               THEN 'tombstone_with_payload' END AS e_tombstone_payload,
        FROM raw_events
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE events_flagged AS
        SELECT *,
          list_filter(
            [e_person, e_event_id, e_revision, e_day, e_kind,
             e_partial, e_live_without_fact, e_tombstone_payload],
            lambda x: x IS NOT NULL) AS codes
        FROM events_coded
        """
    )
    for line_no, person, event_id, kind, codes in con.execute(
        "SELECT src_line, person, event_id, kind, codes FROM events_flagged "
        "WHERE len(codes) > 0 ORDER BY src_line"
    ).fetchall():
        problems.add(
            Issue(
                "event_row_invalid",
                "事件行字段不合法: " + ", ".join(codes),
                file=plan.events_abs_path,
                row=line_no,
                details={"person": person, "event_id": event_id, "kind": kind, "codes": codes},
            )
        )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE events_valid AS
        SELECT src_line, person, event_id,
               TRY_CAST(revision AS HUGEINT) AS revision,
               TRY_CAST(day AS HUGEINT) AS day,
               kind,
               (
                 retracted_flag IN ({",".join("'" + v + "'" for v in _TRUE_FLAGS)})
                 AND day IS NULL AND kind IS NULL
               ) AS is_tombstone,
               (TRY_CAST(revision AS HUGEINT) IS NULL AND revision IS NOT NULL) AS rev_overflow,
               (TRY_CAST(day AS HUGEINT) IS NULL AND day IS NOT NULL) AS day_overflow
        FROM events_flagged
        WHERE len(codes) = 0
        """
    )
    for line_no, val in con.execute(
        "SELECT src_line, revision FROM events_valid "
        "WHERE rev_overflow OR abs(revision) > ? ORDER BY src_line", [MAX_ABS_DAY]
    ).fetchall():
        problems.add(
            Issue(
                "event_value_out_of_range",
                f"revision 超出允许范围 ±{MAX_ABS_DAY}",
                file=plan.events_abs_path,
                row=line_no,
                field="revision",
                value=val,
            )
        )
    for line_no, val in con.execute(
        "SELECT src_line, day FROM events_valid "
        "WHERE day_overflow OR abs(day) > ? ORDER BY src_line", [MAX_ABS_DAY]
    ).fetchall():
        problems.add(
            Issue(
                "event_value_out_of_range",
                f"day 超出允许范围 ±{MAX_ABS_DAY}",
                file=plan.events_abs_path,
                row=line_no,
                field="day",
                value=val,
            )
        )

    # One (person, event_id, revision) must resolve to exactly one fact.
    for person, event_id, revision, variants in con.execute(
        """
        SELECT person, event_id, revision,
               count(DISTINCT struct_pack(day := day, kind := kind)) AS variants
        FROM events_valid
        GROUP BY person, event_id, revision
        HAVING count(DISTINCT struct_pack(day := day, kind := kind)) > 1
        ORDER BY person, event_id, revision
        LIMIT 200
        """
    ).fetchall():
        problems.add(
            Issue(
                "event_identity_conflict",
                "同一 person+event_id+revision 对应多个不同事实；版本改文属于输入冲突",
                file=plan.events_abs_path,
                details={
                    "person": person,
                    "event_id": event_id,
                    "revision": str(revision),
                    "variants": variants,
                },
            )
        )
    problems.raise_if_any()

    # Exact duplicate rows collapse; the highest revision per identity wins.
    # File order is only used to pick a representative of identical rows.
    con.execute(
        """
        CREATE OR REPLACE TABLE events_deduped AS
        SELECT * FROM (
          SELECT *, row_number() OVER (
            PARTITION BY person, event_id, revision ORDER BY src_line
          ) AS dup_rank
          FROM events_valid
        ) WHERE dup_rank = 1
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE events_fact AS
        SELECT e.src_line, e.person, e.event_id, e.revision, e.day, e.kind,
               e.is_tombstone
        FROM events_deduped e
        JOIN (
          SELECT person, event_id, max(revision) AS revision
          FROM events_deduped GROUP BY person, event_id
        ) l USING (person, event_id, revision)
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE effective_events AS
        SELECT src_line, person, event_id, revision, day, kind
        FROM events_fact WHERE NOT is_tombstone
        """
    )


def _coverage_checks(
    con: duckdb.DuckDBPyConnection, plan: Plan, problems: IssueCollector
) -> list[tuple[str, str, str, str]]:
    staged: list[tuple[str, str, str, str]] = []
    for idx, src in enumerate(plan.sources):
        table = f"raw_cov_{idx}"
        if not _try_stage(con, problems, table, src.abs_path, ("person", "start_day", "end_day")):
            continue
        con.execute(
            f"""
            CREATE OR REPLACE TABLE {table}_coded AS
            SELECT src_line, person, start_day, end_day,
              CASE WHEN person IS NULL OR regexp_matches(person, '{_STR_FORBIDDEN_RE}')
                   THEN 'person_invalid' END AS c_person,
              CASE WHEN start_day IS NULL OR NOT regexp_full_match(start_day, '{_INT_RE}')
                   THEN 'start_day_invalid' END AS c_start,
              CASE WHEN end_day IS NULL OR NOT regexp_full_match(end_day, '{_INT_RE}')
                   THEN 'end_day_invalid' END AS c_end
            FROM {table}
            """
        )
        con.execute(
            f"""
            CREATE OR REPLACE TABLE {table}_flagged AS
            SELECT *,
              list_filter([c_person, c_start, c_end], lambda x: x IS NOT NULL) AS codes
            FROM {table}_coded
            """
        )
        bad = con.execute(
            f"SELECT src_line, person, start_day, end_day, codes FROM {table}_flagged "
            "WHERE len(codes) > 0 ORDER BY src_line"
        ).fetchall()
        for line_no, person, s, e, codes in bad:
            problems.add(
                Issue(
                    "coverage_row_invalid",
                    "覆盖行字段不合法: " + ", ".join(codes),
                    file=src.abs_path,
                    row=line_no,
                    details={
                        "source_id": src.source_id,
                        "person": person,
                        "start_day": s,
                        "end_day": e,
                        "codes": codes,
                    },
                )
            )
        if bad:
            continue
        typed = f"cov_typed_{idx}"
        con.execute(
            f"""
            CREATE OR REPLACE TABLE {typed} AS
            SELECT src_line, person,
                   TRY_CAST(start_day AS HUGEINT) AS start_day,
                   TRY_CAST(end_day AS HUGEINT) AS end_day,
                   start_day AS start_raw, end_day AS end_raw,
                   (TRY_CAST(start_day AS HUGEINT) IS NULL OR TRY_CAST(end_day AS HUGEINT) IS NULL)
                     AS overflow
            FROM {table}_flagged
            """
        )
        inverted = con.execute(
            f"SELECT src_line, CAST(start_day AS VARCHAR), CAST(end_day AS VARCHAR) "
            f"FROM {typed} WHERE start_day > end_day ORDER BY src_line"
        ).fetchall()
        for line_no, s, e in inverted:
            problems.add(
                Issue(
                    "coverage_inverted_interval",
                    "覆盖区间 start_day 大于 end_day；闭区间必须 start_day <= end_day",
                    file=src.abs_path,
                    row=line_no,
                    details={"source_id": src.source_id, "start_day": s, "end_day": e},
                )
            )
        for line_no, s, e in con.execute(
            f"SELECT src_line, start_raw, end_raw FROM {typed} "
            f"WHERE overflow OR abs(start_day) > {MAX_ABS_DAY} "
            f"OR abs(end_day) > {MAX_ABS_DAY} ORDER BY src_line"
        ).fetchall():
            problems.add(
                Issue(
                    "coverage_value_out_of_range",
                    f"覆盖日超出允许范围 ±{MAX_ABS_DAY}",
                    file=src.abs_path,
                    row=line_no,
                    details={"source_id": src.source_id, "start_day": s, "end_day": e},
                )
            )
        if inverted:
            continue
        staged.append((src.source_id, src.kind, typed, src.abs_path))
    return staged


def stage_package(con: duckdb.DuckDBPyConnection, plan: Plan) -> StagingStats:
    """Load and validate the event file and every declared coverage file."""

    stats = StagingStats(sources=[s.source_id for s in plan.sources])
    problems = IssueCollector()

    if _try_stage(con, problems, "raw_events", plan.events_abs_path, EVENT_COLUMNS):
        stats.raw_event_rows = con.execute("SELECT count(*) FROM raw_events").fetchone()[0]
        _event_checks(con, plan, problems)

    staged = _coverage_checks(con, plan, problems)
    problems.raise_if_any()

    con.execute(
        "CREATE OR REPLACE TABLE coverage_intervals "
        "(source_id VARCHAR, source_kind VARCHAR, person VARCHAR, "
        "start_day HUGEINT, end_day HUGEINT, src_line BIGINT, coverage_file VARCHAR)"
    )
    for source_id, kind, table, path in staged:
        con.execute(
            f"INSERT INTO coverage_intervals "
            f"SELECT ?, ?, person, start_day, end_day, src_line, ? FROM {table}",
            [source_id, kind, path],
        )

    stats.coverage_rows = con.execute("SELECT count(*) FROM coverage_intervals").fetchone()[0]
    stats.coverage_persons = con.execute(
        "SELECT count(DISTINCT person) FROM coverage_intervals"
    ).fetchone()[0]

    con.execute(
        """
        CREATE OR REPLACE TABLE persons AS
        SELECT DISTINCT person FROM (
          SELECT person FROM events_fact
          UNION
          SELECT person FROM coverage_intervals
        ) ORDER BY person
        """
    )

    # Merge overlapping and *adjacent* intervals per (source, person).  A gap
    # of one day is preserved: merging requires start_day <= prev_max_end + 1.
    con.execute(
        """
        CREATE OR REPLACE TABLE coverage_ordered AS
        SELECT *,
          max(end_day) OVER (
            PARTITION BY source_id, person ORDER BY start_day, end_day
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
          ) AS prev_max_end
        FROM coverage_intervals
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE coverage_grouped AS
        SELECT *,
          sum(CASE WHEN prev_max_end IS NULL OR start_day > prev_max_end + 1
                   THEN 1 ELSE 0 END) OVER (
            PARTITION BY source_id, person ORDER BY start_day, end_day
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
          ) AS grp
        FROM coverage_ordered
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE coverage_merged AS
        SELECT source_id, source_kind, person,
               min(start_day) AS seg_start, max(end_day) AS seg_end
        FROM coverage_grouped
        GROUP BY source_id, source_kind, person, grp
        """
    )

    stats.retraction_rows = con.execute(
        "SELECT count(*) FROM events_fact WHERE is_tombstone"
    ).fetchone()[0]
    stats.valid_event_rows = con.execute("SELECT count(*) FROM events_valid").fetchone()[0]
    stats.effective_event_rows = con.execute(
        "SELECT count(*) FROM effective_events"
    ).fetchone()[0]
    stats.event_counts = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT kind, count(*) FROM effective_events GROUP BY kind ORDER BY kind"
        ).fetchall()
    }

    per_source = {
        row[0]: row
        for row in con.execute(
            """
            SELECT c.source_id, any_value(c.source_kind), any_value(c.coverage_file),
                   count(*) AS rows, count(DISTINCT c.person) AS persons,
                   min(c.start_day) AS min_day, max(c.end_day) AS max_day
            FROM coverage_intervals c
            GROUP BY c.source_id
            """
        ).fetchall()
    }
    merged_counts = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT source_id, count(*) FROM coverage_merged GROUP BY source_id"
        ).fetchall()
    }
    for src in sorted(plan.sources, key=lambda s: s.source_id):
        row = per_source.get(src.source_id)
        if row is None:
            stats.source_stats.append(
                {
                    "source_id": src.source_id,
                    "kind": src.kind,
                    "coverage_file": src.abs_path,
                    "rows": 0,
                    "persons": 0,
                    "merged_segments": 0,
                    "min_day": None,
                    "max_day": None,
                }
            )
        else:
            stats.source_stats.append(
                {
                    "source_id": row[0],
                    "kind": row[1],
                    "coverage_file": row[2],
                    "rows": row[3],
                    "persons": row[4],
                    "merged_segments": merged_counts.get(row[0], 0),
                    "min_day": row[5],
                    "max_day": row[6],
                }
            )
    return stats
