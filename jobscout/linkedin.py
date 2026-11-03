"""LinkedIn URL for each job, without ever logging in to or fetching LinkedIn.

Order of preference:
1. a LinkedIn apply link Google Jobs already returned (checked to belong to the right employer)
2. optional (JOBSCOUT_LINKEDIN_SEARCH=1): a linkedin.com/jobs/view/... posting found by Claude's web search.
   Off by default: postings from the last few days are rarely in the web index yet, so it mostly costs money.
3. a LinkedIn job-search link for "<title> <company>" (always works; you land on the results)
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from .dedup import canonical_url, normalize_company

_GENERIC = {"the", "and", "group", "systems", "system", "technologies", "technology", "tech", "labs", "global",
            "international", "holdings", "services", "solutions", "software", "health", "inc", "usa", "america"}
_VIEW = re.compile(r"linkedin\.com/jobs/view/", re.I)


def company_tokens(company: str) -> list[str]:
    return [t for t in normalize_company(company).split() if len(t) >= 3 and t not in _GENERIC]


def matches_company(url: str, company: str) -> bool:
    """LinkedIn slugs look like /jobs/view/<title>-at-<company>-<id>. If there's a slug, it must name the company."""
    m = re.search(r"/jobs/view/([^/?#]+)", url)
    if not m or re.fullmatch(r"\d+", m.group(1)):
        return True  # bare ID: nothing to check against
    slug = m.group(1).lower()
    parts = slug.rsplit("-at-", 1)
    company_part = parts[1] if len(parts) == 2 else slug
    toks = company_tokens(company)
    return not toks or any(t in company_part or t in company_part.replace("-", "") for t in toks)


def posting_from_urls(urls: list[str], company: str) -> str:
    for u in urls:
        if u and _VIEW.search(u) and matches_company(u, company):
            return canonical_url(u)
    return ""


SEARCH_ENABLED = os.environ.get("JOBSCOUT_LINKEDIN_SEARCH", "").lower() in ("1", "true", "yes")
_NOISE = re.compile(r"(^|\s[-–|]\s*|\()\s*(remote|hybrid|on-?site|us|usa|united states)\s*(\)|\s*[-–|]\s|$)", re.I)
_CO_SUFFIX = re.compile(r",?\s+(inc\.?|incorporated|corp\.?|corporation|llc|ltd\.?|co\.?|company|plc)$", re.I)


def core_title(title: str) -> str:
    """'Senior Data Engineer, Payments - Remote' -> 'Senior Data Engineer'. LinkedIn search needs every keyword to match,
    so long titles with team names usually return no results."""
    t = re.sub(r"\([^)]*\)", " ", title)
    t = _NOISE.sub(" ", t).strip(" -–|")
    t = re.split(r"\s*[,|–]\s*|\s+-\s+|\s+/\s+", t)[0]
    return re.sub(r"\s+", " ", t).strip() or title.strip()


def search_url(title: str, company: str) -> str:
    """LinkedIn job search for the core title at this company."""
    c = _CO_SUFFIX.sub("", company.strip())
    return f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(f'{core_title(title)} {c}'.strip())}"


def resolve(claude, title: str, company: str, location: str, known_urls: list[str]) -> tuple[str, str]:
    """Return (url, kind) where kind is 'posting', 'found' or 'search'."""
    url = posting_from_urls(known_urls, company)
    if url:
        return url, "posting"
    if claude is not None and SEARCH_ENABLED:
        from . import llm as L
        try:
            found = L.find_linkedin_posting(claude, title, company, location)
        except Exception:
            found = ""
        if found and _VIEW.search(found) and matches_company(found, company):
            return canonical_url(found), "found"
    return search_url(title, company), "search"
