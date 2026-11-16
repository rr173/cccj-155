"""Plan and source-registry parsing / validation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .errors import Issue

# Engine arithmetic uses DuckDB HUGEINT.  Keep inputs well inside its range so
# window arithmetic can never overflow.
MAX_ABS_DAY = 10**30

KINDS = ("encounter", "qualifying", "exclusion")
EVENT_COLUMNS = ("person", "event_id", "revision", "day", "kind")
# Retraction representation: a row whose latest revision carries neither a
# day nor a kind (or the optional `retracted` flag is true and carries no
# payload) is a full tombstone for that person+event_id.  A partially null
# row, or any bad value, is rejected instead of being guessed at.
SOURCE_KINDS = ("qualifying", "exclusion")


@dataclass(frozen=True)
class SourceDecl:
    source_id: str
    kind: str
    coverage_file: str
    abs_path: str


@dataclass
class Plan:
    plan_id: str
    lookback_days: int
    confirm_days: int
    events_file: str
    events_abs_path: str
    sources: list[SourceDecl]
    package_dir: str
    warnings: list[Issue] = field(default_factory=list)

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "lookback_days": self.lookback_days,
            "confirm_days": self.confirm_days,
            "events_file": self.events_file,
            "sources": [
                {
                    "source_id": s.source_id,
                    "kind": s.kind,
                    "coverage_file": s.coverage_file,
                }
                for s in self.sources
            ],
        }


def _resolve(base: str, raw: str) -> str:
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(base, raw))


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_plain_str(value: Any) -> bool:
    """No whitespace/control characters; keeps ids safe in CSV/JSON output."""
    return isinstance(value, str) and value != "" and not any(
        ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value
    )


def load_plan(path: str) -> tuple[Plan | None, list[Issue]]:
    """Load plan.json.  Returns (plan, issues); plan is None if unusable."""

    issues: list[Issue] = []
    plan_file = os.path.abspath(path)
    try:
        with open(plan_file, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return None, [Issue("plan_missing", "方案文件不存在", file=plan_file)]
    except (OSError, UnicodeDecodeError) as exc:
        return None, [Issue("plan_unreadable", f"方案文件无法读取: {exc}", file=plan_file)]
    except json.JSONDecodeError as exc:
        return None, [
            Issue(
                "plan_bad_json",
                f"方案文件不是合法 JSON: {exc.msg}",
                file=plan_file,
                row=exc.lineno,
            )
        ]

    if not isinstance(doc, dict):
        return None, [Issue("plan_not_object", "方案 JSON 顶层必须是对象", file=plan_file)]

    package_dir = os.path.dirname(plan_file)
    warnings: list[Issue] = []

    known_top = {
        "plan_id",
        "lookback_days",
        "confirm_days",
        "events_file",
        "sources",
        "description",
    }
    for key in sorted(set(doc) - known_top):
        warnings.append(
            Issue("plan_unknown_key", f"方案含未知键 {key!r}，已忽略", file=plan_file, field=key)
        )

    plan_id = doc.get("plan_id")
    if plan_id is None:
        plan_id = os.path.splitext(os.path.basename(plan_file))[0]
    elif not _nonempty_str(plan_id):
        issues.append(Issue("plan_bad_id", "plan_id 必须是非空字符串", file=plan_file, field="plan_id"))
        plan_id = "invalid"

    def _window(key: str) -> int | None:
        val = doc.get(key)
        if not isinstance(val, int) or isinstance(val, bool):
            issues.append(
                Issue(
                    "plan_bad_window",
                    f"{key} 必须是整数",
                    file=plan_file,
                    field=key,
                    value=repr(val),
                )
            )
            return None
        if val < 0:
            issues.append(
                Issue(
                    "plan_bad_window",
                    f"{key} 不能为负数",
                    file=plan_file,
                    field=key,
                    value=str(val),
                )
            )
            return None
        if val > MAX_ABS_DAY:
            issues.append(
                Issue(
                    "plan_window_too_large",
                    f"{key} 超过引擎安全上限 {MAX_ABS_DAY}",
                    file=plan_file,
                    field=key,
                    value=str(val),
                )
            )
            return None
        return val

    lookback = _window("lookback_days")
    confirm = _window("confirm_days")

    events_raw = doc.get("events_file")
    events_abs = None
    if not _nonempty_str(events_raw):
        issues.append(
            Issue("plan_bad_events_file", "events_file 必须是非空字符串", file=plan_file, field="events_file")
        )
    else:
        events_abs = _resolve(package_dir, events_raw)
        if not os.path.isfile(events_abs):
            issues.append(
                Issue(
                    "events_file_missing",
                    "事件文件不存在",
                    file=events_abs,
                    field="events_file",
                )
            )

    sources_raw = doc.get("sources")
    sources: list[SourceDecl] = []
    seen: dict[str, int] = {}
    if not isinstance(sources_raw, list) or not sources_raw:
        issues.append(
            Issue("plan_bad_sources", "sources 必须是非空数组", file=plan_file, field="sources")
        )
    else:
        for idx, src in enumerate(sources_raw):
            loc = f"sources[{idx}]"
            if not isinstance(src, dict):
                issues.append(
                    Issue("source_not_object", "来源声明必须是对象", file=plan_file, field=loc)
                )
                continue
            sid = src.get("source_id")
            kind = src.get("kind")
            cov = src.get("coverage_file")
            bad = False
            if not _is_plain_str(sid):
                issues.append(
                    Issue(
                        "source_bad_id",
                        "source_id 必须是非空且不含空白/控制字符的字符串",
                        file=plan_file,
                        field=f"{loc}.source_id",
                    )
                )
                bad = True
            elif sid in seen:
                issues.append(
                    Issue(
                        "source_duplicate_id",
                        f"source_id 重复: {sid}",
                        file=plan_file,
                        field=f"{loc}.source_id",
                        value=sid,
                    )
                )
                bad = True
            else:
                seen[sid] = idx
            if kind not in SOURCE_KINDS:
                issues.append(
                    Issue(
                        "source_bad_kind",
                        f"来源 kind 必须是 {SOURCE_KINDS} 之一",
                        file=plan_file,
                        field=f"{loc}.kind",
                        value=repr(kind),
                    )
                )
                bad = True
            if not _nonempty_str(cov):
                issues.append(
                    Issue(
                        "source_bad_coverage_file",
                        "coverage_file 必须是非空字符串",
                        file=plan_file,
                        field=f"{loc}.coverage_file",
                    )
                )
                bad = True
            if bad:
                continue
            cov_abs = _resolve(package_dir, cov)
            if not os.path.isfile(cov_abs):
                issues.append(
                    Issue(
                        "coverage_file_missing",
                        "覆盖文件不存在",
                        file=cov_abs,
                        field=f"{loc}.coverage_file",
                    )
                )
                continue
            for key in sorted(set(src) - {"source_id", "kind", "coverage_file"}):
                warnings.append(
                    Issue(
                        "source_unknown_key",
                        f"来源 {sid} 含未知键 {key!r}，已忽略",
                        file=plan_file,
                        field=f"{loc}.{key}",
                    )
                )
            sources.append(
                SourceDecl(
                    source_id=sid,
                    kind=kind,
                    coverage_file=cov,
                    abs_path=cov_abs,
                )
            )

    # Both kinds need at least one declared source: an empty source set would
    # make "absence" vacuously complete and silently turn unknown into false.
    for kind in SOURCE_KINDS:
        if not any(s.kind == kind for s in sources) and isinstance(sources_raw, list):
            issues.append(
                Issue(
                    "plan_no_sources_for_kind",
                    f"没有声明 {kind} 来源；不能用空来源集合推断“不存在”",
                    file=plan_file,
                    field="sources",
                )
            )

    if issues:
        return None, issues

    assert lookback is not None and confirm is not None and events_abs is not None
    plan = Plan(
        plan_id=plan_id,
        lookback_days=lookback,
        confirm_days=confirm,
        events_file=events_raw,
        events_abs_path=events_abs,
        sources=sources,
        package_dir=package_dir,
        warnings=warnings,
    )
    return plan, []
