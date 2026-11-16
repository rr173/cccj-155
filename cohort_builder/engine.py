"""Core cohort derivation in DuckDB.

Everything is expressed as interval operations and ordered scans:

* window event counts come from event ranks joined with ASOF (a sorted merge),
  never from enumerating days;
* coverage completeness comes from maximally merged intervals; per candidate
  window and declared source only a handful of ASOF boundary lookups plus
  prefix lengths are needed, so a window of length 10^9 costs the same as one
  of length 3;
* covered pieces and complement gaps are materialized by exploding a *rank*
  range (hash join on rank), never a day range;
* the same rank trick recovers the actual events inside a window as
  explanation evidence.

Work therefore scales with (#events + #candidates x #sources + #segments +
#gaps), not with the numeric length of the windows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import duckdb

from .config import Plan
from .ingest import StagingStats, stage_package

# Sentinels for open-ended complement intervals; no real day may be this far out.
SENTINEL = 10**35
# Caps on *listing* detail in explanations.  All totals remain exact; the rows
# carry explicit truncation flags when the cap is hit.
EVIDENCE_CAP = 200
PIECE_CAP = 200
GAP_CAP = 200
# How many candidates are inlined in each person_explain row.
PERSON_CANDIDATE_CAP = 400


@dataclass
class BuildStats:
    persons: int = 0
    indexed: int = 0
    pending: int = 0
    no_index: int = 0
    no_candidate: int = 0
    candidates: int = 0
    pending_gap_rows: int = 0
    staging: StagingStats | None = None
    timings: dict[str, float] = field(default_factory=dict)


def _exec(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None):
    return con.execute(sql, params or [])


def derive_tables(
    con: duckdb.DuckDBPyConnection,
    plan: Plan,
    stats: BuildStats,
) -> None:
    w = plan.lookback_days
    f = plan.confirm_days

    # ------------------------------------------------------------------
    # Source registry
    # ------------------------------------------------------------------
    con.execute(
        "CREATE OR REPLACE TABLE declared_sources "
        "(source_id VARCHAR, source_kind VARCHAR, coverage_file VARCHAR, ord INTEGER)"
    )
    for idx, src in enumerate(sorted(plan.sources, key=lambda s: (s.kind, s.source_id))):
        con.execute(
            "INSERT INTO declared_sources VALUES (?, ?, ?, ?)",
            [src.source_id, src.kind, src.abs_path, idx],
        )

    # ------------------------------------------------------------------
    # Candidates: distinct encounter days of effective (non-retracted) facts.
    # ------------------------------------------------------------------
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE candidates AS
        SELECT DISTINCT person, day AS cand_day
        FROM effective_events
        WHERE kind = 'encounter'
        ORDER BY person, cand_day
        """,
    )
    stats.candidates = con.execute("SELECT count(*) FROM candidates").fetchone()[0]

    _exec(
        con,
        f"""
        CREATE OR REPLACE TABLE cand_windows AS
        SELECT person, cand_day,
               cand_day - {w} AS lb_start,
               cand_day - 1   AS lb_end,
               cand_day       AS cf_start,
               cand_day + {f} AS cf_end,
               ({w} = 0)      AS lb_empty
        FROM candidates
        """,
    )

    # ------------------------------------------------------------------
    # Ranked effective events: one row per surviving fact, ranked densely in
    # business-day order per person+kind.  A window then maps to a rank range
    # and its exact count is a rank difference.
    # ------------------------------------------------------------------
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE ranked_events AS
        SELECT person, kind, day, event_id, revision, src_line,
               row_number() OVER (
                 PARTITION BY person, kind ORDER BY day, event_id, revision
               ) AS erank
        FROM effective_events
        WHERE kind IN ('exclusion', 'qualifying')
        """,
    )
    for kind, table in (("exclusion", "excl_prefix"), ("qualifying", "qual_prefix")):
        # One row per day keeps ASOF keys unique; max rank equals the prefix
        # count through that day.
        _exec(
            con,
            f"""
            CREATE OR REPLACE TABLE {table} AS
            SELECT person, day, max(erank) AS erank
            FROM ranked_events WHERE kind = '{kind}'
            GROUP BY person, day
            """,
        )

    # ------------------------------------------------------------------
    # Boundary ranks.  ASOF yields the prefix rank at each window endpoint.
    # ------------------------------------------------------------------
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE cand_bounds AS
        SELECT cw.person, cw.cand_day,
               cw.lb_start, cw.lb_end, cw.cf_start, cw.cf_end, cw.lb_empty,
               CASE WHEN cw.lb_empty THEN 0 ELSE coalesce(xh.erank, 0) END AS x_rank_hi,
               CASE WHEN cw.lb_empty THEN 0 ELSE coalesce(xl.erank, 0) END AS x_rank_lo,
               coalesce(qh.erank, 0) AS q_rank_hi,
               coalesce(ql.erank, 0) AS q_rank_lo
        FROM cand_windows cw
        ASOF LEFT JOIN excl_prefix xh
          ON cw.person = xh.person AND cw.lb_end >= xh.day
        ASOF LEFT JOIN excl_prefix xl
          ON cw.person = xl.person AND cw.lb_start > xl.day
        ASOF LEFT JOIN qual_prefix qh
          ON cw.person = qh.person AND cw.cf_end >= qh.day
        ASOF LEFT JOIN qual_prefix ql
          ON cw.person = ql.person AND cw.cf_start > ql.day
        ORDER BY cw.person, cw.cand_day
        """,
    )
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE cand_counts AS
        SELECT person, cand_day, lb_start, lb_end, cf_start, cf_end, lb_empty,
               x_rank_hi, x_rank_lo, q_rank_hi, q_rank_lo,
               x_rank_hi - x_rank_lo AS exclusion_count,
               q_rank_hi - q_rank_lo AS qualifying_count
        FROM cand_bounds
        """,
    )

    # ------------------------------------------------------------------
    # Ranked coverage segments and complement gaps (per source and person).
    # Segments are disjoint; a one-day gap is never merged into a segment.
    # ------------------------------------------------------------------
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE seg_ranked AS
        SELECT source_id, source_kind, person, seg_start, seg_end,
               seg_end - seg_start + 1 AS seg_days,
               row_number() OVER (
                 PARTITION BY source_id, person ORDER BY seg_start
               ) AS srank,
               sum(seg_end - seg_start + 1) OVER (
                 PARTITION BY source_id, person ORDER BY seg_start
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS cum_days
        FROM coverage_merged
        """,
    )
    _exec(
        con,
        f"""
        CREATE OR REPLACE TABLE coverage_gaps AS
        WITH ordered AS (
          SELECT source_id, source_kind, person, seg_start, seg_end,
                 lag(seg_end) OVER (
                   PARTITION BY source_id, person ORDER BY seg_start
                 ) AS prev_end
          FROM coverage_merged
        )
        SELECT source_id, source_kind, person,
               coalesce(prev_end + 1, {-SENTINEL}) AS gap_start,
               seg_start - 1 AS gap_end
        FROM ordered
        WHERE prev_end IS NULL OR seg_start > prev_end + 1
        UNION ALL
        SELECT source_id, source_kind, person, max(seg_end) + 1, {SENTINEL}
        FROM coverage_merged
        GROUP BY source_id, source_kind, person
        HAVING max(seg_end) < {SENTINEL}
        """,
    )
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE gap_ranked AS
        SELECT source_id, source_kind, person, gap_start, gap_end,
               gap_end - gap_start + 1 AS gap_days_raw,
               row_number() OVER (
                 PARTITION BY source_id, person ORDER BY gap_start
               ) AS grank
        FROM coverage_gaps
        """,
    )

    # ------------------------------------------------------------------
    # One row per candidate window x declared source of the relevant kind.
    # All coverage metrics are derived from boundary ranks, so no day is
    # enumerated and window length is irrelevant to cost.
    #
    # Segment prefix coverage through day D (per person+source) is
    #   cum_before(seg) + least(D, seg_end) - seg_start + 1
    # for the last segment with seg_start <= D, and 0 when none exists.
    # Two ASOF lookups (at win_end and win_start-1) give exact covered days.
    # ------------------------------------------------------------------
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE window_sources AS
        SELECT cw.person, cw.cand_day, 'lookback' AS window_name,
               cw.lb_start AS win_start, cw.lb_end AS win_end,
               cw.lb_empty AS win_empty, ds.source_id, ds.source_kind
        FROM cand_windows cw
        CROSS JOIN declared_sources ds
        WHERE ds.source_kind = 'exclusion'
        UNION ALL
        SELECT cw.person, cw.cand_day, 'confirm' AS window_name,
               cw.cf_start AS win_start, cw.cf_end AS win_end,
               FALSE AS win_empty, ds.source_id, ds.source_kind
        FROM cand_windows cw
        CROSS JOIN declared_sources ds
        WHERE ds.source_kind = 'qualifying'
        """,
    )

    _exec(
        con,
        """
        CREATE OR REPLACE TABLE seg_stats AS
        SELECT source_id, source_kind, person, srank,
               seg_start, seg_end,
               cum_days - (seg_end - seg_start + 1) AS cum_before
        FROM seg_ranked
        """,
    )

    _exec(
        con,
        """
        CREATE OR REPLACE TABLE window_source_bounds AS
        SELECT ws.person, ws.cand_day, ws.window_name, ws.win_start, ws.win_end,
               ws.win_empty, ws.source_id, ws.source_kind,
               hi.srank   AS s_hi_rank,
               hi.seg_start AS s_hi_start,
               hi.seg_end   AS s_hi_end,
               hi.cum_before AS s_hi_cum_before,
               lo.srank   AS s_lo_rank,
               lo.seg_start AS s_lo_start,
               lo.seg_end   AS s_lo_end,
               lo.cum_before AS s_lo_cum_before,
               ghi.grank  AS g_hi_rank,
               ghi.gap_start AS g_hi_start,
               ghi.gap_end   AS g_hi_end,
               glo.grank  AS g_lo_rank,
               glo.gap_start AS g_lo_start,
               glo.gap_end   AS g_lo_end
        FROM window_sources ws
        ASOF LEFT JOIN seg_stats hi
          ON ws.person = hi.person AND ws.source_id = hi.source_id
         AND ws.win_end >= hi.seg_start
        ASOF LEFT JOIN seg_stats lo
          ON ws.person = lo.person AND ws.source_id = lo.source_id
         AND ws.win_start - 1 >= lo.seg_start
        ASOF LEFT JOIN gap_ranked ghi
          ON ws.person = ghi.person AND ws.source_id = ghi.source_id
         AND ws.win_end >= ghi.gap_start
        ASOF LEFT JOIN gap_ranked glo
          ON ws.person = glo.person AND ws.source_id = glo.source_id
         AND ws.win_start - 1 >= glo.gap_start
        """,
    )

    _exec(
        con,
        """
        CREATE OR REPLACE TABLE window_source_first AS
        SELECT *,
               coalesce(s_lo_rank, 0)
                 + CASE WHEN s_lo_end >= win_start THEN 0 ELSE 1 END AS s_first_rank,
               coalesce(g_lo_rank, 0)
                 + CASE WHEN g_lo_end >= win_start THEN 0 ELSE 1 END AS g_first_rank,
               (win_empty OR (g_hi_rank IS NULL AND s_hi_rank IS NOT NULL
                  AND s_hi_start <= win_start AND s_hi_end >= win_end))
                 AS complete
        FROM window_source_bounds
        """,
    )

    _exec(
        con,
        """
        CREATE OR REPLACE TABLE window_source_metrics AS
        SELECT *,
          CASE WHEN win_empty THEN 0
               ELSE coalesce(s_hi_cum_before
                     + least(win_end, s_hi_end) - s_hi_start + 1, 0)
                    - coalesce(s_lo_cum_before
                     + least(win_start - 1, s_lo_end) - s_lo_start + 1, 0)
          END AS covered_days,
          CASE WHEN win_empty THEN 0 ELSE win_end - win_start + 1 END AS window_days,
          CASE WHEN win_empty OR s_hi_rank IS NULL THEN 0
               ELSE greatest(0, s_hi_rank - s_first_rank + 1) END AS piece_count,
          CASE
            WHEN win_empty THEN 0
            WHEN g_hi_rank IS NULL THEN 1  -- source has no segments at all
            ELSE greatest(0, g_hi_rank - g_first_rank + 1)
          END AS gap_count,
          CAST(%d AS BIGINT) AS piece_cap,
          CAST(%d AS BIGINT) AS gap_cap
        FROM window_source_first
        """ % (PIECE_CAP, GAP_CAP),
    )
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE window_source_final AS
        SELECT person, cand_day, window_name, win_start, win_end, win_empty,
               source_id, source_kind,
               piece_count, gap_count, covered_days, window_days,
               window_days - covered_days AS gap_days,
               (gap_count = 0) AS complete,
               (piece_count > piece_cap) AS pieces_truncated,
               (gap_count > gap_cap) AS gaps_truncated
        FROM window_source_metrics
        """,
    )

    _exec(
        con,
        """
        CREATE OR REPLACE TABLE window_completeness AS
        SELECT person, cand_day, window_name,
               bool_and(complete) AS complete,
               count(*) AS source_count,
               sum(CASE WHEN complete THEN 0 ELSE 1 END) AS incomplete_sources,
               sum(gap_days) AS gap_days
        FROM window_source_final
        GROUP BY person, cand_day, window_name
        """,
    )

    # ------------------------------------------------------------------
    # Three-valued decision.
    #   lookback: any exclusion -> false; none + incomplete -> unknown; else true
    #   confirm : any qualifying -> true; none + complete -> false; else unknown
    #   conjunction: false if either false; true only if both true.
    # ------------------------------------------------------------------
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE candidate_decisions AS
        SELECT cc.person, cc.cand_day,
               cc.lb_start AS lookback_start, cc.lb_end AS lookback_end,
               cc.cf_start AS confirm_start,  cc.cf_end AS confirm_end,
               cc.exclusion_count, cc.x_rank_lo, cc.x_rank_hi,
               cc.qualifying_count, cc.q_rank_lo, cc.q_rank_hi,
               coalesce(lb.complete, TRUE)  AS lookback_complete,
               coalesce(cf.complete, TRUE)  AS confirm_complete,
               coalesce(lb.incomplete_sources, 0) AS incomplete_lookback_sources,
               coalesce(cf.incomplete_sources, 0) AS incomplete_confirm_sources,
               CASE
                 WHEN cc.exclusion_count > 0 THEN 'false'
                 WHEN cc.qualifying_count = 0 AND coalesce(cf.complete, TRUE) THEN 'false'
                 WHEN cc.qualifying_count = 0 THEN 'unknown'
                 WHEN coalesce(lb.complete, TRUE) THEN 'true'
                 ELSE 'unknown'
               END AS decision,
               CASE
                 WHEN cc.exclusion_count > 0 THEN 'exclusion_in_lookback'
                 WHEN cc.qualifying_count = 0 AND coalesce(cf.complete, TRUE)
                   THEN 'no_qualifying_in_complete_confirm'
                 WHEN cc.qualifying_count = 0 THEN 'confirm_coverage_incomplete'
                 WHEN NOT coalesce(lb.complete, TRUE)
                   THEN 'lookback_coverage_incomplete'
                 ELSE 'qualifying_in_confirm_and_lookback_clear'
               END AS primary_reason
        FROM cand_counts cc
        LEFT JOIN window_completeness lb
          ON lb.person = cc.person AND lb.cand_day = cc.cand_day
         AND lb.window_name = 'lookback'
        LEFT JOIN window_completeness cf
          ON cf.person = cc.person AND cf.cand_day = cc.cand_day
         AND cf.window_name = 'confirm'
        ORDER BY cc.person, cc.cand_day
        """,
    )

    # ------------------------------------------------------------------
    # Person-level status: the first candidate in day order that is not
    # false decides.  true -> indexed.  unknown -> pending (never skipped).
    # ------------------------------------------------------------------
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE person_summary AS
        SELECT p.person,
               count(c.cand_day) AS candidate_count,
               min(c.cand_day) AS first_candidate_day,
               min(c.cand_day) FILTER (WHERE c.decision <> 'false') AS first_nonfalse_day,
               count(*) FILTER (WHERE c.decision = 'true') AS true_candidates,
               count(*) FILTER (WHERE c.decision = 'unknown') AS unknown_candidates,
               count(*) FILTER (WHERE c.decision = 'false') AS false_candidates
        FROM persons p
        LEFT JOIN candidate_decisions c ON c.person = p.person
        GROUP BY p.person
        """,
    )
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE person_status AS
        SELECT ps.person,
               CASE
                 WHEN ps.candidate_count = 0 THEN 'no_candidate'
                 WHEN ps.first_nonfalse_day IS NULL THEN 'no_index'
                 WHEN fd.decision = 'true' THEN 'indexed'
                 ELSE 'pending'
               END AS status,
               CASE WHEN fd.decision = 'true' THEN ps.first_nonfalse_day END AS index_day,
               CASE WHEN fd.decision = 'unknown' THEN ps.first_nonfalse_day END
                    AS earliest_pending_day,
               ps.candidate_count, ps.first_candidate_day, ps.first_nonfalse_day,
               fd.decision AS first_nonfalse_decision,
               ps.true_candidates, ps.unknown_candidates, ps.false_candidates
        FROM person_summary ps
        LEFT JOIN candidate_decisions fd
          ON fd.person = ps.person AND fd.cand_day = ps.first_nonfalse_day
        ORDER BY ps.person
        """,
    )
    _exec(
        con,
        """
        CREATE OR REPLACE TABLE later_true AS
        SELECT st.person,
               to_json(list(c.cand_day ORDER BY c.cand_day)) AS later_true_days_json,
               count(*) AS later_true_count,
               min(c.cand_day) AS first_later_true_day,
               max(c.cand_day) AS last_later_true_day
        FROM person_status st
        JOIN candidate_decisions c
          ON c.person = st.person
         AND c.cand_day > st.first_nonfalse_day
         AND c.decision = 'true'
        WHERE st.status = 'pending'
        GROUP BY st.person
        """,
    )

    # ------------------------------------------------------------------
    # Explanation evidence: actual events inside each window, recovered from
    # the rank range.  Capped per window; the exact total is always given.
    # ------------------------------------------------------------------
    for kind, lo_col, hi_col, cnt_col, table in (
        ("exclusion", "x_rank_lo", "x_rank_hi", "exclusion_count", "exclusion_evidence"),
        ("qualifying", "q_rank_lo", "q_rank_hi", "qualifying_count", "qualifying_evidence"),
    ):
        con.execute(
            f"""
            CREATE OR REPLACE TABLE {table} AS
            SELECT cb.person, cb.cand_day, re.kind, re.day, re.event_id, re.revision,
                   re.src_line, re.erank, (cb.{cnt_col} > {EVIDENCE_CAP}) AS truncated
            FROM cand_counts cb
            CROSS JOIN generate_series(
                   CAST(cb.{lo_col} + 1 AS BIGINT),
                   CAST(least(cb.{hi_col}, cb.{lo_col} + {EVIDENCE_CAP}) AS BIGINT)
                 ) g(r)
            JOIN ranked_events re
              ON re.person = cb.person AND re.kind = '{kind}' AND re.erank = g.r
            """
        )
    con.execute(
        """
        CREATE OR REPLACE TABLE candidate_evidence AS
        SELECT person, cand_day, kind, day, event_id, revision, src_line, erank, truncated
        FROM exclusion_evidence
        UNION ALL
        SELECT person, cand_day, kind, day, event_id, revision, src_line, erank, truncated
        FROM qualifying_evidence
        """
    )

    # Pieces and gaps, materialized from rank ranges and clipped to windows.
    # Covered pieces.  A fully covered window has exactly one piece (the whole
    # window), so it is emitted directly instead of exploding its segments;
    # only incomplete windows need the (capped) segment list.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE piece_rows AS
        SELECT b.person, b.cand_day, b.window_name, b.source_id, b.source_kind,
               b.win_start AS piece_start, b.win_end AS piece_end
        FROM window_source_final b
        WHERE b.complete
        UNION ALL
        SELECT b.person, b.cand_day, b.window_name, b.source_id, b.source_kind,
               greatest(s.seg_start, b.win_start) AS piece_start,
               least(s.seg_end, b.win_end)        AS piece_end
        FROM window_source_final f
        JOIN window_source_first b
          ON b.person = f.person AND b.cand_day = f.cand_day
         AND b.window_name = f.window_name AND b.source_id = f.source_id
        CROSS JOIN generate_series(
               CAST(b.s_first_rank AS BIGINT),
               CAST(least(b.s_hi_rank, b.s_first_rank + {PIECE_CAP} - 1) AS BIGINT)) g(r)
        JOIN seg_ranked s
          ON s.person = b.person AND s.source_id = b.source_id AND s.srank = g.r
        WHERE NOT f.win_empty AND NOT f.complete AND b.s_hi_rank IS NOT NULL
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE gap_rows AS
        SELECT b.person, b.cand_day, b.window_name, b.source_id, b.source_kind,
               greatest(gp.gap_start, b.win_start) AS gap_start,
               least(gp.gap_end, b.win_end)        AS gap_end
        FROM window_source_final f
        JOIN window_source_first b
          ON b.person = f.person AND b.cand_day = f.cand_day
         AND b.window_name = f.window_name AND b.source_id = f.source_id
        CROSS JOIN generate_series(
               CAST(b.g_first_rank AS BIGINT),
               CAST(least(b.g_hi_rank, b.g_first_rank + {GAP_CAP} - 1) AS BIGINT)) g(r)
        JOIN gap_ranked gp
          ON gp.person = b.person AND gp.source_id = b.source_id AND gp.grank = g.r
        WHERE NOT f.win_empty AND NOT f.complete
        UNION ALL
        SELECT b.person, b.cand_day, b.window_name, b.source_id, b.source_kind,
               b.win_start AS gap_start, b.win_end AS gap_end
        FROM window_source_final f
        JOIN window_source_first b
          ON b.person = f.person AND b.cand_day = f.cand_day
         AND b.window_name = f.window_name AND b.source_id = f.source_id
        WHERE NOT f.win_empty AND NOT f.complete AND b.g_hi_rank IS NULL
        """
    )

    # ------------------------------------------------------------------
    # JSON assembly (DuckDB forbids nested aggregates, so each list is a
    # separate pre-aggregated table).
    # ------------------------------------------------------------------
    con.execute(
        """
        CREATE OR REPLACE TABLE evidence_json AS
        SELECT person, cand_day, kind,
               to_json(list(struct_pack(
                 day := day, event_id := event_id, revision := revision,
                 input_line := src_line
               ) ORDER BY day, event_id, revision)) AS events_json,
               count(*) AS shown,
               bool_or(truncated) AS truncated
        FROM candidate_evidence
        GROUP BY person, cand_day, kind
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE pieces_json AS
        SELECT person, cand_day, window_name, source_id,
               to_json(list(struct_pack(start := piece_start, stop := piece_end)
                            ORDER BY piece_start)) AS pieces_json
        FROM piece_rows
        GROUP BY person, cand_day, window_name, source_id
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE gaps_json AS
        SELECT person, cand_day, window_name, source_id,
               to_json(list(struct_pack(start := gap_start, stop := gap_end)
                            ORDER BY gap_start)) AS gaps_json
        FROM gap_rows
        GROUP BY person, cand_day, window_name, source_id
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE window_source_detail AS
        SELECT f.person, f.cand_day, f.window_name,
               to_json(list(struct_pack(
                 source_id := f.source_id,
                 kind := f.source_kind,
                 complete := f.complete,
                 window_days := f.window_days,
                 covered_days := f.covered_days,
                 gap_days := f.gap_days,
                 gap_count := f.gap_count,
                 covered := json(coalesce(p.pieces_json, '[]')),
                 gaps := json(coalesce(g.gaps_json, '[]')),
                 pieces_truncated := f.pieces_truncated,
                 gaps_truncated := f.gaps_truncated
               ) ORDER BY f.source_id)) AS sources_json
        FROM window_source_final f
        LEFT JOIN pieces_json p
          ON p.person = f.person AND p.cand_day = f.cand_day
         AND p.window_name = f.window_name AND p.source_id = f.source_id
        LEFT JOIN gaps_json g
          ON g.person = f.person AND g.cand_day = f.cand_day
         AND g.window_name = f.window_name AND g.source_id = f.source_id
        GROUP BY f.person, f.cand_day, f.window_name
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE candidate_explain AS
        SELECT d.person, d.cand_day,
               d.lookback_start, d.lookback_end, d.confirm_start, d.confirm_end,
               d.exclusion_count, d.qualifying_count,
               d.lookback_complete, d.confirm_complete,
               d.incomplete_lookback_sources, d.incomplete_confirm_sources,
               d.decision, d.primary_reason,
               to_json(struct_pack(
                 person := d.person,
                 candidate_day := d.cand_day,
                 decision := d.decision,
                 primary_reason := d.primary_reason,
                 lookback := struct_pack(
                   start_inclusive := d.lookback_start,
                   end_exclusive := d.lookback_end + 1,
                   empty := cw.lb_empty,
                   exclusion_count := d.exclusion_count,
                   exclusions := json(coalesce(xe.events_json, '[]')),
                   exclusions_truncated := coalesce(xe.truncated, FALSE),
                   coverage_complete := d.lookback_complete,
                   incomplete_sources := d.incomplete_lookback_sources,
                   sources := json(coalesce(lb.sources_json, '[]'))
                 ),
                 confirm := struct_pack(
                   start_inclusive := d.confirm_start,
                   end_inclusive := d.confirm_end,
                   qualifying_count := d.qualifying_count,
                   qualifiers := json(coalesce(qe.events_json, '[]')),
                   qualifiers_truncated := coalesce(qe.truncated, FALSE),
                   coverage_complete := d.confirm_complete,
                   incomplete_sources := d.incomplete_confirm_sources,
                   sources := json(coalesce(cf.sources_json, '[]'))
                 )
               )) AS explanation_json
        FROM candidate_decisions d
        JOIN cand_windows cw ON cw.person = d.person AND cw.cand_day = d.cand_day
        LEFT JOIN window_source_detail lb
          ON lb.person = d.person AND lb.cand_day = d.cand_day
         AND lb.window_name = 'lookback'
        LEFT JOIN window_source_detail cf
          ON cf.person = d.person AND cf.cand_day = d.cand_day
         AND cf.window_name = 'confirm'
        LEFT JOIN evidence_json xe
          ON xe.person = d.person AND xe.cand_day = d.cand_day AND xe.kind = 'exclusion'
        LEFT JOIN evidence_json qe
          ON qe.person = d.person AND qe.cand_day = d.cand_day AND qe.kind = 'qualifying'
        ORDER BY d.person, d.cand_day
        """
    )

    # ------------------------------------------------------------------
    # Per-person explanation: status, earliest pending day, later known-true
    # candidates and the full candidate list.
    # ------------------------------------------------------------------
    # The candidate list inside person_explain is a compact index only: the
    # full per-window evidence for every candidate lives in
    # explain_candidates.csv (one row per candidate) and is never duplicated
    # here.  This keeps the per-person bundle small no matter how many
    # candidates a person has.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE person_candidates_json AS
        SELECT person,
               to_json(list(struct_pack(
                 day := cand_day,
                 decision := decision,
                 reason := primary_reason
               ) ORDER BY cand_day)
               FILTER (WHERE rn <= {PERSON_CANDIDATE_CAP})) AS candidates_json,
               max(rn) AS candidate_count,
               (max(rn) > {PERSON_CANDIDATE_CAP}) AS candidates_truncated,
               least(max(rn), {PERSON_CANDIDATE_CAP}) AS candidates_inlined
        FROM (
          SELECT c.person, c.cand_day, c.decision, c.primary_reason,
                 row_number() OVER (PARTITION BY c.person ORDER BY c.cand_day) AS rn
          FROM candidate_decisions c
        )
        GROUP BY person
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE person_explain AS
        SELECT st.person,
               st.status,
               st.index_day,
               st.earliest_pending_day,
               st.candidate_count,
               st.first_candidate_day,
               st.first_nonfalse_day,
               st.first_nonfalse_decision,
               coalesce(lt.later_true_count, 0) AS later_true_count,
               to_json(struct_pack(
                 person := st.person,
                 status := st.status,
                 index_day := st.index_day,
                 earliest_pending_day := st.earliest_pending_day,
                 first_nonfalse_candidate := st.first_nonfalse_day,
                 first_nonfalse_decision := st.first_nonfalse_decision,
                 later_true_days := json(coalesce(lt.later_true_days_json, '[]')),
                 candidate_count := st.candidate_count,
                 candidates_inlined := coalesce(pc.candidates_inlined, 0),
                 candidates_truncated := coalesce(pc.candidates_truncated, FALSE),
                 candidates := json(coalesce(pc.candidates_json, '[]'))
               )) AS person_json
        FROM person_status st
        LEFT JOIN later_true lt ON lt.person = st.person
        LEFT JOIN person_candidates_json pc ON pc.person = st.person
        ORDER BY st.person
        """
    )

    # ------------------------------------------------------------------
    # Pending export: concrete source/date gaps on the earliest pending
    # candidate window that is incomplete.
    # ------------------------------------------------------------------
    con.execute(
        """
        CREATE OR REPLACE TABLE pending_detail AS
        SELECT st.person, st.earliest_pending_day,
               g.window_name, g.source_id, g.source_kind,
               g.gap_start, g.gap_end, g.gap_end - g.gap_start + 1 AS missing_days,
               (SELECT gap_days FROM window_completeness wc
                 WHERE wc.person = g.person AND wc.cand_day = g.cand_day
                   AND wc.window_name = g.window_name) AS window_gap_days,
               lt.later_true_days_json
        FROM person_status st
        JOIN gap_rows g
          ON g.person = st.person AND g.cand_day = st.earliest_pending_day
        LEFT JOIN later_true lt ON lt.person = st.person
        WHERE st.status = 'pending'
        ORDER BY st.person, g.window_name, g.source_id, g.gap_start
        """
    )

    stats.persons = con.execute("SELECT count(*) FROM persons").fetchone()[0]
    for status, attr in (
        ("indexed", "indexed"),
        ("pending", "pending"),
        ("no_index", "no_index"),
        ("no_candidate", "no_candidate"),
    ):
        setattr(
            stats,
            attr,
            con.execute(
                "SELECT count(*) FROM person_status WHERE status = ?", [status]
            ).fetchone()[0],
        )
    stats.pending_gap_rows = con.execute("SELECT count(*) FROM pending_detail").fetchone()[0]


def build(con: duckdb.DuckDBPyConnection, plan: Plan) -> BuildStats:
    stats = BuildStats()
    t0 = time.perf_counter()
    stats.staging = stage_package(con, plan)
    t1 = time.perf_counter()
    derive_tables(con, plan, stats)
    t2 = time.perf_counter()
    stats.timings = {
        "stage_seconds": round(t1 - t0, 3),
        "derive_seconds": round(t2 - t1, 3),
    }
    return stats
