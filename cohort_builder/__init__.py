"""Cohort builder: longitudinal encounter streams -> index day or pending.

Standard library + DuckDB only.  No cross-person identity resolution, no
statistics, no efficacy computation.
"""

__version__ = "1.0.0"

from .config import Plan, load_plan
from .engine import build
from .errors import InputRejected, Issue

__all__ = ["Plan", "load_plan", "build", "InputRejected", "Issue"]
