"""Helpers for the employer's own posting: is a URL on the company's site or ATS, is it a specific job's page
(not a careers landing page), and the schema.org `datePosted` embedded in the page. Used by careers.LinkResolver.
"""
from __future__ import annotations

import html
import json
import logging
import re
from datetime import date

import httpx

from .dedup import normalize_title
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

