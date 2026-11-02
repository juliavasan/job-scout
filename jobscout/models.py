"""Shared data types and the sheet layout."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Column order of the `Jobs` tab. The pipeline reads rows by header name, so you
# can add your own columns to the right without breaking anything.
JOBS_COLUMNS = [
    "Date added",
    "Fit score (1–10)",
    "Job title",
    "Company",
    "Why it fits",
    "Concerns / red flags",
    "Location",
    "Work mode",
    "Employment type",
    "Seniority",
    "Salary range",
    "Equity mentioned",
    "Posted date",
    "Company type / industry",
    "Company size (employees)",
    "Funding stage / public",
    "HQ",
    "Company description",
    "Job URL",
    "Company website",
    "Source",
    "Status",
    "My feedback",
    "Best resume",
    "LinkedIn URL",
]

STATUS_VALUES = ["New", "Interested", "Applied", "Not a fit"]
NOT_A_FIT = "Not a fit"

LOG_COLUMNS = [
    "Run at (UTC)",
    "Found",
    "Added",
    "Skipped: duplicate",
    "Skipped: excluded / low score",
    "Skipped: over cap",
    "New learned exclusions",
    "Errors",
]

# Hidden bookkeeping tab: lets the agent remember what it already processed
# without ever touching your rows.
STATE_COLUMNS = ["Kind", "Key", "Date"]
STATE_FEEDBACK = "feedback_processed"
STATE_REJECTED = "rejected"

TAB_BRIEF = "Brief"
TAB_JOBS = "Jobs"
TAB_LOG = "Log"
TAB_STATE = "_state"

LEARNED_HEADER = "=== Learned exclusions (auto-added from your feedback — edit or delete freely) ==="


@dataclass
class RawJob:
    """A posting as returned by a source, before any judging."""

    title: str
    company: str
    location: str
    url: str  # best URL for the sheet (LinkedIn when available)
    source: str
    description: str = ""
    posted_date: date | None = None
    other_urls: list[str] = field(default_factory=list)  # every apply link we saw (used for dedup)
    salary_hint: str = ""
    work_mode_hint: str = ""
    employment_type_hint: str = ""
    company_website: str = ""

    def all_urls(self) -> list[str]:
        return [u for u in [self.url, *self.other_urls] if u]
