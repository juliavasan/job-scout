import json
from datetime import date

from jobscout.careers import _amazon_date, _amazon_locations
from jobscout.sources.company_sites import CompanySites


def test_amazon_entry_and_parsing():
    ats = CompanySites(resolver=None)._ats("amazon: Sunnyvale, Palo Alto")
    assert (ats.kind, ats.a) == ("amazon", "Sunnyvale|Palo Alto")
    assert CompanySites(resolver=None)._ats("Amazon").a == ""
    assert _amazon_date("November  2, 2026") == date(2026, 11, 2) and _amazon_date("") is None
    j = {"normalized_location": "Seattle, Washington, USA", "locations": [
        json.dumps({"city": "Seattle", "normalizedStateName": "Washington"}),
        json.dumps({"city": "Sunnyvale", "normalizedStateName": "California"})]}
    assert _amazon_locations(j) == "Seattle, Washington; Sunnyvale, California"
