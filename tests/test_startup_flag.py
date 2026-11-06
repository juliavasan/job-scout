from jobscout.models import startup_flag


def test_startup_flag():
    assert startup_flag("Series A ($60M, led by a16z, May 2026)", "unknown") == "Yes"
    assert startup_flag("Series A ($60M, 2026; private)", "unknown (sources conflict: ~57-77; 11-200 elsewhere)") == "Yes"
    assert startup_flag("Seed", "11-50") == "Yes"
    assert startup_flag("Private – Series F ($750M, $44B valuation)", "~2,565 (TipRanks, Jul 2026)") == "Yes"
    assert startup_flag("Private – Series I", "~8,500") == "No"           # big late-stage: a scale-up
    assert startup_flag("Public (NASDAQ: NFLX)", "~16,000") == "No"
    assert startup_flag("Public (NASDAQ: GOOGL, GOOG) – subsidiary of Alphabet", "~183,323") == "No"
    assert startup_flag("Private (acquired by ServiceNow, 2025)", "~1,400") == "No"
    assert startup_flag("Private", "120") == "Yes"
    assert startup_flag("Private", "~40,000") == "No"
    assert startup_flag("unknown", "unknown") == "unknown"
