"""End-to-end pipeline test with an in-memory sheet, fake source and fake Claude."""
from datetime import date

import pytest

from jobscout import llm as L
from jobscout import pipeline
from jobscout.config import ConfigError, Profile
from jobscout.models import JOBS_COLUMNS, LEARNED_HEADER, RawJob, STATE_COLUMNS
from jobscout.sources import SearchPlan, SearchQuery, Source

BRIEF = """Target roles: Forward Deployed Engineer, Solutions Engineer.
Remote in the US. Base salary required, $180k+. Equity-only does NOT count.
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

    monkeypatch.setattr(L, "plan_searches", lambda c, b, n: SearchPlan(queries=[SearchQuery("fde remote")]))

    def judge(c, brief, job):
        calls["judge"] += 1
        bad = "agency" in job.company.lower() or ("sales" in job.title.lower() and "sales-heavy" in brief)
        return L.Judgment(include=not bad, fit_score=8 if not bad else 2, exclusion_reason="agency" if bad else "",
                          why_it_fits="matches", concerns="none", job_title=job.title, location=job.location,
                          work_mode="Remote", employment_type="FT", seniority="Senior", salary_range="not listed",
                          equity_mentioned="unknown")
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
    return calls


class DummyClaude:
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}


P = Profile(name="t")


def test_first_run_adds_second_run_dedups(fake_llm):
    store = FakeStore()
    src = FakeSource([
        J("Forward Deployed Engineer", "Acme", url="https://www.linkedin.com/jobs/view/111111111?trk=a"),
        J("Forward Deployed Engineer", "Acme", url="https://www.linkedin.com/jobs/view/111111111?trk=b"),  # same URL
        J("Sr. Forward Deployed Engineer", "Beta Inc.", url="https://beta.com/1"),
        J("Senior Forward Deployed Engineer", "Beta", url="https://indeed.com/2"),  # same job, other board
        J("Solutions Engineer", "Talent Agency Co", url="https://agency.com/1"),   # excluded by judge
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
    pipeline.run_profile(P, store, DummyClaude(), [FakeSource([J("Solutions Engineer", "Gamma")])])
    status, fb = JOBS_COLUMNS.index("Status"), JOBS_COLUMNS.index("My feedback")
    store.jobs[1][status] = "Not a fit"
    store.jobs[1][fb] = "too sales-heavy"
    snapshot = [list(r) for r in store.jobs]

    src = FakeSource([J("Sales Engineer", "Delta"), J("Solutions Engineer", "Gamma", loc="New York, NY")])
    rep = pipeline.run_profile(P, store, DummyClaude(), [src])
    assert rep.learned == ["Exclude sales-heavy roles"]
    assert LEARNED_HEADER in store.brief and "Exclude sales-heavy roles" in store.brief
    assert store.jobs[: len(snapshot)] == snapshot  # existing rows never edited
    # Gamma/Solutions Engineer was marked Not a fit -> skipped even at another location; Delta sales role excluded
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
    jobs = [J(f"Forward Deployed Engineer {i}", f"Co{i}") for i in range(30)]
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
