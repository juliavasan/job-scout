"""Google Sheets access (service account). Append-only for your data."""
from __future__ import annotations

import logging

import gspread
from google.oauth2.service_account import Credentials

from .models import (
    JOBS_COLUMNS, LEARNED_HEADER, LOG_COLUMNS, STATE_COLUMNS, STATUS_VALUES,
    TAB_BRIEF, TAB_JOBS, TAB_LOG, TAB_STATE,
)

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

BRIEF_TEMPLATE = """Target roles:
- (e.g. Forward Deployed Engineer, Solutions Engineer, Technical PM for AI products)

Seniority: (e.g. Senior / Staff; no junior roles)
Location / remote: (e.g. Remote in the US, or hybrid in the SF Bay Area at most 2 days/week)
Employment type: (e.g. full-time only)

Compensation:
- Base salary required, $XXXk+ base.
- Salary + equity is great.
- Equity-only (common at early startups) does NOT count — exclude.

Company preferences: (stage, size, industry — e.g. Series B+ or public, 50+ employees, AI/devtools)

Nice to have: (anything that should raise the score)

Don't show me:
- Staffing / recruiting agencies
- Roles requiring on-site 5 days a week in SF
- Crypto / web3

"""

WATCHLIST_HELP = "ATS watchlist (optional) — one per line in B2, e.g. greenhouse:anthropic / lever:mistral / ashby:openai"


def client_from_info(info: dict) -> gspread.Client:
    return gspread.authorize(Credentials.from_service_account_info(info, scopes=SCOPES))


class SheetStore:
    """Thin wrapper over one person's spreadsheet."""

    def __init__(self, gc: gspread.Client, sheet_id: str):
        self.sh = gc.open_by_key(sheet_id)

    def _ws(self, title: str) -> gspread.Worksheet:
        return self.sh.worksheet(title)

    # --- Brief ---
    def read_brief(self) -> str:
        return self._ws(TAB_BRIEF).acell("A1").value or ""

    def read_watchlist(self) -> str:
        return self._ws(TAB_BRIEF).acell("B2").value or ""

    def write_brief(self, text: str) -> None:
        self._ws(TAB_BRIEF).update_acell("A1", text)

    # --- Jobs ---
    def read_jobs(self) -> list[dict]:
        values = self._ws(TAB_JOBS).get_all_values()
        if not values:
            return []
        header = values[0]
        return [dict(zip(header, row + [""] * (len(header) - len(row)))) for row in values[1:] if any(c.strip() for c in row)]

    def append_jobs(self, rows: list[list[str]]) -> None:
        if rows:
            # RAW so text from postings is never interpreted as a formula.
            self._ws(TAB_JOBS).append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS", table_range="A1")

    # --- Log / state ---
    def append_log(self, row: list) -> None:
        self._ws(TAB_LOG).append_row([str(c) for c in row], value_input_option="RAW", table_range="A1")

    def read_state(self) -> list[dict]:
        values = self._ws(TAB_STATE).get_all_values()
        return [dict(zip(STATE_COLUMNS, r)) for r in values[1:]] if values else []

    def append_state(self, rows: list[list[str]]) -> None:
        if rows:
            self._ws(TAB_STATE).append_rows(rows, value_input_option="RAW", table_range="A1")


def init_sheet(gc: gspread.Client, sheet_id: str) -> list[str]:
    """Create/repair the Brief, Jobs, Log and _state tabs. Never overwrites existing content."""
    sh = gc.open_by_key(sheet_id)
    done: list[str] = []
    existing = {ws.title: ws for ws in sh.worksheets()}

    def ensure(title: str, rows: int, cols: int) -> gspread.Worksheet:
        if title in existing:
            return existing[title]
        # Reuse an untouched default "Sheet1" for the first tab we create.
        for name in ("Sheet1", "Лист1", "Feuille 1", "Hoja 1"):
            ws = existing.get(name)
            if ws is not None and not any(any(c for c in r) for r in ws.get_all_values()):
                ws.update_title(title)
                ws.resize(rows=rows, cols=cols)
                existing.pop(name)
                existing[title] = ws
                done.append(f"renamed {name} -> {title}")
                return ws
        ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
        existing[title] = ws
        done.append(f"created tab {title}")
        return ws

    brief = ensure(TAB_BRIEF, 50, 2)
    jobs = ensure(TAB_JOBS, 1000, len(JOBS_COLUMNS))
    logws = ensure(TAB_LOG, 1000, len(LOG_COLUMNS))
    state = ensure(TAB_STATE, 1000, len(STATE_COLUMNS))

    if not (brief.acell("A1").value or "").strip():
        brief.update_acell("A1", BRIEF_TEMPLATE + LEARNED_HEADER + "\n")
        done.append("wrote brief template to Brief!A1")
    if not (brief.acell("B1").value or "").strip():
        brief.update_acell("B1", WATCHLIST_HELP)

    for ws, header in ((jobs, JOBS_COLUMNS), (logws, LOG_COLUMNS), (state, STATE_COLUMNS)):
        first = ws.row_values(1)
        if not any(first):
            ws.update([header], "A1", value_input_option="RAW")
            done.append(f"wrote {ws.title} header")
        elif first[: len(header)] != header:
            done.append(f"WARNING: {ws.title} header differs from expected; left as is")

    status_col = JOBS_COLUMNS.index("Status")
    url_col = JOBS_COLUMNS.index("Job URL")
    requests = [
        # Brief: one wide, wrapped, top-aligned cell
        {"updateDimensionProperties": {"range": {"sheetId": brief.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 900}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": brief.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                                       "properties": {"pixelSize": 320}, "fields": "pixelSize"}},
        {"repeatCell": {"range": {"sheetId": brief.id, "startRowIndex": 0, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": 2},
                        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        # Freeze + bold headers
        *[{"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                                     "fields": "gridProperties.frozenRowCount"}} for ws in (jobs, logws, state)],
        *[{"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                          "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                          "fields": "userEnteredFormat.textFormat.bold"}} for ws in (jobs, logws, state)],
        # Status dropdown on the whole column (below the header)
        {"setDataValidation": {
            "range": {"sheetId": jobs.id, "startRowIndex": 1, "startColumnIndex": status_col, "endColumnIndex": status_col + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in STATUS_VALUES]},
                     "strict": True, "showCustomUi": True}}},
        # Keep long text from blowing up row heights; URLs clip
        {"repeatCell": {"range": {"sheetId": jobs.id, "startRowIndex": 1, "startColumnIndex": url_col, "endColumnIndex": url_col + 2},
                        "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP"}}, "fields": "userEnteredFormat.wrapStrategy"}},
        # Hide the bookkeeping tab
        {"updateSheetProperties": {"properties": {"sheetId": state.id, "hidden": True}, "fields": "hidden"}},
    ]
    sh.batch_update({"requests": requests})
    done.append("formatted tabs (frozen headers, Status dropdown, hidden _state)")
    return done
