"""One run for one profile: feedback -> search -> dedup -> judge -> enrich -> append."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import linkedin
from .careers import NOT_FOUND, LinkResolver
from . import llm as L
from .config import ConfigError, Profile
from .dedup import canonical_url, company_title_key, job_key, normalize_company
from .models import (
    LEARNED_HEADER, NOT_A_FIT, STATE_ATS, STATE_FEEDBACK, STATE_REJECTED, RawJob,
)
from .sources import SearchPlan, Source, parse_watchlist

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
MAX_TO_JUDGE = 80
WORKERS = 4
_LEARNED_RE = re.compile(r"^=+\s*Learned exclusions.*$", re.I | re.M)
UNKNOWN_FACTS = L.CompanyFacts(company_type_industry="unknown", size_employees="unknown", funding_stage_or_public="unknown",
                               hq="unknown", one_line_description="unknown", website="unknown")


@dataclass
class RunReport:
    profile: str
    found: int = 0
    added: int = 0
    skipped_dup: int = 0
    skipped_excluded: int = 0
    skipped_over_cap: int = 0
    learned: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    excluded_samples: list[tuple[str, str, str]] = field(default_factory=list)  # (title, company, reason) for dry-run output
    raw_jobs: list = field(default_factory=list)  # everything the sources returned (saved locally for --replay)

    def log_row(self) -> list:
        return [
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), self.found, self.added, self.skipped_dup,
            self.skipped_excluded, self.skipped_over_cap, len(self.learned), "; ".join(self.errors)[:1000],
        ]


# ---------- brief helpers ----------

def validate_brief(brief: str) -> None:
    body = _LEARNED_RE.split(brief)[0].strip()
    if len(body) < 40:
        raise ConfigError("the Brief doc is empty or too short — write your search brief there first")
    if "$XXXk" in body or "(e.g. Forward Deployed" in body:
        raise ConfigError("the Brief doc still contains the template placeholders — fill it in first")


def learned_addition(brief: str, rules: list[str], day: str) -> str:
    """Text to append at the end of the brief (adds the section header the first time)."""
    if not rules:
        return ""
    lines = "\n".join(f"- [{day}] {r}" for r in rules)
    if _LEARNED_RE.search(brief):
        return ("" if brief.endswith("\n") else "\n") + lines + "\n"
    return "\n\n" + LEARNED_HEADER + "\n" + lines + "\n"


_LOC_STOP = {"ca", "usa", "us", "united", "states", "america", "the", "of", "or", "and", "flexible", "hybrid", "onsite", "office", "other", "others"}


def _loc_words(location: str) -> set[str]:
    import re as _re
    return {w for w in _re.findall(r"[a-z]+", (location or "").lower()) if len(w) > 2 and w not in _LOC_STOP}


def _row_keys(row: dict) -> tuple[str, str]:
    return canonical_url(row.get("Job URL", "")), job_key(row.get("Company", ""), row.get("Job title", ""), row.get("Location", ""))


# ---------- main ----------

def run_profile(
    profile: Profile,
    store,
    claude: L.Claude,
    sources: list[Source],
    dry_run: bool = False,
    resolver: LinkResolver | None = None,
    replay: bool = False,
) -> RunReport:
    """replay=True: re-judge previously fetched postings (sources are replay sources) against the current brief,
    ignoring the sheet's history so every posting is judged again. Always a dry run."""
    dry_run = dry_run or replay
    rep = RunReport(profile=profile.name)
    day = datetime.now(LOCAL_TZ).date().isoformat()

    brief = store.read_brief()
    validate_brief(brief)
    try:
        resumes = store.read_resumes()
    except Exception as e:  # the resume library is optional
        resumes = []
        rep.errors.append(f"resumes: {type(e).__name__}: {str(e)[:120]}")
    resume_names = {n for n, _ in resumes}
    log.info("[%s] resume library: %d files", profile.name, len(resumes))
    rows = store.read_jobs()
    state = store.read_state()

    # 1) Feedback loop -> learned exclusions
    #    Signals: Status = "Not a fit" (with or without a reason) or a row highlighted orange/red.
    flagged = getattr(store, "read_flagged_rows", lambda: set())()
    processed = {s["Key"] for s in state if s.get("Kind") == STATE_FEEDBACK}
    pending, pending_keys = [], []
    for r in rows:
        fb = (r.get("My feedback") or "").strip()
        is_flagged = r.get("_row") in flagged
        if (r.get("Status") or "").strip() == NOT_A_FIT or is_flagged:
            r["_not_fit"] = True
            if not fb:
                fb = "(highlighted / marked not for me, no reason written)"
            key = "|".join(_row_keys(r)) + "|" + fb[:200]  # new/edited feedback on the same row counts as new
            if key not in processed:
                pending.append({"title": r.get("Job title", ""), "company": r.get("Company", ""),
                                "location": r.get("Location", ""), "work_mode": r.get("Work mode", ""),
                                "why": r.get("Why it fits", ""), "concerns": r.get("Concerns / red flags", ""),
                                "industry": r.get("Company type / industry", ""), "feedback": fb})
                pending_keys.append(key)
    state_rows: list[list[str]] = []
    if pending and not replay:
        try:
            rep.learned = L.learn_exclusions(claude, brief, pending)
            if rep.learned:
                fresh = store.read_brief()  # re-read right before writing, in case it was edited from a phone
                addition = learned_addition(fresh, rep.learned, day)
                if not dry_run:
                    store.append_to_brief(addition)  # append-only: your text and formatting stay untouched
                brief = fresh + addition
            state_rows += [[STATE_FEEDBACK, k, day, ""] for k in pending_keys]
        except L.LLMError as e:
            rep.errors.append(f"learn: {e}")
    log.info("[%s] feedback rows: %d new, learned %d exclusions", profile.name, len(pending), len(rep.learned))

    # 2) Search
    try:
        plan = L.plan_searches(claude, brief, profile.max_queries, resumes)
    except L.LLMError as e:
        rep.errors.append(f"plan: {e}")
        plan = SearchPlan(queries=[])
    plan.ats_boards = parse_watchlist(brief)  # "Companies to watch" section of the brief
    if not replay:  # let sources skip what we've already seen before applying their per-company caps
        plan.known_urls = {u for r in rows if (u := canonical_url(r.get("Job URL", "")))} | \
                          {u for r in rows if (u := canonical_url(r.get("Company job URL", "")))} | \
                          {s_["Key"] for s_ in state if s_.get("Kind") == STATE_REJECTED}
    raw: list[RawJob] = []
    for src in sources:
        try:
            raw += src.fetch(plan, profile.max_age_days)
            rep.errors += getattr(src, "errors", [])
        except Exception as e:
            rep.errors.append(f"{src.name}: {type(e).__name__}: {str(e)[:150]}")
            log.warning("[%s] source %s failed: %s", profile.name, src.name, type(e).__name__)
    rep.found = len(raw)
    rep.raw_jobs = raw

    # 3) Dedup (URL first, then company+title+location; also anything marked Not a fit or rejected before).
    #    Rows in "Jobs archive" tabs count as already seen.
    archived = [] if replay else getattr(store, "read_archived_jobs", lambda: [])()
    history = [] if replay else rows + archived
    seen_urls = {u for r in history if (u := canonical_url(r.get("Job URL", "")))}
    seen_keys = {job_key(r.get("Company", ""), r.get("Job title", ""), r.get("Location", "")) for r in history}
    not_fit = {company_title_key(r.get("Company", ""), r.get("Job title", "")) for r in rows if r.get("_not_fit")}
    rejected = {s["Key"] for s in state if s.get("Kind") == STATE_REJECTED}
    # company+title -> location words seen, to catch "Santa Clara" vs "Santa Clara (Flexible or Remote)"
    seen_ct: dict[str, set[str]] = {}
    for r in history:
        seen_ct.setdefault(company_title_key(r.get("Company", ""), r.get("Job title", "")), set()).update(_loc_words(r.get("Location", "")))
    candidates: list[RawJob] = []
    for job in raw:
        urls = {canonical_url(u) for u in job.all_urls()} - {""}
        key = job_key(job.company, job.title, job.location)
        ct = company_title_key(job.company, job.title)
        locs = _loc_words(job.location)
        prev = seen_ct.get(ct)
        pure_remote = lambda w: w <= {"remote", "anywhere"}
        same_job_elsewhere = prev is not None and (
            not locs or not prev or bool((locs & prev) - {"remote", "anywhere"}) or pure_remote(locs) or pure_remote(prev))
        if replay:
            if urls & seen_urls or key in seen_keys or same_job_elsewhere:  # only dedup within the replayed batch
                rep.skipped_dup += 1
                continue
        elif urls & seen_urls or key in seen_keys or ct in not_fit or same_job_elsewhere \
                or urls & rejected or key in rejected:
            rep.skipped_dup += 1
            continue
        seen_urls |= urls
        seen_keys.add(key)
        seen_ct.setdefault(ct, set()).update(locs)
        candidates.append(job)
    if len(candidates) > MAX_TO_JUDGE:
        rep.skipped_over_cap += len(candidates) - MAX_TO_JUDGE
        candidates = candidates[:MAX_TO_JUDGE]
    log.info("[%s] found %d, %d new candidates after dedup", profile.name, rep.found, len(candidates))

    # 4) Judge
    def _judge(job: RawJob):
        try:
            return job, L.judge(claude, brief, job, resumes), None
        except Exception as e:
            return job, None, f"{type(e).__name__}: {str(e)[:120]}"

    accepted: list[tuple[RawJob, L.Judgment]] = []
    with ThreadPoolExecutor(WORKERS) as pool:
        for job, j, err in pool.map(_judge, candidates):
            if err:
                rep.errors.append(f"judge: {err}")
                continue
            if j.include and j.fit_score >= profile.min_fit_score:
                accepted.append((job, j))
            else:
                rep.skipped_excluded += 1
                rep.excluded_samples.append((job.title, job.company, j.exclusion_reason or f"score {j.fit_score}"))
                state_rows.append([STATE_REJECTED, canonical_url(job.url) or job_key(job.company, job.title, job.location), day, ""])
    accepted.sort(key=lambda t: -t[1].fit_score)
    if len(accepted) > profile.max_new_rows:
        rep.skipped_over_cap += len(accepted) - profile.max_new_rows
        accepted = accepted[: profile.max_new_rows]

    # 5) Company facts: reuse what's already in the sheet, else look it up once per company
    known: dict[str, L.CompanyFacts] = {}
    for r in rows:
        c = normalize_company(r.get("Company", ""))
        if c and c not in known and r.get("Company size (employees)", "unknown") != "unknown":
            known[c] = L.CompanyFacts(
                company_type_industry=r.get("Company type / industry", "unknown"),
                size_employees=r.get("Company size (employees)", "unknown"),
                funding_stage_or_public=r.get("Funding stage / public", "unknown"),
                hq=r.get("HQ", "unknown"), one_line_description=r.get("Company description", "unknown"),
                website=r.get("Company website", "unknown"),
            )
    todo = {}
    for job, _ in accepted:
        c = normalize_company(job.company)
        if c not in known and c not in todo:
            todo[c] = job

    def _facts(item):
        c, job = item
        try:
            return c, L.company_facts(claude, job.company, job), None
        except Exception as e:
            return c, UNKNOWN_FACTS, f"{type(e).__name__}: {str(e)[:120]}"

    if replay:
        todo = {}  # tuning run: skip paid company research
    with ThreadPoolExecutor(WORKERS) as pool:
        for c, facts, err in pool.map(_facts, todo.items()):
            known[c] = facts
            if err:
                rep.errors.append(f"company: {err}")

    # 6) Links: the posting on the company's own careers site, the exact LinkedIn posting, date opened
    if resolver is not None:
        resolver.cache.update({st["Key"]: st.get("Value", "") for st in state if st.get("Kind") == STATE_ATS and st.get("Value")})

    def _links(item):
        job, j = item
        f = known.get(normalize_company(job.company), UNKNOWN_FACTS)
        if "careers site" in job.source:  # found directly on the company's site: URL and date are exact already
            when = f"{job.posted_date.isoformat()} ({job.source.split('(')[-1].rstrip(')')})" if job.posted_date else "unknown"
            return NOT_FOUND, job.url, when
        try:
            if resolver is not None:
                lk = resolver.resolve(job.title, job.company, job.location, job.all_urls(), job.posted_date,
                                      company_website=job.company_website or f.website, alt_titles=[j.job_title])
                return lk.linkedin_url, lk.company_url, lk.date_opened
        except Exception as e:
            log.debug("link lookup failed: %s", type(e).__name__)
        li = linkedin.posting_from_urls(job.all_urls(), job.company) or NOT_FOUND
        return li, NOT_FOUND, (f"~{job.posted_date.isoformat()} (job board)" if job.posted_date else "unknown")

    with ThreadPoolExecutor(2) as pool:  # gentle on company career sites
        link_info = list(pool.map(_links, accepted))
    if resolver is not None:
        state_rows += [[STATE_ATS, k, day, v] for k, v in resolver.new_cache.items()]

    # 7) Build + append rows
    for (job, j), (li_url, co_url, opened) in zip(accepted, link_info):
        f = known.get(normalize_company(job.company), UNKNOWN_FACTS)
        rep.rows.append([
            "", day, str(j.fit_score), j.job_title or job.title, job.company, j.why_it_fits, j.concerns,
            j.location or job.location, j.work_mode, j.employment_type, j.seniority, j.salary_range,
            j.equity_mentioned, job.posted_date.isoformat() if job.posted_date else "unknown",
            f.company_type_industry, f.size_employees, f.funding_stage_or_public, f.hq, f.one_line_description,
            job.url, job.company_website or f.website, job.source, "New", "",
            j.best_resume if j.best_resume in resume_names else ("none" if not resumes else "unclear"),
            li_url, co_url, opened,
        ])
    rep.added = len(rep.rows)

    if not dry_run:
        store.append_jobs(rep.rows)
        store.append_state(state_rows)
        store.append_log(rep.log_row())
    log.info("[%s] added %d | dup %d | excluded %d | over cap %d | errors %d | claude calls %d (%d in / %d out tokens)",
             profile.name, rep.added, rep.skipped_dup, rep.skipped_excluded, rep.skipped_over_cap, len(rep.errors),
             claude.usage["calls"], claude.usage["input_tokens"], claude.usage["output_tokens"])
    return rep
