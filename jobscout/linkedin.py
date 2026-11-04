"""Employer matching for job URLs, and the LinkedIn URL for each job, without ever logging in to or fetching LinkedIn.

The LinkedIn URL is the exact linkedin.com/jobs/view/... link when Google Jobs returned one for the right employer,
otherwise "not found". (A LinkedIn search link was tried and dropped: it rarely lands on the posting.)
"""
from __future__ import annotations

import re

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


_NOISE = re.compile(r"(^|\s[-–|]\s*|\()\s*(remote|hybrid|on-?site|us|usa|united states)\s*(\)|\s*[-–|]\s|$)", re.I)


def core_title(title: str) -> str:
    """'Senior Data Engineer, Payments - Remote' -> 'Senior Data Engineer'. Career-site searches match every keyword,
    so long titles with team names usually return nothing."""
    t = re.sub(r"\([^)]*\)", " ", title)
    t = _NOISE.sub(" ", t).strip(" -–|")
    t = re.split(r"\s*[,|–]\s*|\s+-\s+|\s+/\s+", t)[0]
    return re.sub(r"\s+", " ", t).strip() or title.strip()

