"""Find each job on the company's own careers site (its ATS), plus the exact LinkedIn posting.

No LinkedIn access at any point: LinkedIn links only come from Google Jobs results.

Per accepted job:
1. Look at every apply link we know (from Google Jobs). Detect the company's ATS from them
   (Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Eightfold) and pick up a LinkedIn /jobs/view/ link.
2. If we still lack the ATS or the LinkedIn link: one extra Google Jobs search via SerpApi for "<title> <company>"
   (its apply options list every site the job is posted on). Budget-capped.
3. If the ATS is still unknown: try the company's public Greenhouse/Lever/Ashby board by name, then (once per
   company, cached in the sheet's _state tab) ask Claude to find the company's careers/ATS URL.
4. Search the ATS by title -> exact posting URL + exact date opened.
   If the ATS is known but has no matching job, the job is likely closed: say so.
5. Every URL we write is checked with a real HTTP request (no 404s, no redirects to a generic search page).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote, urlsplit

import httpx

from .company_posting import date_posted_from_page, is_company_url, looks_like_posting
from .dedup import canonical_url, normalize_company, normalize_title
from .linkedin import company_tokens, core_title, matches_company
from .sources.base import parse_iso_date, parse_relative_date

log = logging.getLogger(__name__)

NOT_ON_SITE = "not listed on company site (may be closed)"
NOT_FOUND = "not found"

_http = httpx.Client(timeout=20, follow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0 (compatible; job-scout/0.1; +https://github.com/juliavasan/job-scout)",
                              "Accept": "application/json, text/html;q=0.9"})


def _get(url: str, **kw) -> httpx.Response | None:
    for attempt in range(3):
        try:
            r = _http.get(url, **kw)
        except httpx.HTTPError:
            return None
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        return r
    return None


def _post(url: str, payload: dict) -> httpx.Response | None:
    for attempt in range(3):
        try:
            r = _http.post(url, json=payload, headers={"Content-Type": "application/json"})
        except httpx.HTTPError:
            return None
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        return r
    return None


# ---------------- ATS detection ----------------

@dataclass(frozen=True)
class Ats:
    kind: str           # workday | greenhouse | lever | ashby | smartrecruiters | eightfold
    a: str              # main id (workday host / slug / eightfold host)
    b: str = ""         # secondary (workday site / eightfold domain)

    def dumps(self) -> str:
        return json.dumps([self.kind, self.a, self.b])

    @classmethod
    def loads(cls, s: str) -> "Ats | None":
        try:
            k, a, b = json.loads(s)
            return cls(k, a, b)
        except (ValueError, TypeError):
            return None


def _registered_domain(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def detect_ats(url: str, company_domain: str = "") -> Ats | None:
    if not url:
        return None
    u = url.strip()
    host = urlsplit(u).netloc.lower().removeprefix("www.")
    path = urlsplit(u).path
    m = re.match(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if m:
        seg = [s for s in path.split("/") if s]
        if seg and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", seg[0]):
            seg = seg[1:]
        if seg and seg[0] not in ("wday", "job"):
            return Ats("workday", host, seg[0])
        return None
    m = re.match(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io$", host)
    if m:
        seg = [s for s in path.split("/") if s]
        if seg and seg[0] != "embed":
            return Ats("greenhouse", seg[0])
        q = dict(x.split("=", 1) for x in urlsplit(u).query.split("&") if "=" in x)
        return Ats("greenhouse", q["for"]) if q.get("for") else None
    if host == "jobs.lever.co":
        seg = [s for s in path.split("/") if s]
        return Ats("lever", seg[0]) if seg else None
    if host == "jobs.ashbyhq.com":
        seg = [s for s in path.split("/") if s]
        return Ats("ashby", seg[0]) if seg else None
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
        seg = [s for s in path.split("/") if s]
        return Ats("smartrecruiters", seg[0]) if seg else None
    if re.search(r"/careers/job/\d{6,}", path) or host.endswith("eightfold.ai"):
        domain = company_domain or _registered_domain(host)
        return Ats("eightfold", host, domain)
    return None


def guess_board(company: str) -> Ats | None:
    """Try the company's public Greenhouse / Lever / Ashby board by name (free)."""
    toks = normalize_company(company).split()
    for slug in dict.fromkeys(["".join(toks), "-".join(toks), toks[0] if toks else ""]):
        if not slug:
            continue
        r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if r is not None and r.status_code == 200 and r.json().get("jobs"):
            return Ats("greenhouse", slug)
        r = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json", "limit": 1})
        if r is not None and r.status_code == 200 and isinstance(r.json(), list) and r.json():
            return Ats("lever", slug)
        r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if r is not None and r.status_code == 200 and r.json().get("jobs"):
            return Ats("ashby", slug)
    return None


# ---------------- ATS search ----------------

@dataclass
class Posting:
    title: str
    url: str
    opened: date | None
    location: str = ""
    description: str = ""
    detail: str = ""  # ATS-specific id/path to fetch the full description


def _html_to_text(s: str) -> str:
    import html as _h
    s = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", _h.unescape(s))).strip()


def fetch_details(ats: Ats, p: Posting) -> Posting:
    """Fill description (and exact date when the list didn't have it) from the ATS detail endpoint."""
    try:
        if ats.kind == "workday" and p.detail:
            tenant = ats.a.split(".")[0]
            r = _get(f"https://{ats.a}/wday/cxs/{tenant}/{ats.b}{p.detail}")
            if r is not None and r.status_code == 200:
                info = r.json().get("jobPostingInfo", {})
                p.description = _html_to_text(info.get("jobDescription", ""))
                p.opened = parse_iso_date(info.get("startDate")) or p.opened
                p.url = info.get("externalUrl") or p.url
        elif ats.kind == "eightfold" and p.detail:
            host, pid = p.detail.split("|", 1)
            r = _get(f"https://{host}/api/apply/v2/jobs/{pid}", params={"domain": ats.b})
            if r is not None and r.status_code == 200:
                d = r.json()
                p.description = _html_to_text(d.get("job_description", ""))
        elif ats.kind == "greenhouse" and p.detail:
            r = _get(f"https://boards-api.greenhouse.io/v1/boards/{ats.a}/jobs/{p.detail}")
            if r is not None and r.status_code == 200:
                p.description = _html_to_text(r.json().get("content", ""))
    except (ValueError, KeyError, TypeError):
        pass
    return p


def search_ats(ats: Ats, title: str) -> list[Posting] | None:
    """Postings on the ATS matching the title query. None = the ATS couldn't be queried."""
    q = core_title(title)
    try:
        if ats.kind == "workday":
            tenant = ats.a.split(".")[0]
            r = _post(f"https://{ats.a}/wday/cxs/{tenant}/{ats.b}/jobs",
                      {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": title})
            if r is None or r.status_code != 200:
                return None
            return [Posting(p.get("title", ""), f"https://{ats.a}/{ats.b}{p.get('externalPath', '')}",
                            parse_relative_date(p.get("postedOn") or ""), p.get("locationsText", ""),
                            detail=p.get("externalPath", ""))
                    for p in r.json().get("jobPostings", [])]
        if ats.kind == "eightfold":
            r = None
            hosts = [ats.a]
            if not ats.a.endswith("eightfold.ai") and ats.b:
                hosts.append(f"{ats.b.split('.')[0]}.eightfold.ai")  # custom domains often don't expose the API
            for h in hosts:
                r = _get(f"https://{h}/api/apply/v2/jobs", params={"domain": ats.b, "query": title, "num": 10})
                if r is not None and r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                    break
            else:
                return None
            out = []
            for p in r.json().get("positions", []):
                t = p.get("t_create")
                out.append(Posting(p.get("name", ""), p.get("canonicalPositionUrl") or f"https://{h}/careers/job/{p.get('id')}",
                                   date.fromtimestamp(t) if t else None, p.get("location", ""), detail=f"{h}|{p.get('id')}"))
            return out
        if ats.kind == "greenhouse":
            r = _get(f"https://boards-api.greenhouse.io/v1/boards/{ats.a}/jobs")
            if r is None or r.status_code != 200:
                return None
            return [Posting(j.get("title", ""), j.get("absolute_url", ""),
                            parse_iso_date(j.get("first_published") or j.get("updated_at")),
                            (j.get("location") or {}).get("name", ""), detail=str(j.get("id", ""))) for j in r.json().get("jobs", [])]
        if ats.kind == "lever":
            r = _get(f"https://api.lever.co/v0/postings/{ats.a}", params={"mode": "json"})
            if r is None or r.status_code != 200:
                return None
            return [Posting(j.get("text", ""), j.get("hostedUrl", ""), parse_iso_date(j.get("createdAt")),
                            (j.get("categories") or {}).get("location", ""),
                            description=(j.get("descriptionPlain") or "") + "\n\n" + (j.get("additionalPlain") or "")) for j in r.json()]
        if ats.kind == "ashby":
            r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{ats.a}")
            if r is None or r.status_code != 200:
                return None
            return [Posting(j.get("title", ""), j.get("jobUrl", ""), parse_iso_date(j.get("publishedAt")), j.get("location", ""),
                            description=j.get("descriptionPlain") or "") for j in r.json().get("jobs", [])]
        if ats.kind == "smartrecruiters":
            r = _get(f"https://api.smartrecruiters.com/v1/companies/{ats.a}/postings", params={"q": title or q, "limit": 50})
            if r is None or r.status_code != 200:
                return None
            return [Posting(p.get("name", ""), f"https://jobs.smartrecruiters.com/{ats.a}/{p.get('id')}",
                            parse_iso_date(p.get("releasedDate")), (p.get("location") or {}).get("city", ""))
                    for p in r.json().get("content", [])]
    except (ValueError, KeyError, TypeError):
        return None
    return None


def title_score(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    return len(ta & tb) / max(len(ta | tb), 1)


def best_match(postings: list[Posting], titles: list[str], location: str) -> Posting | None:
    loc_toks = {t for t in re.split(r"[^a-z]+", (location or "").lower()) if len(t) >= 4}

    def score(p: Posting):
        ts = max(title_score(p.title, t) for t in titles if t)
        lt = {t for t in re.split(r"[^a-z]+", p.location.lower()) if len(t) >= 4}
        loc = 1 if loc_toks & lt or ("remote" in loc_toks and "remote" in lt) else 0
        return (ts, loc, p.opened or date.min)

    ranked = sorted((p for p in postings if p.url), key=score, reverse=True)
    if ranked and score(ranked[0])[0] >= 0.8:
        return ranked[0]
    return None


# ---------------- link verification ----------------

_GENERIC_PATH = re.compile(r"(^/?$|search|/jobs/?$|/careers/?$|job-listings|results)", re.I)


def _page_text(html_: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return normalize_title(t)


def verify(url: str, titles: list[str]) -> bool:
    return check(url, titles) == "ok"


def check(url: str, titles: list[str]) -> str:
    """'ok' | 'blocked' (site refuses automated requests) | 'bad' (404, redirect to search page, wrong job)."""
    r = _get(url)
    if r is None or r.status_code in (400, 401, 403, 429, 503):
        return "blocked"
    if r.status_code != 200:
        return "bad"
    if _shows_job(r, url, titles):
        return "ok"
    final_path = urlsplit(str(r.url)).path
    if _GENERIC_PATH.search(final_path) and not re.search(r"\d{4,}", str(r.url)):
        return "bad"  # bounced to a search/landing page
    if len(_page_text(r.text[:3_000_000])) < 3000:
        return "blocked"  # JavaScript-only page: nothing to read, can't verify either way
    return "bad"


def _shows_job(r, url: str, titles: list[str]) -> bool:
    final_path = urlsplit(str(r.url)).path
    if _GENERIC_PATH.search(final_path) and not re.search(r"\d{4,}", str(r.url)):
        return False
    text = _page_text(r.text[:3_000_000])
    for t in titles:
        words = [w for w in normalize_title(t).split() if len(w) >= 3]
        core = normalize_title(core_title(t))
        if core and core in text and sum(w in text for w in words) >= 0.6 * len(words):
            return True
    return False


# ---------------- orchestration ----------------

@dataclass
class Links:
    company_url: str = NOT_FOUND
    date_opened: str = "unknown"
    linkedin_url: str = NOT_FOUND
    ats: Ats | None = None
    notes: list[str] = field(default_factory=list)


def _linkedin_from(urls: list[str], company: str) -> str:
    for u in urls:
        if u and "linkedin.com/jobs/view/" in u and matches_company(u, company):
            return canonical_url(u)
    return ""


class LinkResolver:
    """Shared across one run; keeps per-company ATS cache and the SerpApi lookup budget."""

    def __init__(self, serp=None, claude=None, ats_cache: dict[str, str] | None = None, max_lookups: int = 5):
        self.serp = serp                  # SerpApiGoogleJobs (or None)
        self.claude = claude              # llm.Claude (or None)
        self.cache: dict[str, str] = dict(ats_cache or {})   # normalized company -> Ats.dumps() or "none"
        self.new_cache: dict[str, str] = {}
        self.lookups_left = max_lookups

    def _cached_ats(self, company: str) -> Ats | None | str:
        v = self.cache.get(normalize_company(company))
        if v is None:
            return "unknown"
        return None if v == "none" else Ats.loads(v)

    def _remember(self, company: str, ats: Ats | None) -> None:
        key = normalize_company(company)
        val = ats.dumps() if ats else "none"
        if self.cache.get(key) != val:
            self.cache[key] = self.new_cache[key] = val

    def ats_for_company(self, company: str, hint_title: str = "") -> Ats | None:
        """Company name -> its careers system (cached in the sheet). Free board guess first, then Claude once."""
        cached = self._cached_ats(company)
        if cached != "unknown":
            return cached
        ats = guess_board(company)
        if ats is None:
            found = self._claude_careers_url(company, hint_title or "careers")
            ats = detect_ats(found) if found else None
        self._remember(company, ats)
        return ats

    def _serp_lookup(self, title: str, company: str) -> list[str]:
        if self.serp is None or self.lookups_left <= 0:
            return []
        self.lookups_left -= 1
        try:
            data = self.serp._search({"q": f"{core_title(title)} {company}"})
        except Exception as e:
            log.debug("serp lookup failed: %s", type(e).__name__)
            return []
        urls: list[str] = []
        for item in data.get("jobs_results") or []:
            if normalize_company(item.get("company_name", "")) != normalize_company(company):
                continue
            if title_score(item.get("title", ""), title) < 0.6:
                continue
            urls += [o.get("link") for o in item.get("apply_options") or [] if o.get("link")]
        return urls

    def _claude_careers_url(self, company: str, title: str) -> str:
        if self.claude is None:
            return ""
        from . import llm as L
        try:
            if title and title != "careers":
                return L.find_company_posting(self.claude, title, company)
            return L.find_careers_site(self.claude, company)
        except Exception:
            return ""

    def resolve(self, title: str, company: str, location: str, known_urls: list[str],
                board_date: date | None, company_website: str = "", alt_titles: list[str] | None = None) -> Links:
        titles = [t for t in [title, *(alt_titles or [])] if t]
        out = Links()
        domain = _registered_domain(urlsplit(company_website).netloc.lower()) if company_website.startswith("http") else ""
        urls = [u for u in known_urls if u]

        out.linkedin_url = _linkedin_from(urls, company)
        ats = next((a for a in (detect_ats(u, domain) for u in urls if is_company_url(u, company) or detect_ats(u, domain)) if a), None)
        cached = self._cached_ats(company)
        if ats is None and cached not in ("unknown", None):
            ats = cached

        # 2) one fresh Google Jobs lookup for this exact job when something's missing
        if not out.linkedin_url or (ats is None and cached == "unknown"):
            more = self._serp_lookup(title, company)
            if more:
                urls += more
                out.linkedin_url = out.linkedin_url or _linkedin_from(more, company)
                ats = ats or next((a for a in (detect_ats(u, domain) for u in more) if a), None)

        # 3) guess public boards, then (once per company) ask Claude for the careers site
        if ats is None and cached == "unknown":
            ats = guess_board(company)
            if ats is None:
                found = self._claude_careers_url(company, title)
                ats = detect_ats(found, domain) if found else None
                if found and ats is None and is_company_url(found, company) and looks_like_posting(found, titles) and verify(found, titles):
                    out.company_url = found  # custom careers site with no public API: keep the verified page
            self._remember(company, ats)
        elif ats is not None:
            self._remember(company, ats)
        out.ats = ats

        # 4) search the ATS
        if ats is not None:
            postings = search_ats(ats, title)
            if postings is not None:
                m = best_match(postings, titles, location)
                if m:
                    out.company_url = m.url
                    if m.opened:
                        out.date_opened = f"{m.opened.isoformat()} ({ats.kind})"
                else:
                    out.company_url = NOT_ON_SITE
            else:
                out.notes.append(f"{ats.kind} search unavailable")

        # 5) fall back to a company-domain link from the apply options: verified, or (for sites that block
        #    automated requests, e.g. metacareers.com) trusted because Google Jobs listed it for this job
        if out.company_url == NOT_FOUND:
            blocked = ""
            for u in urls:
                if is_company_url(u, company) and looks_like_posting(u, titles):
                    status = check(u, titles)
                    if status == "ok":
                        out.company_url = u
                        break
                    if status == "blocked" and not blocked:
                        blocked = u
            if out.company_url == NOT_FOUND and blocked:
                out.company_url = blocked
                out.notes.append("company site blocks automated checks; link taken from Google Jobs")

        if out.company_url.startswith("http") and out.date_opened == "unknown":
            d = date_posted_from_page(out.company_url)
            if d:
                out.date_opened = f"{d.isoformat()} (company page)"
        if out.date_opened == "unknown" and board_date:
            out.date_opened = f"~{board_date.isoformat()} (job board)"
        out.linkedin_url = out.linkedin_url or NOT_FOUND
        return out
