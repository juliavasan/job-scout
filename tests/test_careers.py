from datetime import date

from jobscout import careers as C


def test_detect_ats():
    assert C.detect_ats("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/TPM_JR1") == \
        C.Ats("workday", "nvidia.wd5.myworkdayjobs.com", "NVIDIAExternalCareerSite")
    assert C.detect_ats("https://boards.greenhouse.io/acme/jobs/123") == C.Ats("greenhouse", "acme")
    assert C.detect_ats("https://job-boards.greenhouse.io/acme/jobs/123") == C.Ats("greenhouse", "acme")
    assert C.detect_ats("https://jobs.lever.co/mistral/abc") == C.Ats("lever", "mistral")
    assert C.detect_ats("https://jobs.ashbyhq.com/openai/abc") == C.Ats("ashby", "openai")
    assert C.detect_ats("https://explore.jobs.netflix.net/careers/job/790313749011", "netflix.com") == \
        C.Ats("eightfold", "explore.jobs.netflix.net", "netflix.com")
    assert C.detect_ats("https://www.indeed.com/viewjob?jk=1") is None
    a = C.Ats("workday", "x.wd1.myworkdayjobs.com", "Site")
    assert C.Ats.loads(a.dumps()) == a


def test_best_match_prefers_title_then_location_then_recent():
    ps = [C.Posting("Senior Customer Technical Program Manager - AI Datacenter", "https://w/1", date(2026, 9, 1), "Japan, Tokyo"),
          C.Posting("Senior Customer Technical Program Manager - AI Datacenter", "https://w/2", date(2026, 10, 30), "US, CA, Santa Clara"),
          C.Posting("Datacenter Technical Program Manager", "https://w/3", date(2026, 10, 31), "US, CA, Santa Clara")]
    m = C.best_match(ps, ["Senior Customer Technical Program Manager - AI Datacenter"], "Santa Clara, CA")
    assert m.url == "https://w/2"
    assert C.best_match(ps, ["Staff Product Manager, Ads"], "") is None


def test_resolver_finds_company_posting_and_flags_closed(monkeypatch):
    monkeypatch.setattr(C, "guess_board", lambda company: None)
    monkeypatch.setattr(C, "date_posted_from_page", lambda url: None)
    monkeypatch.setattr(C, "verify", lambda url, titles: True)
    monkeypatch.setattr(C, "search_ats", lambda ats, title: [
        C.Posting("Senior Technical Program Manager", "https://acme.wd1.myworkdayjobs.com/Ext/job/X_JR1", date(2026, 10, 29), "Remote")])
    r = C.LinkResolver()
    lk = r.resolve("Senior Technical Program Manager", "Acme", "Remote",
                   ["https://acme.wd1.myworkdayjobs.com/Ext/job/X_JR1", "https://www.linkedin.com/jobs/view/sr-tpm-at-acme-4012345678"],
                   None)
    assert lk.company_url == "https://acme.wd1.myworkdayjobs.com/Ext/job/X_JR1"
    assert lk.date_opened == "2026-10-29 (workday)"
    assert lk.linkedin_url == "https://linkedin.com/jobs/view/4012345678"
    assert r.new_cache == {"acme": C.Ats("workday", "acme.wd1.myworkdayjobs.com", "Ext").dumps()}

    # ATS known (cached) but the job isn't there -> likely closed
    monkeypatch.setattr(C, "search_ats", lambda ats, title: [C.Posting("Chef", "https://acme.wd1.myworkdayjobs.com/Ext/job/C", None)])
    lk2 = r.resolve("Senior Technical Program Manager", "Acme", "Remote", ["https://www.tealhq.com/job/x"], date(2026, 10, 28))
    assert lk2.company_url == C.NOT_ON_SITE and lk2.date_opened == "~2026-10-28 (job board)" and lk2.linkedin_url == C.NOT_FOUND


def test_serp_lookup_adds_linkedin_and_ats(monkeypatch):
    monkeypatch.setattr(C, "guess_board", lambda company: None)
    monkeypatch.setattr(C, "date_posted_from_page", lambda url: None)
    monkeypatch.setattr(C, "search_ats", lambda ats, title: [C.Posting("AI Product Manager, Content Platform", "https://explore.jobs.netflix.net/careers/job/790300000001", date(2026, 10, 20), "Los Gatos")])

    class Serp:
        searches_used = 0

        def _search(self, params):
            self.searches_used += 1
            return {"jobs_results": [{"title": "AI Product Manager, Content Platform", "company_name": "Netflix",
                                      "apply_options": [{"link": "https://www.linkedin.com/jobs/view/ai-product-manager-at-netflix-4400000001"},
                                                        {"link": "https://explore.jobs.netflix.net/careers/job/790300000001"}]}]}

    r = C.LinkResolver(serp=Serp(), max_lookups=1)
    lk = r.resolve("AI Product Manager, Content Platform", "Netflix", "Los Gatos, CA", ["https://www.mediabistro.com/jobs/1"], None,
                   company_website="https://netflix.com")
    assert lk.linkedin_url == "https://linkedin.com/jobs/view/4400000001"
    assert lk.company_url == "https://explore.jobs.netflix.net/careers/job/790300000001"
    assert lk.date_opened == "2026-10-20 (eightfold)" and r.lookups_left == 0


def test_verify_rejects_redirect_to_search_and_wrong_title(monkeypatch):
    class R:
        def __init__(self, url, text, status=200):
            self.url, self.text, self.status_code = url, text, status
    pages = {
        "https://co.com/job/eden-prairie/technical-project-manager": R("https://co.com/search-jobs", "<h1>5881 results</h1>"),
        "https://co.com/job/123456/senior-tpm": R("https://co.com/job/123456/senior-tpm", "<h1>Senior Technical Program Manager</h1> Remote"),
        "https://co.com/job/999999/other": R("https://co.com/job/999999/other", "<h1>Data Engineer</h1>"),
    }
    monkeypatch.setattr(C, "_get", lambda url, **kw: pages[url])
    t = ["Senior Technical Program Manager, Services Operations - Remote"]
    assert not C.verify("https://co.com/job/eden-prairie/technical-project-manager", t)
    assert C.verify("https://co.com/job/123456/senior-tpm", t)
    assert not C.verify("https://co.com/job/999999/other", t)


def test_parse_watchlist_section_only():
    from jobscout.sources import parse_watchlist
    brief = """Target roles: TPM
Companies I'm especially interested in: Netflix
Companies to watch (one per line — a company name, a careers URL, or greenhouse:slug):
- Netflix
# greenhouse:anthropic
https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
lever:mistral

=== Learned exclusions (auto-added) ===
- [2026-11-03] Exclude sales roles
"""
    assert parse_watchlist(brief) == ["Netflix", "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", "lever:mistral"]


def test_company_sites_source(monkeypatch):
    from jobscout.sources import CompanySites, SearchPlan, SearchQuery
    ats = C.Ats("eightfold", "explore.jobs.netflix.net", "netflix.com")

    class Res:
        def ats_for_company(self, company, hint_title=""):
            return ats
    monkeypatch.setattr(C, "search_ats", lambda a, q: [
        C.Posting("Senior Technical Program Manager, Studio", "https://explore.jobs.netflix.net/careers/job/1", date.today(), "Los Gatos", detail="h|1"),
        C.Posting("Chef", "https://explore.jobs.netflix.net/careers/job/2", date.today(), "Los Gatos", detail="h|2")])
    monkeypatch.setattr(C, "fetch_details", lambda a, p: (setattr(p, "description", "full JD"), p)[1])
    plan = SearchPlan(queries=[SearchQuery("tpm")], title_keywords=["technical program manager"], ats_boards=["Netflix"])
    jobs = CompanySites(Res()).fetch(plan, 7)
    assert [j.title for j in jobs] == ["Senior Technical Program Manager, Studio"]
    assert jobs[0].company == "Netflix" and jobs[0].description == "full JD" and "careers site (eightfold)" in jobs[0].source
