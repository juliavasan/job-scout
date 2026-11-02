"""SerpApi Google Jobs source.

Google for Jobs aggregates LinkedIn, Indeed, Glassdoor, company career pages, etc.
We never touch LinkedIn directly: we only read what Google's index returns, and
prefer the LinkedIn apply link as the row URL when one exists.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..linkedin import company_tokens, matches_company
from ..models import RawJob
from .base import SearchPlan, Source, is_recent, parse_relative_date

log = logging.getLogger(__name__)

ENDPOINT = "https://serpapi.com/search.json"
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com", "myworkdayjobs.com",
             "icims.com", "jobvite.com", "bamboohr.com", "taleo.net", "successfactors", "oraclecloud.com", "eightfold.ai")
# Well-known boards: acceptable when there's no LinkedIn/company link.
KNOWN_BOARDS = ("indeed.", "glassdoor.", "ziprecruiter.", "builtin", "wellfound.", "dice.com", "monster.", "simplyhired.")
_TRACKING = re.compile(r"^(utm_|trk|refid$|trackingid$|src$|source$|gh_src$|lever-source)", re.I)


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def clean_url(url: str) -> str:
    """Drop tracking parameters (utm_*, trk, refId…) but keep ones that identify the job."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def _company_tokens(company: str) -> list[str]:
    return company_tokens(company)


def linkedin_matches_company(url: str, company: str) -> bool:
    return matches_company(url, company)


def _is_company_site(url: str, company: str) -> bool:
    host = _host(url).replace("-", "")
    return any(t in host for t in _company_tokens(company) if len(t) >= 4)


def pick_best_url(apply_options: list[dict], share_link: str, company: str = "") -> tuple[str, str, list[str]]:
    """Return (url, source_label, other_valid_urls).
    Preference: LinkedIn (verified) > company site / ATS > well-known board > anything else > Google share link."""
    links = []
    for o in apply_options:
        link = o.get("link") or ""
        if not link:
            continue
        if "linkedin.com" in _host(link) and company and not linkedin_matches_company(link, company):
            continue  # wrong employer's posting attached by Google; ignore it entirely
        links.append((o.get("title") or _host(link), clean_url(link)))
    tiers = [
        lambda l: "linkedin.com" in _host(l),
        lambda l: _is_company_site(l, company) or any(h in _host(l) for h in ATS_HOSTS),
        lambda l: any(h in _host(l) for h in KNOWN_BOARDS),
        lambda l: True,
    ]
    for tier in tiers:
        for title, link in links:
            if tier(link):
                label = "LinkedIn (via Google Jobs)" if "linkedin.com" in _host(link) else f"{title} (via Google Jobs)"
                return link, label, [l for _, l in links if l != link]
    return clean_url(share_link), "Google Jobs", []


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
    url, source, others = pick_best_url(apply_options, item.get("share_link") or "", item.get("company_name") or "")
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
        other_urls=others,
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
        self.errors: list[str] = []

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

    # Locations SerpApi rejected earlier in this run: fold them into the query text up front
    # so a failing location costs one search per run at most.
    _bad_locations: set[str] = set()

    def _run_query(self, query, max_age_days: int) -> list[RawJob]:
        params = {"q": query.q}
        if query.location and query.location in self._bad_locations:
            params = {"q": f"{query.q} {query.location}"}
        elif query.location:
            params["location"] = query.location
        out, token = [], None
        for _ in range(self.pages_per_query):
            try:
                data = self._search({**params, **({"next_page_token": token} if token else {})})
            except RuntimeError as e:
                if "location" in str(e).lower() and "location" in params:
                    # SerpApi only knows certain location names; fold it into the query text instead.
                    self._bad_locations.add(params["location"])
                    params = {"q": f"{query.q} {params.pop('location')}"}
                    data = self._search(params)
                else:
                    raise
            for item in data.get("jobs_results") or []:
                job = parse_result(item)
                if job.title and job.company and is_recent(job.posted_date, max_age_days):
                    out.append(job)
            token = (data.get("serpapi_pagination") or {}).get("next_page_token")
            if not token:
                break
        return out

    def fetch(self, plan: SearchPlan, max_age_days: int) -> list[RawJob]:
        out: list[RawJob] = []
        self.errors = []
        for query in plan.queries[: self.max_queries]:
            try:
                out += self._run_query(query, max_age_days)
            except Exception as e:  # one bad query shouldn't sink the others
                self.errors.append(f"serpapi query failed: {str(e)[:150]}")
                log.warning("serpapi query failed: %s", type(e).__name__)
        if self.errors and not out:
            raise RuntimeError("; ".join(self.errors))
        log.info("serpapi: %d searches, %d recent postings", self.searches_used, len(out))
        return out
