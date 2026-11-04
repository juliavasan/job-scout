"""Past decisions: the person's own yes / no verdicts, shown to the judge as calibration examples.

This is the self-improvement loop. Every job the person ticks in the Jobs tab becomes an example the judge sees on
the next run:
  yes = Applied ticked (or Status "Applied")
  no  = Ignore ticked, Status "Not a fit", or the row highlighted orange/red
The examples sit in the judge's system prompt next to the brief. The brief's rules always win; the examples help
with the gray zone the brief doesn't spell out (and with calibrating the 1-10 score).

The eval keeps this honest: eval cases are never used as examples (see evaluation.py), so the effect of the
examples is measured on jobs the judge hasn't been shown.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

from .dedup import company_title_key

MAX_PER_SIDE = 10          # newest 10 yes + newest 10 no
NOTE_CHARS = 160


@dataclass(frozen=True)
class Decision:
    verdict: str            # "yes" | "no"
    title: str
    company: str
    location: str = ""
    salary: str = ""
    note: str = ""

    @property
    def key(self) -> str:
        return decision_key(self.company, self.title, self.location)


def decision_key(company: str, title: str, location: str) -> str:
    """Same job = same company + title + city (the eval uses the same key to keep its cases out of the examples)."""
    return company_title_key(company or "", title or "") + "|" + (location or "").split(",")[0].strip().lower()


def _clean(v, n: int = 120) -> str:
    """Sheet text, flattened so it can't close our tags or add lines of instructions."""
    return re.sub(r"\s+", " ", str(v or "")).replace("<", "(").replace(">", ")").strip()[:n]


def _ticked(v) -> bool:
    return str(v or "").strip().upper() == "TRUE"


def from_sheet_rows(rows: list[dict], flagged: set | frozenset = frozenset()) -> list[Decision]:
    """Decisions from Jobs-tab rows, newest first (rows are appended in date order). A row marked both yes and no
    is ambiguous and skipped."""
    out = []
    for r in reversed(rows):
        status = (r.get("Status") or "").strip()
        yes = _ticked(r.get("Applied")) or status == "Applied"
        no = _ticked(r.get("Ignore")) or status == "Not a fit" or r.get("_row") in flagged
        if yes == no:
            continue
        out.append(Decision("yes" if yes else "no", _clean(r.get("Job title")), _clean(r.get("Company")),
                            _clean(r.get("Location"), 60), _clean(r.get("Salary range"), 60),
                            _clean(r.get("My feedback"), NOTE_CHARS)))
    return out


def select(decisions: list[Decision], exclude_keys: set[str] = frozenset(), per_side: int = MAX_PER_SIDE) -> list[Decision]:
    """Newest `per_side` of each verdict, minus any job in exclude_keys (e.g. the eval's cases)."""
    kept, n = [], {"yes": 0, "no": 0}
    for d in decisions:
        if d.key in exclude_keys or n[d.verdict] >= per_side:
            continue
        kept.append(d)
        n[d.verdict] += 1
    return kept


def format_block(decisions: list[Decision]) -> str:
    if not decisions:
        return ""
    lines = []
    for d in decisions:
        tag = "YES (would apply)" if d.verdict == "yes" else "NO (not for me)"
        extra = "; ".join(x for x in (d.location, d.salary) if x and x.lower() not in ("not listed", "unknown"))
        lines.append(f"- {tag}: {d.title} at {d.company}" + (f" ({extra})" if extra else "") +
                     (f". Their note: {d.note}" if d.note else ""))
    return "\n\n<past_decisions>\n" + "\n".join(lines) + "\n</past_decisions>"


def split_labelled(cases: list[dict], labels: dict[str, dict], seed: int = 11) -> tuple[list[dict], list[dict]]:
    """Stratified half split of the labelled eval cases: (example half, test half). Alternates within each label so
    both halves get a fair share of Fit and Not fit. Deterministic."""
    train, test = [], []
    for lab in (1, 0):
        group = sorted((c for c in cases if c["case_id"] in labels and labels[c["case_id"]]["label"] == lab),
                       key=lambda c: c["case_id"])
        random.Random(seed).shuffle(group)
        for i, c in enumerate(group):
            (train if i % 2 == 0 else test).append(c)
    return train, test


def from_eval_cases(cases: list[dict], labels: dict[str, dict]) -> list[Decision]:
    return [Decision("yes" if labels[c["case_id"]]["label"] else "no", _clean(c.get("title")), _clean(c.get("company")),
                     _clean(c.get("location"), 60), _clean(c.get("salary_hint"), 60),
                     _clean(labels[c["case_id"]].get("note"), NOTE_CHARS))
            for c in cases]
