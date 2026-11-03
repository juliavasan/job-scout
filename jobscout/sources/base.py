"""Pluggable source interface. Add a new source by subclassing `Source`."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..models import RawJob


@dataclass
class SearchQuery:
    q: str
    location: str = ""  # empty = let the query text decide (e.g. "remote")


@dataclass
class SearchPlan:
    """What the LLM derived from the brief: search queries + title keywords."""

    queries: list[SearchQuery]
    title_keywords: list[str] = field(default_factory=list)
    ats_boards: list[str] = field(default_factory=list)  # "greenhouse:acme", "lever:foo", "ashby:bar"
    known_urls: set[str] = field(default_factory=set)    # canonical URLs already in the sheet or already rejected


class Source(ABC):
    name: str = "source"

    @abstractmethod
    def fetch(self, plan: SearchPlan, max_age_days: int) -> list[RawJob]:
        """Return postings no older than `max_age_days` (unknown dates are kept)."""


def today() -> date:
    return datetime.now(timezone.utc).date()


_REL = re.compile(r"(\d+|an?|one)\+?\s*(minute|hour|day|week|month|year)s?\s*ago", re.I)


def parse_relative_date(text: str, ref: date | None = None) -> date | None:
    """'3 days ago' -> date. Returns None if not parseable."""
    if not text:
        return None
    ref = ref or today()
    t = text.lower()
    if "just posted" in t or "today" in t:
        return ref
    if "yesterday" in t:
        return ref - timedelta(days=1)
    m = _REL.search(t)
    if not m:
        return None
    n_raw, unit = m.group(1), m.group(2).lower()
    n = 1 if n_raw in ("a", "an", "one") else int(n_raw)
    days = {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n, "year": 365 * n}[unit]
    return ref - timedelta(days=days)


def parse_iso_date(value) -> date | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):  # epoch ms (Lever)
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, OSError):
        return None


def is_recent(d: date | None, max_age_days: int) -> bool:
    return d is None or (today() - d).days <= max_age_days
