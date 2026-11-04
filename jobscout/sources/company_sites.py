"""Search companies' own career sites directly.

Companies come from the "Companies to watch" section of the brief, one per line, in any of these forms:

    Acme Corp                                       <- a name: the careers system is found automatically (and cached)
    https://acme.wd5.myworkdayjobs.com/External     <- a careers/ATS URL
    greenhouse:acme   lever:acme   ashby:acme       <- explicit public boards

Supported careers systems: Workday, Eightfold, Greenhouse, Lever, Ashby, SmartRecruiters.
Each company's site is searched for the brief's role keywords; matches come with the exact company URL,
the date the job was opened, and the full description. No job boards, no LinkedIn.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import timedelta

from ..dedup import canonical_url
from ..models import RawJob
from .base import SearchPlan, Source, today

log = logging.getLogger(__name__)

_EXPLICIT = re.compile(r"^(greenhouse|lever|ashby|smartrecruiters|workday|eightfold):\s*(\S+)(?:\s+(\S+))?$", re.I)
PER_COMPANY = int(os.environ.get("JOBSCOUT_PER_COMPANY", "5"))
WATCH_MAX_AGE_DAYS = int(os.environ.get("JOBSCOUT_WATCH_MAX_AGE_DAYS", "90"))


def parse_watchlist(brief: str) -> list[str]:
    """Lines of the 'Companies to watch' section (comments starting with # are ignored)."""
    out, inside = [], False
    for raw in (brief or "").splitlines():
        line = raw.strip()
        if re.match(r"^companies to watch", line, re.I):
            inside = True
            head = re.sub(r"\([^)]*\)", "", line)  # ignore "(...)" help text on the heading
            rest = head.split(":", 1)[1].strip() if ":" in head else ""
            if rest:
                out += [x.strip() for x in rest.split(",") if x.strip()]
            continue
        if not inside:
            continue
        if not line:
            continue
        if re.match(r"^(=+|[A-Z][A-Za-z /'&-]{2,40}:\s*$)", line):  # next section header
            break
        line = line.lstrip("-•* ").strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return list(dict.fromkeys(out))


def _display_name(entry: str, ats) -> str:
    if not re.match(r"^(https?://|\w+:)", entry):
        return entry
    base = ats.a.split(".")[0] if ats.kind in ("workday",) else (ats.b.split(".")[0] if ats.kind == "eightfold" and ats.b else ats.a)
    return base.replace("-", " ").title()


class CompanySites(Source):
    name = "company_sites"

    def __init__(self, resolver):
        self.resolver = resolver
        self.errors: list[str] = []

    def _ats(self, entry: str):
        from ..careers import Ats, detect_ats

        m = _EXPLICIT.match(entry)
        if m:
            kind, a, b = m.group(1).lower(), m.group(2), m.group(3) or ""
            if kind == "workday":
                return detect_ats(a if a.startswith("http") else f"https://{a}")
            if kind == "eightfold":
                return Ats("eightfold", a.removeprefix("https://").split("/")[0], b)
            return Ats(kind, a)
        if entry.startswith("http"):
            return detect_ats(entry)
        return self.resolver.ats_for_company(entry)

    def fetch(self, plan: SearchPlan, max_age_days: int) -> list[RawJob]:
        from ..careers import fetch_details, search_ats

        out: list[RawJob] = []
        keywords = [k.lower() for k in plan.title_keywords] or [q.q.lower() for q in plan.queries]
        cutoff = today() - timedelta(days=max(max_age_days, WATCH_MAX_AGE_DAYS))
        for entry in plan.ats_boards:
            ats = self._ats(entry)
            if ats is None:
                self.errors.append("careers site not found for a watched company")
                continue
            company = _display_name(entry, ats)
            postings, seen = [], set()
            # list-style boards return everything in one call; search-style ATSs get one query per keyword
            queries = keywords[:4] if ats.kind in ("workday", "eightfold", "smartrecruiters") else [""]
            for q in queries:
                res = search_ats(ats, q or "")
                if res is None:
                    self.errors.append(f"{ats.kind} search failed for a watched company")
                    break
                for p in res:
                    if p.url in seen:
                        continue
                    seen.add(p.url)
                    if keywords and not any(k in p.title.lower() for k in keywords):
                        continue
                    if p.opened and p.opened < cutoff:
                        continue
                    if canonical_url(p.url) in plan.known_urls:
                        continue  # already in the sheet or already rejected: don't let it use up the per-company cap
                    postings.append(p)
            postings.sort(key=lambda p: p.opened or today(), reverse=True)
            for p in postings[:PER_COMPANY]:
                p = fetch_details(ats, p)
                out.append(RawJob(title=p.title, company=company, location=p.location, url=p.url,
                                  source=f"{company} careers site ({ats.kind})", description=p.description,
                                  posted_date=p.opened))
            log.info("company site: %d matching postings (%s)", len(postings[:PER_COMPANY]), ats.kind)
        return out
