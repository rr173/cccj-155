"""Independent reference implementation over a small integer day domain.

Deliberately naive: enumerates every day of every window and computes the
three-valued decision by set membership.  Used only by tests to validate the
DuckDB interval/rank engine.  It shares no code with the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RefResult:
    status: str
    index_day: int | None
    earliest_pending_day: int | None
    later_true: list[int]
    candidates: dict[int, dict] = field(default_factory=dict)


def reference_build(
    events: list[dict],
    coverage: dict[tuple[str, str], list[tuple[int, int]]],
    sources: dict[str, str],
    W: int,
    F: int,
    persons: list[str],
) -> dict[str, RefResult]:
    """events: dicts with person/event_id/revision/day/kind (day None = tombstone).
    coverage: (source_id, person) -> list of inclusive (start, end).
    sources: source_id -> 'exclusion' | 'qualifying'.
    """

    # Resolve revisions: highest revision per (person, event_id); retraction
    # = tombstone at that revision.
    latest: dict[tuple[str, str], dict] = {}
    for ev in events:
        key = (ev["person"], ev["event_id"])
        cur = latest.get(key)
        if cur is None or ev["revision"] > cur["revision"]:
            latest[key] = ev
        elif ev["revision"] == cur["revision"]:
            # identical duplicate rows are fine; different facts are a conflict
            a = (cur["day"], cur["kind"])
            b = (ev["day"], ev["kind"])
            assert a == b, "identity conflict should have been rejected"

    facts = [e for e in latest.values() if e["day"] is not None]

    def days_covered(source_id: str, person: str) -> set[int]:
        out: set[int] = set()
        for s, e in coverage.get((source_id, person), []):
            out.update(range(s, e + 1))
        return out

    excl_sources = [s for s, k in sources.items() if k == "exclusion"]
    qual_sources = [s for s, k in sources.items() if k == "qualifying"]

    results: dict[str, RefResult] = {}
    for person in sorted(persons):
        enc = sorted({e["day"] for e in facts if e["person"] == person and e["kind"] == "encounter"})
        excl_days = {e["day"] for e in facts if e["person"] == person and e["kind"] == "exclusion"}
        qual_days = {e["day"] for e in facts if e["person"] == person and e["kind"] == "qualifying"}

        cov_excl = {s: days_covered(s, person) for s in excl_sources}
        cov_qual = {s: days_covered(s, person) for s in qual_sources}

        cands: dict[int, dict] = {}
        for d in enc:
            lb_days = set(range(d - W, d))
            cf_days = set(range(d, d + F + 1))

            found_excl = lb_days & excl_days
            if found_excl:
                lookback = "false"
            else:
                complete = all(all(day in cov_excl[s] for day in lb_days) for s in excl_sources)
                lookback = "true" if complete else "unknown"

            found_qual = cf_days & qual_days
            if found_qual:
                confirm = "true"
            else:
                complete = all(all(day in cov_qual[s] for day in cf_days) for s in qual_sources)
                confirm = "false" if complete else "unknown"

            if lookback == "false" or confirm == "false":
                decision = "false"
            elif lookback == "true" and confirm == "true":
                decision = "true"
            else:
                decision = "unknown"

            cands[d] = {
                "decision": decision,
                "exclusions": sorted(found_excl),
                "qualifiers": sorted(found_qual),
                "lookback": lookback,
                "confirm": confirm,
            }

        first_nonfalse = None
        for d in sorted(cands):
            if cands[d]["decision"] != "false":
                first_nonfalse = d
                break
        if first_nonfalse is None:
            status = "no_index" if cands else "no_candidate"
            index_day = None
            pending_day = None
            later: list[int] = []
        elif cands[first_nonfalse]["decision"] == "true":
            status = "indexed"
            index_day = first_nonfalse
            pending_day = None
            later = []
        else:
            status = "pending"
            index_day = None
            pending_day = first_nonfalse
            later = [d for d in sorted(cands) if d > first_nonfalse and cands[d]["decision"] == "true"]

        results[person] = RefResult(
            status=status,
            index_day=index_day,
            earliest_pending_day=pending_day,
            later_true=later,
            candidates=cands,
        )
    return results
