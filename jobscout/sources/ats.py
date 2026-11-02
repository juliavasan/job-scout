"""Public ATS job boards (Greenhouse, Lever, Ashby). Free, no key, no login.

Boards come from the optional "Companies to watch" lines in the brief,
one per line, e.g. `greenhouse:anthropic`, `lever:mistral`, `ashby:openai`.
Only titles matching the brief's keywords are kept, so a big board doesn't
flood the LLM step.
"""
from __future__ import annotations

import html
import logging
import re

import httpx

from ..models import RawJob
from .base import SearchPlan, Source, is_recent, parse_iso_date

log = logging.getLogger(__name__)


def _strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", s)).strip()


def _matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    t = title.lower()
    return any(k.lower() in t for k in keywords)


def _greenhouse(client: httpx.Client, slug: str) -> list[RawJob]:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    r.raise_for_status()
    company = slug
    try:
        meta = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        if meta.status_code == 200:
            company = meta.json().get("name") or slug
    except httpx.HTTPError:
        pass
    out = []
    for j in r.json().get("jobs", []):
        out.append(RawJob(
            title=j.get("title", ""), company=company,
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""), source="Greenhouse",
            description=_strip_html(j.get("content", "")),
            posted_date=parse_iso_date(j.get("first_published") or j.get("updated_at")),
        ))
    return out


def _lever(client: httpx.Client, slug: str) -> list[RawJob]:
    r = client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        sal = j.get("salaryRange") or {}
        salary = f"{sal.get('currency', '')} {sal.get('min', '')}–{sal.get('max', '')} {sal.get('interval', '')}".strip() if sal else ""
        desc = j.get("descriptionPlain", "") + "\n\n" + "\n\n".join(
            f"{l.get('text', '')}:\n{_strip_html(l.get('content', ''))}" for l in j.get("lists") or []
        ) + "\n\n" + (j.get("additionalPlain") or "")
        out.append(RawJob(
            title=j.get("text", ""), company=slug,
            location=cats.get("location", "") or ", ".join(cats.get("allLocations") or []),
            url=j.get("hostedUrl", ""), source="Lever", description=desc.strip(),
            posted_date=parse_iso_date(j.get("createdAt")), salary_hint=salary,
            work_mode_hint=(j.get("workplaceType") or "").replace("onsite", "On-site").title(),
            employment_type_hint=cats.get("commitment", ""),
        ))
    return out


def _ashby(client: httpx.Client, slug: str) -> list[RawJob]:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        mode = j.get("workplaceType") or ("Remote" if j.get("isRemote") else "")
        out.append(RawJob(
            title=j.get("title", ""), company=slug, location=j.get("location", ""),
            url=j.get("jobUrl", ""), source="Ashby",
            description=j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", "")),
            posted_date=parse_iso_date(j.get("publishedAt")), salary_hint=comp,
            work_mode_hint=str(mode).replace("OnSite", "On-site"), employment_type_hint=j.get("employmentType", ""),
        ))
    return out


_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def parse_watchlist(text: str) -> list[str]:
    boards = []
    for line in (text or "").splitlines():
        line = line.strip().strip(",")
        if not line or line.startswith("#") or ":" not in line:
            continue
        kind, slug = (p.strip().lower() for p in line.split(":", 1))
        if kind in _FETCHERS and re.fullmatch(r"[a-z0-9._-]+", slug):
            boards.append(f"{kind}:{slug}")
    return boards


class AtsBoards(Source):
    name = "ats_boards"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self.errors: list[str] = []

    def fetch(self, plan: SearchPlan, max_age_days: int) -> list[RawJob]:
        out: list[RawJob] = []
        for board in plan.ats_boards:
            kind, slug = board.split(":", 1)
            try:
                jobs = _FETCHERS[kind](self.client, slug)
            except (httpx.HTTPError, ValueError) as e:
                self.errors.append(f"{board}: {type(e).__name__}")
                log.warning("ATS board %s failed: %s", board, type(e).__name__)
                continue
            out += [j for j in jobs if j.title and _matches(j.title, plan.title_keywords) and is_recent(j.posted_date, max_age_days)]
        log.info("ats: %d boards, %d matching recent postings", len(plan.ats_boards), len(out))
        return out
