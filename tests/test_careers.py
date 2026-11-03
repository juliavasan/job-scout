from datetime import date

from jobscout import careers as C


def test_detect_ats():
    assert C.detect_ats("https://initech.wd5.myworkdayjobs.com/en-US/InitechExternal/job/US-CA/DE_JR1") == \
        C.Ats("workday", "initech.wd5.myworkdayjobs.com", "InitechExternal")
    assert C.detect_ats("https://boards.greenhouse.io/acme/jobs/123") == C.Ats("greenhouse", "acme")
    assert C.detect_ats("https://job-boards.greenhouse.io/acme/jobs/123") == C.Ats("greenhouse", "acme")
    assert C.detect_ats("https://jobs.lever.co/acmeai/abc") == C.Ats("lever", "acmeai")
    assert C.detect_ats("https://jobs.ashbyhq.com/piedpiper/abc") == C.Ats("ashby", "piedpiper")
    assert C.detect_ats("https://careers.globex.net/careers/job/790313749011", "globex.com") == \
        C.Ats("eightfold", "careers.globex.net", "globex.com")
    assert C.detect_ats("https://www.indeed.com/viewjob?jk=1") is None
    a = C.Ats("workday", "x.wd1.myworkdayjobs.com", "Site")
    assert C.Ats.loads(a.dumps()) == a


def test_best_match_prefers_title_then_location_then_recent():
    ps = [C.Posting("Senior Platform Engineer - Data Infrastructure", "https://w/1", date(2026, 9, 1), "Japan, Tokyo"),
          C.Posting("Senior Platform Engineer - Data Infrastructure", "https://w/2", date(2026, 10, 30), "US, OR, Springfield"),
          C.Posting("Infrastructure Platform Engineer", "https://w/3", date(2026, 10, 31), "US, OR, Springfield")]
    m = C.best_match(ps, ["Senior Platform Engineer - Data Infrastructure"], "Springfield, OR")
    assert m.url == "https://w/2"
    assert C.best_match(ps, ["Staff Product Manager, Ads"], "") is None


def test_resolver_finds_company_posting_and_flags_closed(monkeypatch):
    monkeypatch.setattr(C, "guess_board", lambda company: None)
    monkeypatch.setattr(C, "date_posted_from_page", lambda url: None)
    monkeypatch.setattr(C, "verify", lambda url, titles: True)
    monkeypatch.setattr(C, "search_ats", lambda ats, title: [
        C.Posting("Senior Data Engineer", "https://acme.wd1.myworkdayjobs.com/Ext/job/X_JR1", date(2026, 10, 29), "Remote")])
    r = C.LinkResolver()
    lk = r.resolve("Senior Data Engineer", "Acme", "Remote",
                   ["https://acme.wd1.myworkdayjobs.com/Ext/job/X_JR1", "https://www.linkedin.com/jobs/view/sr-data-engineer-at-acme-4012345678"],
                   None)
    assert lk.company_url == "https://acme.wd1.myworkdayjobs.com/Ext/job/X_JR1"
    assert lk.date_opened == "2026-10-29 (workday)"
    assert lk.linkedin_url == "https://linkedin.com/jobs/view/4012345678"
    assert r.new_cache == {"acme": C.Ats("workday", "acme.wd1.myworkdayjobs.com", "Ext").dumps()}

    # ATS known (cached) but the job isn't there -> likely closed
    monkeypatch.setattr(C, "search_ats", lambda ats, title: [C.Posting("Chef", "https://acme.wd1.myworkdayjobs.com/Ext/job/C", None)])
    lk2 = r.resolve("Senior Data Engineer", "Acme", "Remote", ["https://www.tealhq.com/job/x"], date(2026, 10, 28))
    assert lk2.company_url == C.NOT_ON_SITE and lk2.date_opened == "~2026-10-28 (job board)" and lk2.linkedin_url == C.NOT_FOUND


def test_serp_lookup_adds_linkedin_and_ats(monkeypatch):
    monkeypatch.setattr(C, "guess_board", lambda company: None)
    monkeypatch.setattr(C, "date_posted_from_page", lambda url: None)
    monkeypatch.setattr(C, "search_ats", lambda ats, title: [C.Posting("Analytics Engineer, Content Platform", "https://careers.globex.net/careers/job/790300000001", date(2026, 10, 20), "Springfield")])

    class Serp:
        searches_used = 0

        def _search(self, params):
            self.searches_used += 1
            return {"jobs_results": [{"title": "Analytics Engineer, Content Platform", "company_name": "Globex",
                                      "apply_options": [{"link": "https://www.linkedin.com/jobs/view/data-engineer-at-globex-4400000001"},
                                                        {"link": "https://careers.globex.net/careers/job/790300000001"}]}]}

    r = C.LinkResolver(serp=Serp(), max_lookups=1)
    lk = r.resolve("Analytics Engineer, Content Platform", "Globex", "Springfield, OR", ["https://www.mediabistro.com/jobs/1"], None,
                   company_website="https://globex.com")
    assert lk.linkedin_url == "https://linkedin.com/jobs/view/4400000001"
    assert lk.company_url == "https://careers.globex.net/careers/job/790300000001"
    assert lk.date_opened == "2026-10-20 (eightfold)" and r.lookups_left == 0


def test_verify_rejects_redirect_to_search_and_wrong_title(monkeypatch):
    class R:
        def __init__(self, url, text, status=200):
            self.url, self.text, self.status_code = url, text, status
    pages = {
        "https://co.com/job/eden-prairie/technical-project-manager": R("https://co.com/search-jobs", "<h1>5881 results</h1>"),
        "https://co.com/job/123456/senior-data-engineer": R("https://co.com/job/123456/senior-data-engineer", "<h1>Senior Data Engineer</h1> Remote"),
        "https://co.com/job/999999/other": R("https://co.com/job/999999/other", "<h1>Data Engineer</h1>"),
    }
    monkeypatch.setattr(C, "_get", lambda url, **kw: pages[url])
    t = ["Senior Data Engineer, Payments - Remote"]
    assert not C.verify("https://co.com/job/eden-prairie/technical-project-manager", t)
    assert C.verify("https://co.com/job/123456/senior-data-engineer", t)
    assert not C.verify("https://co.com/job/999999/other", t)


def test_parse_watchlist_section_only():
    from jobscout.sources import parse_watchlist
    brief = """Target roles: Data Engineer
Companies I'm especially interested in: Globex
Companies to watch (one per line — a company name, a careers URL, or greenhouse:slug):
- Globex
# greenhouse:acme
https://initech.wd5.myworkdayjobs.com/InitechExternal
lever:acmeai

=== Learned exclusions (auto-added) ===
- [2026-11-03] Exclude sales roles
"""
    assert parse_watchlist(brief) == ["Globex", "https://initech.wd5.myworkdayjobs.com/InitechExternal", "lever:acmeai"]


def test_company_sites_source(monkeypatch):
    from jobscout.sources import CompanySites, SearchPlan, SearchQuery
    ats = C.Ats("eightfold", "careers.globex.net", "globex.com")

    class Res:
        def ats_for_company(self, company, hint_title=""):
            return ats
    monkeypatch.setattr(C, "search_ats", lambda a, q: [
        C.Posting("Senior Data Engineer, Studio", "https://careers.globex.net/careers/job/1", date.today(), "Springfield", detail="h|1"),
        C.Posting("Chef", "https://careers.globex.net/careers/job/2", date.today(), "Springfield", detail="h|2")])
    monkeypatch.setattr(C, "fetch_details", lambda a, p: (setattr(p, "description", "full JD"), p)[1])
    plan = SearchPlan(queries=[SearchQuery("de")], title_keywords=["data engineer"], ats_boards=["Globex"])
    jobs = CompanySites(Res()).fetch(plan, 7)
    assert [j.title for j in jobs] == ["Senior Data Engineer, Studio"]
    assert jobs[0].company == "Globex" and jobs[0].description == "full JD" and "careers site (eightfold)" in jobs[0].source


def test_blocked_company_site_link_trusted_from_google(monkeypatch):
    class R:
        status_code, url, text = 400, "https://www.hoolicareers.com/jobs/4815162342/", ""
    monkeypatch.setattr(C, "_get", lambda url, **kw: R())
    monkeypatch.setattr(C, "guess_board", lambda company: None)
    monkeypatch.setattr(C, "date_posted_from_page", lambda url: None)
    lk = C.LinkResolver().resolve("Data Engineer", "Hooli", "Shelbyville, OR",
                                  ["https://www.hoolicareers.com/jobs/4815162342/"], None)
    assert lk.company_url == "https://www.hoolicareers.com/jobs/4815162342/"
    assert C.check("https://x.com/job/123456", ["DE"]) == "blocked"


def test_js_only_page_counts_as_unverifiable(monkeypatch):
    class R:
        status_code, url, text = 200, "https://www.hoolicareers.com/jobs/4815162342/", "<html><div id=root></div></html>"
    monkeypatch.setattr(C, "_get", lambda url, **kw: R())
    assert C.check(R.url, ["Data Engineer"]) == "blocked"


def test_company_sites_skips_known_before_cap(monkeypatch):
    from jobscout.sources import CompanySites, SearchPlan, SearchQuery
    from jobscout.sources import company_sites as cs
    monkeypatch.setattr(cs, "PER_COMPANY", 1)
    ats = C.Ats("greenhouse", "acme")

    class Res:
        def ats_for_company(self, company, hint_title=""):
            return ats
    monkeypatch.setattr(C, "search_ats", lambda a, q: [
        C.Posting("Senior Data Engineer A", "https://boards.greenhouse.io/acme/jobs/1111", date.today(), "US"),
        C.Posting("Senior Data Engineer B", "https://boards.greenhouse.io/acme/jobs/2222", date.today(), "US")])
    monkeypatch.setattr(C, "fetch_details", lambda a, p: p)
    plan = SearchPlan(queries=[SearchQuery("de")], title_keywords=["data engineer"], ats_boards=["Acme"],
                      known_urls={"https://boards.greenhouse.io/acme/jobs/1111"})
    jobs = CompanySites(Res()).fetch(plan, 7)
    assert [j.title for j in jobs] == ["Senior Data Engineer B"]
