from jobscout.careers import _lever_salary, _with_mode


def test_all_offices_and_work_mode_are_kept():
    # a Bay Area office listed second must not be dropped
    assert _with_mode(["US-CA-Dublin", "US-CA-Menlo Park"], "Hybrid") == "US-CA-Dublin; US-CA-Menlo Park (Hybrid)"
    assert _with_mode(["San Francisco", "San Francisco", ""], "Remote") == "San Francisco (Remote)"
    assert _with_mode(["Austin, TX"], "OnSite") == "Austin, TX (On-site)"
    assert _with_mode(["Austin, TX"], None) == "Austin, TX"
    assert _with_mode([], "remote") == "Remote"


def test_lever_salary():
    assert _lever_salary({"currency": "USD", "min": 180000, "max": 220000, "interval": "per-year-salary"}) == \
        "USD 180,000 - 220,000 per-year-salary"
    assert _lever_salary(None) == "" and _lever_salary({"min": 0}) == ""
