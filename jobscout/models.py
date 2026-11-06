"""Shared data types and the sheet layout."""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from datetime import date

# Column order of the `Jobs` tab. The pipeline reads rows by header name, so you
# can add your own columns to the right without breaking anything.
JOBS_COLUMNS = [
    "Applied",  # checkbox, yours to tick; the agent never sets it
    "Ignore",   # checkbox: "not for me" -> never re-added, and the next run learns a rule from it
    "Job URL",  # next to the checkboxes: open the posting, then tick
    "Date added",
    "Fit score (1–10)",
    "Job title",
    "Company",
    "Startup?",  # Yes / No / unknown, from the company's funding stage and size
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
    "Company website",
    "Source",
    "Status",
    "My feedback",
    "Best resume",
    "LinkedIn URL",
    "Company job URL",
    "Date opened",
]

STATUS_VALUES = ["New", "Interested", "Applied", "Not a fit"]
NOT_A_FIT = "Not a fit"
CHECKBOX_COLUMNS = ["Applied", "Ignore"]

STARTUP_MAX_EMPLOYEES = 5000  # a venture-backed company bigger than this is a scale-up, not a startup


def _employees(size: str) -> int | None:
    m = re.search(r"\d[\d,]*", size or "")
    return int(m.group().replace(",", "")) if m and not (size or "").lower().startswith("unknown") else None


def startup_flag(funding: str, size: str) -> str:
    """'Yes' for a private, venture-backed (or small private) company; 'No' for public, acquired or big ones."""
    f = (funding or "").lower()
    n = _employees(size)
    if re.search(r"\bpublic\b|nasdaq|nyse|subsidiary|acquired|division of|part of", f):
        return "No"
    if re.search(r"pre-seed|\bseed\b|series [a-z]\b|venture|vc-backed|bootstrapped", f):
        return "No" if n and n > STARTUP_MAX_EMPLOYEES else "Yes"
    if "private" in f and n is not None:
        return "Yes" if n <= 1000 else "No"
    return "unknown"

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

# Optional separate "Log" spreadsheet in the person's folder: one detailed row per run, incl. cost.
RUNLOG_TAB = "Runs"
RUNLOG_COLUMNS = [
    "Run at (UTC)",
    "Type",
    "Where",
    "Found",
    "Added",
    "Skipped: duplicate",
    "Skipped: excluded / low score",
    "Skipped: over cap",
    "New learned exclusions",
    "SerpApi searches",
    "Claude calls",
    "Input tokens",
    "Output tokens",
    "Claude web searches",
    "Est. cost (USD)",
    "Duration (s)",
    "Errors / notes",
]

# Hidden bookkeeping tab: lets the agent remember what it already processed
# without ever touching your rows.
STATE_COLUMNS = ["Kind", "Key", "Date", "Value"]
STATE_FEEDBACK = "feedback_processed"
STATE_REJECTED = "rejected"
STATE_ATS = "ats"  # Key = normalized company, Value = detected careers system (or "none")

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
