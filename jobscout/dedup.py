"""Canonical URLs and normalized (company, title, location) keys for dedup."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that actually identify a job (everything else is tracking noise).
_ID_PARAMS = {"gh_jid", "jk", "currentjobid", "jobid", "job_id", "id"}

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|gmbh|plc|pbc)\b\.?"
)
_TITLE_SYNONYMS = [
    (r"\bsr\b\.?", "senior"),
    (r"\bjr\b\.?", "junior"),
    (r"\bmgr\b", "manager"),
    (r"\beng\b", "engineer"),
    (r"\bpm\b", "product manager"),
    (r"\bii\b", "2"),
    (r"\biii\b", "3"),
]


def canonical_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = parts.netloc.lower().split("@")[-1].split(":")[0]
    host = host.removeprefix("www.")
    if host.endswith(".linkedin.com"):  # de.linkedin.com, uk.linkedin.com, ...
        host = "linkedin.com"
    path = re.sub(r"/+$", "", parts.path) or ""

    if host == "linkedin.com":
        # /jobs/view/some-title-slug-1234567890  ->  /jobs/view/1234567890
        m = re.search(r"/jobs/view/(?:[^/]*?-)?(\d{6,})", path)
        if m:
            return f"https://linkedin.com/jobs/view/{m.group(1)}"
        q = dict((k.lower(), v) for k, v in parse_qsl(parts.query))
        if q.get("currentjobid"):
            return f"https://linkedin.com/jobs/view/{q['currentjobid']}"

    kept = sorted((k.lower(), v) for k, v in parse_qsl(parts.query) if k.lower() in _ID_PARAMS)
    return urlunsplit(("https", host, path, urlencode(kept), ""))


def _clean(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_company(company: str) -> str:
    return re.sub(r"\s+", " ", _COMPANY_SUFFIXES.sub(" ", _clean(company))).strip()


def normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"\([^)]*\)", " ", t)  # "(Remote)", "(m/f/d)"
    t = re.sub(r"\([^)]*$", " ", t)     # unclosed "(Enterprise Modernization & AI Delivery" (truncated by some sites)
    for pat, rep in _TITLE_SYNONYMS:
        t = re.sub(pat, rep, t)
    return _clean(t)


def normalize_location(location: str) -> str:
    loc = _clean(location)
    if not loc or "remote" in loc or "anywhere" in loc:
        return "remote" if loc else ""
    # "San Francisco, CA, United States" -> "san francisco"
    return _clean((location or "").split(",")[0])


def job_key(company: str, title: str, location: str) -> str:
    return "|".join([normalize_company(company), normalize_title(title), normalize_location(location)])


def company_title_key(company: str, title: str) -> str:
    return "|".join([normalize_company(company), normalize_title(title)])
