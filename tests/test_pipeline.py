"""End-to-end pipeline test with an in-memory sheet, fake source and fake Claude."""
from datetime import date

import pytest

from jobscout import llm as L
from jobscout import pipeline
from jobscout.config import ConfigError, Profile
from jobscout.models import JOBS_COLUMNS, LEARNED_HEADER, RawJob, STATE_COLUMNS
from jobscout.sources import SearchPlan, SearchQuery, Source

BRIEF = """Target roles: Data Engineer, Analytics Engineer.
Remote in the US. Base salary required, $150k+. Equity-only does NOT count.
Don't show me:
- staffing agencies
"""


class FakeStore:
    def __init__(self, brief=BRIEF):
        self.brief = brief
        self.jobs: list[list[str]] = [JOBS_COLUMNS]
        self.log: list[list] = []
        self.state: list[list[str]] = [STATE_COLUMNS]

    def read_brief(self): return self.brief
    def append_to_brief(self, t): self.brief += t
    def read_resumes(self): return getattr(self, 'resumes', [])
    def read_jobs(self): return [dict(zip(self.jobs[0], r)) for r in self.jobs[1:]]
    def append_jobs(self, rows): self.jobs += [list(r) for r in rows]
    def append_log(self, row): self.log.append(row)
    def read_state(self): return [dict(zip(STATE_COLUMNS, r)) for r in self.state[1:]]
    def append_state(self, rows): self.state += rows


class FakeSource(Source):
    name = "fake"

    def __init__(self, jobs): self.jobs = jobs
    def fetch(self, plan, max_age_days): return list(self.jobs)


def J(title, company, loc="Remote", url=None, **kw):
    return RawJob(title=title, company=company, location=loc, url=url or f"https://x.com/{company}/{title}".replace(" ", "-"),
                  source="fake", description="desc", posted_date=date(2026, 11, 1), **kw)


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    calls = {"judge": 0, "facts": 0, "learn": []}

    monkeypatch.setattr(L, "plan_searches", lambda c, b, n, r=None: SearchPlan(queries=[SearchQuery("fde remote")]))

    def judge(c, brief, job, resumes=None):
        calls["judge"] += 1
        bad = "agency" in job.company.lower() or ("sales" in job.title.lower() and "sales-heavy" in brief)
        return L.Judgment(include=not bad, fit_score=8 if not bad else 2, exclusion_reason="agency" if bad else "",
                          why_it_fits="matches", concerns="none", job_title=job.title, location=job.location,
                          work_mode="Remote", employment_type="FT", seniority="Senior", salary_range="not listed",
                          equity_mentioned="unknown", best_resume="DE resume.pdf")
    monkeypatch.setattr(L, "judge", judge)

    def facts(c, company, job):
        calls["facts"] += 1
        return L.CompanyFacts(company_type_industry="AI", size_employees="51-200", funding_stage_or_public="Series B",
                              hq="SF", one_line_description="Makes AI.", website="https://acme.com")
    monkeypatch.setattr(L, "company_facts", facts)

    def learn(c, brief, feedback):
        calls["learn"].append(feedback)
        return ["Exclude sales-heavy roles"] if any("sales" in f["feedback"] for f in feedback) else []
    monkeypatch.setattr(L, "learn_exclusions", learn)
    monkeypatch.setattr(L, "find_linkedin_posting", lambda c, t, co, loc: "")
    from jobscout import company_posting
    monkeypatch.setattr(company_posting, "resolve", lambda co, titles, urls, d, c=None: ("not found", f"~{d.isoformat()} (job board)" if d else "unknown"))
    return calls


class DummyClaude:
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0}


P = Profile(name="t")


def test_first_run_adds_second_run_dedups(fake_llm):
    store = FakeStore()
    src = FakeSource([
        J("Data Engineer", "Acme", url="https://www.linkedin.com/jobs/view/111111111?trk=a"),
        J("Data Engineer", "Acme", url="https://www.linkedin.com/jobs/view/111111111?trk=b"),  # same URL
        J("Sr. Data Engineer", "Beta Inc.", url="https://beta.com/1"),
        J("Senior Data Engineer", "Beta", url="https://indeed.com/2"),  # same job, other board
        J("Analytics Engineer", "Talent Agency Co", url="https://agency.com/1"),   # excluded by judge
    ])
    rep = pipeline.run_profile(P, store, DummyClaude(), [src])
    assert rep.added == 2 and rep.skipped_dup == 2 and rep.skipped_excluded == 1
    assert len(store.jobs) == 3 and store.jobs[1][JOBS_COLUMNS.index("Status")] == "New"
    assert store.jobs[1][JOBS_COLUMNS.index("Funding stage / public")] == "Series B"
    assert len(store.log) == 1

    judged_before = fake_llm["judge"]
    rep2 = pipeline.run_profile(P, store, DummyClaude(), [src])
    assert rep2.added == 0 and len(store.jobs) == 3
    assert fake_llm["judge"] == judged_before  # rejected agency job remembered in _state, not re-judged
    assert len(store.log) == 2


def test_feedback_creates_learned_exclusion_once_and_rows_untouched(fake_llm):
    store = FakeStore()
    pipeline.run_profile(P, store, DummyClaude(), [FakeSource([J("Analytics Engineer", "Gamma")])])
    status, fb = JOBS_COLUMNS.index("Status"), JOBS_COLUMNS.index("My feedback")
    store.jobs[1][status] = "Not a fit"
    store.jobs[1][fb] = "too sales-heavy"
    snapshot = [list(r) for r in store.jobs]

    src = FakeSource([J("Sales Engineer", "Delta"), J("Analytics Engineer", "Gamma", loc="New York, NY")])
    rep = pipeline.run_profile(P, store, DummyClaude(), [src])
    assert rep.learned == ["Exclude sales-heavy roles"]
    assert LEARNED_HEADER in store.brief and "Exclude sales-heavy roles" in store.brief
    assert store.jobs[: len(snapshot)] == snapshot  # existing rows never edited
    # Gamma/Analytics Engineer was marked Not a fit -> skipped even at another location; Delta sales role excluded
    assert rep.added == 0

    pipeline.run_profile(P, store, DummyClaude(), [FakeSource([])])
    assert len(fake_llm["learn"]) == 1  # same feedback is not re-processed
    assert store.brief.count("Exclude sales-heavy roles") == 1


def test_dry_run_writes_nothing():
    store = FakeStore()
    rep = pipeline.run_profile(P, store, DummyClaude(), [FakeSource([J("FDE", "Acme")])], dry_run=True)
    assert rep.added == 1 and len(store.jobs) == 1 and not store.log and len(store.state) == 1


def test_cap_respected():
    store = FakeStore()
    jobs = [J(f"Data Engineer {i}", f"Co{i}") for i in range(30)]
    rep = pipeline.run_profile(Profile(name="t", max_new_rows=25), store, DummyClaude(), [FakeSource(jobs)])
    assert rep.added == 25 and rep.skipped_over_cap == 5


def test_template_brief_rejected():
    from jobscout.google_io import BRIEF_TEMPLATE
    with pytest.raises(ConfigError):
        pipeline.run_profile(P, FakeStore(BRIEF_TEMPLATE), DummyClaude(), [])


def test_strict_schema_shape():
    s = L._strict_schema(L.Judgment)
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])


def test_learned_addition_appends_header_once():
    a = pipeline.learned_addition("my brief", ["Exclude X"], "2026-11-03")
    assert a.startswith("\n\n" + LEARNED_HEADER) and a.endswith("- [2026-11-03] Exclude X\n")
    b = pipeline.learned_addition("my brief" + a, ["Exclude Y"], "2026-11-04")
    assert LEARNED_HEADER not in b and b == "- [2026-11-04] Exclude Y\n"


def test_best_resume_column(fake_llm):
    store = FakeStore()
    store.resumes = [("DE resume.pdf", "Data engineer, 5 years")]
    pipeline.run_profile(P, store, DummyClaude(), [FakeSource([J("FDE", "Acme")])])
    assert store.jobs[1][JOBS_COLUMNS.index("Best resume")] == "DE resume.pdf"
    assert len(store.jobs[1]) == len(JOBS_COLUMNS)
    store2 = FakeStore()  # no library -> 'none'
    pipeline.run_profile(P, store2, DummyClaude(), [FakeSource([J("FDE", "Acme")])])
    assert store2.jobs[1][JOBS_COLUMNS.index("Best resume")] == "none"


def test_resume_block_and_braces_safe():
    block = L.format_resumes([("a.pdf", "skills {python}")])
    assert '<resume name="a.pdf">' in block and "{python}" in block
    system = L.JUDGE_SYSTEM.replace("{brief}", "brief with {braces}").replace("{resumes}", block)
    assert "{braces}" in system and "<resumes>" in system


def test_docx_text():
    import io, zipfile
    from jobscout.google_io import docx_text
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Jane &amp; Co</w:t></w:r></w:p><w:p><w:r><w:t>Python</w:t></w:r></w:p></w:body></w:document>")
    assert docx_text(buf.getvalue()) == "Jane & Co\nPython"


def test_run_log_line():
    from datetime import datetime
    from jobscout.cli import format_run_line
    rep = pipeline.RunReport(profile="alex", found=16, added=5, skipped_excluded=11)
    line = format_run_line(datetime(2026, 11, 2, 21, 46), "alex", "run", rep, searches=5,
                           usage={"calls": 22, "input_tokens": 47286, "output_tokens": 6987, "web_searches": 9, "cost_usd": 0.27}, seconds=81)
    assert line == ("2026-11-02 21:46 UTC | alex | run | found 16 | added 5 | dup 0 | excluded 11 | over cap 0 | learned 0 | errors 0"
                    " | serpapi 5 | claude 22 calls, 47.3k in / 7.0k out, 9 web searches, ~$0.27 | 81s")


def test_linkedin_column_filled(fake_llm):
    store = FakeStore()
    pipeline.run_profile(P, store, DummyClaude(), [FakeSource([J("FDE", "Acme", url="https://www.linkedin.com/jobs/view/fde-at-acme-4012345678"),
                                                               J("DE", "Beta", url="https://beta.com/1")])])
    li = {r[JOBS_COLUMNS.index("Company")]: r[JOBS_COLUMNS.index("LinkedIn URL")] for r in store.jobs[1:]}
    assert li["Acme"] == "https://linkedin.com/jobs/view/4012345678"
    assert li["Beta"] == "not found"  # no search links any more: exact posting or "not found"


def test_run_log_row_shape():
    from datetime import datetime
    from jobscout.cli import run_log_row
    from jobscout.models import RUNLOG_COLUMNS
    rep = pipeline.RunReport(profile="alex", found=16, added=5, skipped_excluded=11)
    row = run_log_row(datetime(2026, 11, 2, 21, 46), "run", rep, 5,
                      {"calls": 22, "input_tokens": 47286, "output_tokens": 6987, "web_searches": 9, "cost_usd": 0.2712}, 81.4)
    assert len(row) == len(RUNLOG_COLUMNS)
    d = dict(zip(RUNLOG_COLUMNS, row))
    assert d["Est. cost (USD)"] == 0.27 and d["Added"] == 5 and d["Duration (s)"] == 81 and d["Type"] == "run"


def test_resolver_used_and_ats_cached(fake_llm):
    from jobscout.careers import Links

    class FakeResolver:
        def __init__(self):
            self.cache, self.new_cache, self.calls = {}, {}, 0

        def resolve(self, title, company, location, urls, d, company_website="", alt_titles=None):
            self.calls += 1
            self.new_cache["acme"] = '["greenhouse", "acme", ""]'
            return Links(company_url="https://boards.greenhouse.io/acme/jobs/123456", date_opened="2026-10-29 (greenhouse)",
                         linkedin_url="https://linkedin.com/jobs/view/4012345678")

    store, res = FakeStore(), FakeResolver()
    pipeline.run_profile(P, store, DummyClaude(), [FakeSource([J("FDE", "Acme")])], resolver=res)
    row = dict(zip(JOBS_COLUMNS, store.jobs[1]))
    assert row["Company job URL"].endswith("/123456") and row["Date opened"].startswith("2026-10-29")
    assert row["LinkedIn URL"] == "https://linkedin.com/jobs/view/4012345678"
    assert ["ats", "acme"] == store.state[-1][:2] or any(r[0] == "ats" for r in store.state[1:])


def test_same_company_title_with_remote_variant_is_duplicate():
    store = FakeStore()
    pipeline.run_profile(P, store, DummyClaude(), [FakeSource([J("Strategic Partnerships, Data Engineer", "Vandelay", loc="Springfield, OR (Flexible or Remote)", url="https://a.com/1")])])
    rep = pipeline.run_profile(P, store, DummyClaude(), [FakeSource([J("Strategic Partnerships, Data Engineer", "VANDELAY", loc="Springfield", url="https://b.com/2"),
                                                                   J("Strategic Partnerships, Data Engineer", "Vandelay", loc="London, UK", url="https://c.com/3")])])
    assert rep.skipped_dup == 1 and rep.added == 1  # Springfield copy is a duplicate; London is a different posting


def test_ignore_checkbox_learns_and_never_readds(fake_llm):
    store = FakeStore()
    pipeline.run_profile(P, store, DummyClaude(), [FakeSource([J("Analytics Engineer", "Gamma", url="https://g.com/1")])])
    store.jobs[1][JOBS_COLUMNS.index("Ignore")] = "TRUE"
    store.jobs[1][JOBS_COLUMNS.index("My feedback")] = "too sales-heavy"
    rep = pipeline.run_profile(P, store, DummyClaude(), [FakeSource([J("Analytics Engineer", "Gamma", loc="Remote", url="https://other.com/9")])])
    assert rep.learned == ["Exclude sales-heavy roles"] and rep.added == 0
    assert len(store.jobs) == 2  # never re-added, even from another site
