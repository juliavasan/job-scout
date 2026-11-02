from datetime import date, timedelta

from jobscout.dedup import canonical_url, job_key
from jobscout.sources.base import parse_relative_date
from jobscout.sources.serpapi_google_jobs import parse_result, pick_best_url


def test_linkedin_urls_canonicalize_to_job_id():
    a = "https://www.linkedin.com/jobs/view/forward-deployed-engineer-at-acme-4012345678?refId=abc&trackingId=xyz"
    b = "https://uk.linkedin.com/jobs/view/4012345678/"
    c = "https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=fde"
    assert canonical_url(a) == canonical_url(b) == canonical_url(c) == "https://linkedin.com/jobs/view/4012345678"


def test_tracking_params_stripped_but_ids_kept():
    assert canonical_url("https://boards.greenhouse.io/acme/jobs/123?utm_source=x&gh_src=y") == "https://boards.greenhouse.io/acme/jobs/123"
    assert canonical_url("https://acme.com/careers?gh_jid=55&utm_campaign=z") == "https://acme.com/careers?gh_jid=55"


def test_job_key_normalizes_company_title_location():
    k1 = job_key("Acme, Inc.", "Sr. Solutions Engineer (Remote)", "Remote, US")
    k2 = job_key("ACME", "Senior Solutions Engineer", "Anywhere")
    assert k1 == k2
    assert job_key("Acme", "Solutions Engineer", "New York, NY") != job_key("Acme", "Solutions Engineer", "San Francisco, CA")


def test_relative_dates():
    ref = date(2026, 11, 2)
    assert parse_relative_date("3 days ago", ref) == ref - timedelta(days=3)
    assert parse_relative_date("5 hours ago", ref) == ref
    assert parse_relative_date("a week ago", ref) == ref - timedelta(days=7)
    assert parse_relative_date("30+ days ago", ref) == ref - timedelta(days=30)
    assert parse_relative_date("Full-time", ref) is None


def test_prefers_linkedin_apply_link():
    opts = [{"title": "Indeed", "link": "https://indeed.com/viewjob?jk=1"},
            {"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/view/123456789"},
            {"title": "Greenhouse", "link": "https://boards.greenhouse.io/acme/jobs/9"}]
    url, src, others = pick_best_url(opts, "https://google.com/share", "Acme")
    assert "linkedin.com" in url and src.startswith("LinkedIn") and len(others) == 2
    url, _, _ = pick_best_url(opts[::2], "", "Acme")
    assert "greenhouse" in url


def test_rejects_other_employers_linkedin_link_and_strips_tracking():
    opts = [{"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/view/senior-substation-project-manager-at-wsp-in-the-u-s-4472815594?utm_campaign=x"},
            {"title": "Trabajo", "link": "https://us.trabajo.org/job-1?utm_source=google_jobs_apply"},
            {"title": "UHG Careers", "link": "https://careers.unitedhealthgroup.com/job/123?utm_medium=organic&id=9"}]
    url, src, others = pick_best_url(opts, "", "UnitedHealth Group")
    assert url == "https://careers.unitedhealthgroup.com/job/123?id=9"
    assert not any("linkedin" in o for o in others)
    ok = "https://www.linkedin.com/jobs/view/senior-tpm-at-unitedhealth-group-4471234567"
    assert pick_best_url([{"title": "LinkedIn", "link": ok}], "", "UnitedHealth Group")[0] == ok


def test_parse_serpapi_result():
    item = {
        "title": "Forward Deployed Engineer", "company_name": "Acme", "location": "Anywhere",
        "description": "Build things.", "job_highlights": [{"title": "Qualifications", "items": ["Python"]}],
        "detected_extensions": {"posted_at": "2 days ago", "salary": "180K–220K a year", "work_from_home": True},
        "apply_options": [{"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/view/987654321"},
                          {"title": "Acme", "link": "https://acme.com/jobs/1"}],
    }
    j = parse_result(item)
    assert j.url.endswith("987654321") and j.other_urls == ["https://acme.com/jobs/1"]
    assert j.salary_hint and j.work_mode_hint == "Remote" and "Python" in j.description and j.posted_date


def test_bad_location_falls_back_and_bad_query_is_isolated():
    from jobscout.sources import SearchPlan, SearchQuery
    from jobscout.sources.serpapi_google_jobs import SerpApiGoogleJobs
    calls = []

    class Src(SerpApiGoogleJobs):
        def _search(self, params):
            calls.append(params)
            if params.get("location") == "Los Gatos, CA":
                raise RuntimeError('SerpApi HTTP 400: {"error": "Unsupported `Los Gatos, CA` location - location parameter."}')
            if params["q"] == "boom":
                raise RuntimeError("SerpApi HTTP 500")
            return {"jobs_results": [{"title": "TPM", "company_name": "Netflix", "location": "Los Gatos, CA",
                                      "apply_options": [{"title": "Netflix", "link": "https://jobs.netflix.com/1"}]}]}

    src = Src("k", max_queries=3, client=object())
    jobs = src.fetch(SearchPlan(queries=[SearchQuery("tpm", "Los Gatos, CA"), SearchQuery("boom"), SearchQuery("tpm remote")]), 7)
    assert len(jobs) == 2 and len(src.errors) == 1
    assert {"q": "tpm Los Gatos, CA"} in calls


def test_linkedin_resolve_order():
    from jobscout import linkedin
    assert linkedin.resolve(None, "TPM", "Acme", "", ["https://www.linkedin.com/jobs/view/tpm-at-acme-4012345678?trk=x"]) == \
        ("https://linkedin.com/jobs/view/4012345678", "posting")
    url, kind = linkedin.resolve(None, "Senior TPM", "Acme Inc", "", ["https://acme.com/jobs/1"])
    assert kind == "search" and url == "https://www.linkedin.com/jobs/search/?keywords=Senior+TPM+Acme&f_TPR=r2592000"
    assert linkedin.search_url("Remote - Senior Technical Program Manager", "NVIDIA Corporation") == \
        "https://www.linkedin.com/jobs/search/?keywords=Senior+Technical+Program+Manager+NVIDIA&f_TPR=r2592000"
    assert "Remote" not in linkedin.search_url("Senior TPM, Services Operations - Remote", "SentinelOne")

    class FakeClaude: pass
    import jobscout.llm as L
    orig, orig_flag = L.find_linkedin_posting, linkedin.SEARCH_ENABLED
    linkedin.SEARCH_ENABLED = True
    try:
        L.find_linkedin_posting = lambda c, t, co, loc: "https://www.linkedin.com/jobs/view/senior-tpm-at-other-co-4000000001"
        assert linkedin.resolve(FakeClaude(), "Senior TPM", "Acme", "", [])[1] == "search"  # wrong employer rejected
        L.find_linkedin_posting = lambda c, t, co, loc: "https://www.linkedin.com/jobs/view/senior-tpm-at-acme-4000000002"
        assert linkedin.resolve(FakeClaude(), "Senior TPM", "Acme", "", []) == ("https://linkedin.com/jobs/view/4000000002", "found")
    finally:
        L.find_linkedin_posting, linkedin.SEARCH_ENABLED = orig, orig_flag
