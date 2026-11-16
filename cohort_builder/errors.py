"""Structured input-rejection errors.

A malformed plan, malformed coverage/source declaration, malformed event row or
an event-identity conflict rejects the whole run.  "Insufficient history" is not
an error: it is represented as an ``unknown`` candidate and a ``pending`` person.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

MAX_ISSUES_REPORTED = 200


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    file: str | None = None
    row: int | None = None
    field: str | None = None
    value: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.file is not None:
            out["file"] = self.file
        if self.row is not None:
            out["row"] = self.row
        if self.field is not None:
            out["field"] = self.field
        if self.value is not None:
            out["value"] = self.value
        if self.details:
            out["details"] = self.details
        return out


class InputRejected(Exception):
    """Raised when the input package cannot be accepted as a whole."""

    def __init__(self, issues: Iterable[Issue], total: int | None = None):
        issues = list(issues)
        self.total_issues = total if total is not None else len(issues)
        self.issues = issues
        super().__init__(f"input rejected with {self.total_issues} issue(s)")

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "rejected",
            "total_issues": self.total_issues,
            "issues": [i.as_dict() for i in self.issues[:MAX_ISSUES_REPORTED]],
            "issues_truncated": self.total_issues > MAX_ISSUES_REPORTED,
        }


def reject(code: str, message: str, **kw: Any) -> None:
    raise InputRejected([Issue(code=code, message=message, **kw)])


@dataclass
class IssueCollector:
    issues: list[Issue] = field(default_factory=list)
    total: int = 0

    def add(self, issue: Issue) -> None:
        self.total += 1
        if len(self.issues) < MAX_ISSUES_REPORTED:
            self.issues.append(issue)

    def add_many(self, issues: Iterable[Issue]) -> None:
        for issue in issues:
            self.add(issue)

    def raise_if_any(self) -> None:
        if self.total:
            raise InputRejected(self.issues, total=self.total)
