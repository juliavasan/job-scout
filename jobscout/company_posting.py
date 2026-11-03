"""The employer's own posting URL and the date the position was opened.

Order of preference for the URL:
1. an apply link Google Jobs returned that is on the company's own site or ATS (Greenhouse, Lever, Workday, ...)
2. the company's public Greenhouse / Lever / Ashby board (free JSON APIs), matched by job title
3. Claude web search (job boards blocked), checked to be on the employer's own domain/ATS (~3 cents per job)
Date opened:
1. the ATS's own publish date (exact)
2. schema.org JobPosting `datePosted` embedded in the company posting page (what Google Jobs itself reads)
3. Google's "N days ago" (approximate, marked with ~)
"""
from __future__ import annotations

import html
import json
import logging
import re
from datetime import date
from functools import lru_cache

import httpx

from .dedup import normalize_company, normalize_title
from .linkedin import company_tokens
from .sources.base import parse_iso_date

log = logging.getLogger(__name__)

ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com", "myworkdayjobs.com",
             "icims.com", "jobvite.com", "bamboohr.com", "taleo.net", "successfactors", "oraclecloud.com", "eightfold.ai")
JOB_BOARDS = ("linkedin.", "indeed.", "glassdoor.", "ziprecruiter.", "monster.", "simplyhired.", "builtin", "wellfound.",
              "dice.com", "talent.com", "jooble", "adzuna", "bebee", "jobright", "lensa", "tealhq", "trabajo", "bandana",
              "jobleads", "mediabistro", "remotesource", "univision", "learn4good", "careerbuilder", "theladders", "hiring.cafe")

_client = httpx.Client(timeout=15, follow_redirects=True,
                       headers={"User-Agent": "Mozilla/5.0 (compatible; job-scout/0.1)"})


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "", re.I)
    return m.group(1).lower().removeprefix("www.") if m else ""


def is_company_url(url: str, company: str) -> bool:
    host = _host(url)
    if not host or any(b in host for b in JOB_BOARDS):
        return False
    if any(h in host for h in ATS_HOSTS):
        # ATS URLs carry the company slug, e.g. boards.greenhouse.io/acme/..., acme.wd5.myworkdayjobs.com
        flat = url.lower().replace("-", "")
        return any(t in flat for t in company_tokens(company)) or not company_tokens(company)
    flat_host = host.replace("-", "")
    return any(t in flat_host for t in company_tokens(company) if len(t) >= 4)


def looks_like_posting(url: str, titles: list[str]) -> bool:
    """A specific job's page, not a careers landing/search page: has a job id or the title's words in the URL."""
    m = re.match(r"https?://[^/]+(/[^#]*)", url or "")
    if not m:
        return False
    path = m.group(1).lower()
    if re.search(r"\d{4,}", re.sub(r"[?&](refresh|utm_[a-z]+|page|p)=[^&]*", "", path)):
        return True
    flat = re.sub(r"[^a-z0-9]+", " ", path)
    for t in titles:
        words = [w for w in normalize_title(t).split() if len(w) >= 4]
        if len(words) >= 2 and sum(w in flat for w in words) >= 2:
            return True
    return False


def _title_match(a: str, b: str) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return len(ta & tb) / max(len(ta | tb), 1) >= 0.75


def _slugs(company: str) -> list[str]:
    toks = normalize_company(company).split()
    cands = ["".join(toks), "-".join(toks)] + ([toks[0]] if toks else [])
    return list(dict.fromkeys(c for c in cands if c))


@lru_cache(maxsize=256)
def _ats_board(company: str) -> tuple[tuple[str, str, str, str], ...]:
    """All postings on the company's public Greenhouse/Lever/Ashby board: (title, url, iso date, ats). Cached per run."""
    for slug in _slugs(company):
        try:
            r = _client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            if r.status_code == 200 and r.json().get("jobs"):
                return tuple((j.get("title", ""), j.get("absolute_url", ""), j.get("first_published") or j.get("updated_at") or "", "Greenhouse")
                             for j in r.json()["jobs"])
            r = _client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
            if r.status_code == 200 and isinstance(r.json(), list) and r.json():
                return tuple((j.get("text", ""), j.get("hostedUrl", ""),
                              parse_iso_date(j.get("createdAt")).isoformat() if j.get("createdAt") else "", "Lever")
                             for j in r.json())
            r = _client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            if r.status_code == 200 and r.json().get("jobs"):
                return tuple((j.get("title", ""), j.get("jobUrl", ""), j.get("publishedAt") or "", "Ashby") for j in r.json()["jobs"])
        except (httpx.HTTPError, ValueError):
            continue
    return ()


def find_on_ats(company: str, title: str) -> tuple[str, date | None, str]:
    for t, url, d, ats in _ats_board(company):
        if _title_match(t, title):
            return url, parse_iso_date(d), ats
    return "", None, ""


def _walk_jsonld(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk_jsonld(n)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                yield from _walk_jsonld(node[key])


def date_posted_from_page(url: str) -> date | None:
    """schema.org JobPosting.datePosted from the posting page (no JS executed)."""
    try:
        r = _client.get(url)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return None
        page = r.text[:3_000_000]
    except httpx.HTTPError:
        return None
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(html.unescape(block.strip()))
        except ValueError:
            continue
        for node in _walk_jsonld(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" in types and node.get("datePosted"):
                return parse_iso_date(str(node["datePosted"])[:25])
    m = re.search(r'"datePosted"\s*:\s*"([^"]+)"', page)
    return parse_iso_date(m.group(1)[:25]) if m else None


def resolve(company: str, titles: list[str], known_urls: list[str], board_date: date | None, claude=None) -> tuple[str, str]:
    """Return (company job URL or 'not found', date opened text)."""
    url, opened, source = "", None, ""
    for u in known_urls:
        if u and is_company_url(u, company) and looks_like_posting(u, titles):
            url = u
            break
    if not url:
        for t in titles:
            url, opened, source = find_on_ats(company, t)
            if url:
                break
    if not url and claude is not None:
        from . import llm as L
        try:
            found = L.find_company_posting(claude, titles[-1] or titles[0], company)
        except Exception:
            found = ""
        if found and is_company_url(found, company) and looks_like_posting(found, titles):
            url = found
    if url and opened is None:
        opened = date_posted_from_page(url)
        source = "company page" if opened else ""
    if opened:
        date_text = f"{opened.isoformat()} ({source})"
    elif board_date:
        date_text = f"~{board_date.isoformat()} (job board)"
    else:
        date_text = "unknown"
    return url or "not found", date_text
