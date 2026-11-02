"""SerpApi Google Jobs source.

Google for Jobs aggregates LinkedIn, Indeed, Glassdoor, company career pages, etc.
We never touch LinkedIn directly: we only read what Google's index returns, and
prefer the LinkedIn apply link as the row URL when one exists.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

from ..models import RawJob
from .base import SearchPlan, Source, is_recent, parse_relative_date

log = logging.getLogger(__name__)

ENDPOINT = "https://serpapi.com/search.json"
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com", "myworkdayjobs.com")
AGGREGATOR_HOSTS = ("indeed.", "glassdoor.", "ziprecruiter.", "monster.", "simplyhired.", "talent.com", "builtin", "wellfound.", "dice.com", "jooble", "adzuna", "learn4good", "bebee", "jobright", "lensa")


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def pick_best_url(apply_options: list[dict], share_link: str) -> tuple[str, str]:
    """Return (url, source_label). Preference: LinkedIn > ATS/company page > aggregator > Google share link."""
    links = [(o.get("title") or "", o.get("link") or "") for o in apply_options if o.get("link")]
    for title, link in links:
        if "linkedin.com" in _host(link):
            return link, "LinkedIn (via Google Jobs)"
    for title, link in links:
        if any(h in _host(link) for h in ATS_HOSTS):
            return link, f"{title or _host(link)} (via Google Jobs)"
    for title, link in links:
        if not any(h in _host(link) for h in AGGREGATOR_HOSTS):
            return link, f"{title or _host(link)} (via Google Jobs)"
    if links:
        title, link = links[0]
        return link, f"{title or _host(link)} (via Google Jobs)"
    return share_link, "Google Jobs"


def _description(item: dict) -> str:
    parts = [item.get("description") or ""]
    for h in item.get("job_highlights") or []:
        items = h.get("items") or []
        if items:
            parts.append(f"{h.get('title') or 'Highlights'}:\n- " + "\n- ".join(items))
    return "\n\n".join(p for p in parts if p).strip()


def parse_result(item: dict) -> RawJob:
    ext = item.get("detected_extensions") or {}
    apply_options = item.get("apply_options") or []
    url, source = pick_best_url(apply_options, item.get("share_link") or "")
    posted = parse_relative_date(ext.get("posted_at") or "")
    if posted is None:
        for e in item.get("extensions") or []:
            posted = parse_relative_date(e)
            if posted:
                break
    work_mode = "Remote" if ext.get("work_from_home") else ""
    return RawJob(
        title=(item.get("title") or "").strip(),
        company=(item.get("company_name") or "").strip(),
        location=(item.get("location") or "").strip(),
        url=url,
        source=source,
        description=_description(item),
        posted_date=posted,
        other_urls=[o.get("link") for o in apply_options if o.get("link") and o.get("link") != url],
        salary_hint=ext.get("salary") or "",
        work_mode_hint=work_mode,
        employment_type_hint=ext.get("schedule_type") or "",
    )


class SerpApiGoogleJobs(Source):
    name = "serpapi_google_jobs"

    def __init__(self, api_key: str, max_queries: int = 4, pages_per_query: int = 1, client: httpx.Client | None = None):
        self.api_key = api_key
        self.max_queries = max_queries
        self.pages_per_query = pages_per_query
        self.client = client or httpx.Client(timeout=60)
        self.searches_used = 0

    def _search(self, params: dict) -> dict:
        self.searches_used += 1
        r = self.client.get(ENDPOINT, params={**params, "engine": "google_jobs", "api_key": self.api_key, "hl": "en", "gl": "us"})
        if r.status_code >= 400:
            # Don't echo the URL (it contains the API key).
            raise RuntimeError(f"SerpApi HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("error") and "hasn't returned any results" not in data["error"]:
            raise RuntimeError(f"SerpApi error: {data['error']}")
        return data

    def fetch(self, plan: SearchPlan, max_age_days: int) -> list[RawJob]:
        out: list[RawJob] = []
        for query in plan.queries[: self.max_queries]:
            params = {"q": query.q}
            if query.location:
                params["location"] = query.location
            token = None
            for _ in range(self.pages_per_query):
                data = self._search({**params, **({"next_page_token": token} if token else {})})
                for item in data.get("jobs_results") or []:
                    job = parse_result(item)
                    if job.title and job.company and is_recent(job.posted_date, max_age_days):
                        out.append(job)
                token = (data.get("serpapi_pagination") or {}).get("next_page_token")
                if not token:
                    break
        log.info("serpapi: %d searches, %d recent postings", self.searches_used, len(out))
        return out
