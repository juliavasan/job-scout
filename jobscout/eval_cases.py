"""Build the eval's labelling set: freeze real postings into cases.jsonl and write them to an "Eval" tab for blind labelling.

Cases come from a run's saved postings (logs/candidates_<name>.json, written by every local run) - every posting the
search found, before the judge saw it, so the set holds jobs the agent rejected as well as the ones it kept. That's
what lets the eval measure recall: the Jobs tab alone only ever shows kept jobs.

Labels already given in the Eval tab are carried over for postings that stay in the set (same company, title, city).
"""
from __future__ import annotations

import json
import random
import shutil
from datetime import date
from pathlib import Path

from .dedup import company_title_key
from .evaluation import FLOW_DIR

EVAL_TAB = "Eval"
EVAL_COLUMNS = ["Case", "Your label", "Note (optional)", "Job title", "Company", "Location", "Salary (as posted)",
                "Posted", "Link", "Posting (start)"]
LABEL_CHOICES = ["Fit", "Not fit", "Unsure"]


def _key(c: dict) -> str:
    return company_title_key(c.get("company", ""), c.get("title", "")) + "|" + (c.get("location") or "").split(",")[0].strip().lower()


def build_cases(candidates: list[dict], n: int = 45, seed: int = 7) -> list[dict]:
    """De-duplicate (same company + title + city), shuffle with a fixed seed so the order gives nothing away, keep n."""
    seen, out = set(), []
    for c in candidates:
        k = _key(c)
        if k not in seen:
            seen.add(k)
            out.append(dict(c))
    random.Random(seed).shuffle(out)
    return [dict(c, case_id=f"c{i:02d}") for i, c in enumerate(out[:n], start=1)]


def _row(c: dict, label: str = "", note: str = "") -> list[str]:
    return [c["case_id"], label, note, c.get("title", ""), c.get("company", ""), c.get("location", ""),
            c.get("salary_hint") or "", c.get("posted_date") or "", c.get("url", ""),
            " ".join((c.get("description") or "")[:300].split())]


def write_cases(store, cases: list[dict]) -> int:
    """Save cases.jsonl (backing up the previous set) and rewrite the Eval tab. Returns how many labels were kept."""
    FLOW_DIR.mkdir(parents=True, exist_ok=True)
    path = FLOW_DIR / "cases.jsonl"
    if path.exists():
        shutil.copy(path, FLOW_DIR / f"cases_{date.today().isoformat()}_previous.jsonl")
    old_cases = {c["case_id"]: c for c in map(json.loads, path.read_text().splitlines())} if path.exists() else {}

    try:
        ws = store.sh.worksheet(EVAL_TAB)
        values = ws.get_all_values()
    except Exception:  # gspread.WorksheetNotFound
        ws = store.sh.add_worksheet(title=EVAL_TAB, rows=len(cases) + 5, cols=len(EVAL_COLUMNS))
        values = []
    old_labels = {}
    if values:
        hdr = values[0]
        for r in values[1:]:
            d = dict(zip(hdr, r))
            c = old_cases.get(d.get("Case", ""))
            if c and d.get("Your label", "").strip():
                old_labels[_key(c)] = (d["Your label"].strip(), d.get("Note (optional)", ""))

    rows = [_row(c, *old_labels.get(_key(c), ("", ""))) for c in cases]
    path.write_text("".join(json.dumps(c) + "\n" for c in cases))

    if ws.row_count < len(rows) + 1:
        ws.add_rows(len(rows) + 1 - ws.row_count)
    ws.batch_clear(["A1:J"])
    ws.update([EVAL_COLUMNS] + rows, "A1", value_input_option="RAW")
    store.sh.batch_update({"requests": [
        {"setDataValidation": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": len(rows) + 1, "startColumnIndex": 1, "endColumnIndex": 2},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in LABEL_CHOICES]},
                     "showCustomUi": True, "strict": True}}},
        {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2}},
                                   "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}},
    ]})
    return sum(1 for r in rows if r[1])


def load_candidates(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a list of postings")
    return data
