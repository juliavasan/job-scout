import json

from jobscout import evaluation as E


def _rows(tmp_path, rows):
    d = tmp_path / "baseline"
    d.mkdir(parents=True)
    (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def test_confusion_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "FLOW_DIR", tmp_path)
    g = lambda keep, fit: {"grade": {"correct": int(keep == fit), "keep": keep, "label_fit": fit}, "latency_s": 2.0}
    _rows(tmp_path, [g(1, 1), g(1, 1), g(1, 0), g(0, 1), g(0, 0), g(0, 0), g(0, 0)])
    s = E.summarize("baseline")
    assert s["confusion"] == {"tp": 2, "fp": 1, "fn": 1, "tn": 3}
    assert s["precision"]["value"] == round(2 / 3, 3) and s["recall"]["value"] == round(2 / 3, 3)
    assert s["specificity"]["value"] == 0.75 and s["accuracy"]["value"] == round(5 / 7, 3)
    assert s["majority_baseline"] == round(4 / 7, 3)
    lo, hi = s["accuracy"]["ci95"]
    assert lo < 5 / 7 < hi


def test_oracle_and_null(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "FLOW_DIR", tmp_path)
    labels = [1, 0, 1, 0, 0]
    _rows(tmp_path, [{"grade": {"correct": 1, "keep": l, "label_fit": l}, "latency_s": 1} for l in labels])  # oracle
    assert E.summarize("baseline")["accuracy"]["value"] == 1.0
    (tmp_path / "baseline" / "results.jsonl").write_text("\n".join(json.dumps(
        {"grade": {"correct": int(0 == l), "keep": 0, "label_fit": l}, "latency_s": 1}) for l in labels))  # null: keep nothing
    s = E.summarize("baseline")
    assert s["recall"]["value"] == 0.0 and s["precision"]["value"] is None


def test_build_cases_dedups_and_numbers():
    from jobscout.eval_cases import build_cases
    cands = [{"title": "Data Engineer", "company": "Acme Inc.", "location": "Austin, TX"},
             {"title": "Data Engineer (Remote)", "company": "ACME", "location": "Austin"},   # same job
             {"title": "Data Engineer", "company": "Acme", "location": "Denver, CO"},       # other city
             {"title": "Analyst", "company": "Beta", "location": "Remote"}]
    cases = build_cases(cands, n=10)
    assert len(cases) == 3
    assert [c["case_id"] for c in cases] == ["c01", "c02", "c03"]
    assert build_cases(cands, n=10) == cases          # fixed seed: same order every time
    assert len(build_cases(cands, n=2)) == 2
