"""Google Drive / Docs / Sheets access via a service account.

You share ONE folder with the service account's email; it sees nothing else.

    Job Scout/                 <- root folder (its ID goes in JOBSCOUT_ROOT_FOLDER_ID)
      julia/                   <- one subfolder per person = one profile
        Brief                  <- Google Doc (or .txt) with the search brief
        Jobs                   <- Google Sheet (you create it empty; tabs are added automatically)
      friend/
        ...

Service accounts can't own files in your Drive, so you create the Doc and the
Sheet by hand; the agent only edits them.

Everything is append-only: the agent appends rows to the sheet and appends
"Learned exclusions" lines to the end of the brief; it never edits your rows.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass

import gspread
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

from .models import (
    JOBS_COLUMNS, LEARNED_HEADER, LOG_COLUMNS, RUNLOG_COLUMNS, RUNLOG_TAB, STATE_COLUMNS, STATUS_VALUES,
    TAB_JOBS, TAB_LOG, TAB_STATE,
)

log = logging.getLogger(__name__)

# Drive scope covers the Sheets and Docs APIs too. The service account can only
# reach files/folders explicitly shared with its email.
SCOPES = ["https://www.googleapis.com/auth/drive"]

DRIVE = "https://www.googleapis.com/drive/v3"
DOCS = "https://docs.googleapis.com/v1"
MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_DOC = "application/vnd.google-apps.document"
MIME_SHEET = "application/vnd.google-apps.spreadsheet"
TEXT_MIMES = ("text/plain", "text/markdown", "text/x-markdown")
MIME_PDF = "application/pdf"
MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

BRIEF_TEMPLATE = """About me (paste your resume or a short summary — used to judge whether you're a strong candidate):
- (e.g. 8 years as a software engineer, last 3 building LLM features at a Series C startup;
  Python, TypeScript, AWS; led customer integrations for enterprise clients; based in Oakland, CA)

Target roles:
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

Companies to watch (optional, one per line — their own career sites are searched directly):
# Netflix
# https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
# greenhouse:anthropic

"""


class GoogleSetupError(RuntimeError):
    pass


# ---------- auth ----------

def credentials_from_service_account(info: dict) -> Credentials:
    return Credentials.from_service_account_info(info, scopes=SCOPES)


# ---------- Drive helpers ----------

class Drive:
    def __init__(self, creds: Credentials):
        self.creds = creds
        self.http = AuthorizedSession(creds)
        self.gc = gspread.authorize(creds)

    def _check(self, r, what: str):
        if r.status_code >= 400:
            hint = ""
            if r.status_code == 403 and ("has not been used" in r.text or "is disabled" in r.text):
                hint = " — enable this API in your Google Cloud project (README step 2)"
            elif r.status_code in (403, 404):
                hint = " — is the 'Job Scout' folder shared with the service account email as Editor?"
            raise GoogleSetupError(f"{what}: HTTP {r.status_code}{hint}")
        return r

    def get(self, file_id: str) -> dict:
        r = self.http.get(f"{DRIVE}/files/{file_id}", params={"fields": "id,name,mimeType,trashed", "supportsAllDrives": "true"})
        return self._check(r, "open Drive item").json()

    def children(self, folder_id: str) -> list[dict]:
        out, token = [], None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": "nextPageToken, files(id,name,mimeType,modifiedTime)",
                "pageSize": 200, "orderBy": "name",
                "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            data = self._check(self.http.get(f"{DRIVE}/files", params=params), "list folder").json()
            out += data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                return out

    # --- brief file (Google Doc or plain text) ---
    def read_text(self, f: dict) -> str:
        if f["mimeType"] == MIME_DOC:
            r = self.http.get(f"{DRIVE}/files/{f['id']}/export", params={"mimeType": "text/plain"})
        else:
            r = self.http.get(f"{DRIVE}/files/{f['id']}", params={"alt": "media", "supportsAllDrives": "true"})
        self._check(r, "read brief")
        return r.content.decode("utf-8-sig").replace("\r\n", "\n")

    def download(self, f: dict) -> bytes:
        r = self.http.get(f"{DRIVE}/files/{f['id']}", params={"alt": "media", "supportsAllDrives": "true"})
        return self._check(r, f"download {f['name']}").content

    def resume_text(self, f: dict) -> str | None:
        """Plain text of a resume file (Google Doc, PDF, .docx, .txt/.md). None if unsupported."""
        mime, name = f["mimeType"], f["name"].lower()
        if mime == MIME_DOC or mime in TEXT_MIMES or name.endswith((".txt", ".md")):
            return self.read_text(f)
        if mime == MIME_PDF or name.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(self.download(f)))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        if mime == MIME_DOCX or name.endswith(".docx"):
            return docx_text(self.download(f))
        return None

    def append_text(self, f: dict, text: str) -> None:
        if f["mimeType"] == MIME_DOC:
            doc = self._check(self.http.get(f"{DOCS}/documents/{f['id']}", params={"fields": "body(content(endIndex))"}), "open brief doc").json()
            end = doc["body"]["content"][-1]["endIndex"] - 1
            body = {"requests": [{"insertText": {"location": {"index": max(1, end)}, "text": text}}]}
            self._check(self.http.post(f"{DOCS}/documents/{f['id']}:batchUpdate", json=body), "append to brief doc")
        else:
            current = self.read_text(f)
            r = self.http.patch(f"https://www.googleapis.com/upload/drive/v3/files/{f['id']}",
                                params={"uploadType": "media", "supportsAllDrives": "true"},
                                data=(current + text).encode(), headers={"Content-Type": "text/plain; charset=utf-8"})
            self._check(r, "append to brief file")


def docx_text(data: bytes) -> str:
    """Text of a .docx without extra dependencies (paragraphs from word/document.xml)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    import html as _html
    return re.sub(r"\n{3,}", "\n\n", _html.unescape(text)).strip()


# ---------- profile discovery ----------

@dataclass
class ProfileFiles:
    name: str
    folder_id: str
    brief: dict | None
    sheet: dict | None
    resumes_folder: dict | None = None
    log_sheet: dict | None = None


def _pick(files: list[dict], prefer: tuple[str, ...]) -> dict | None:
    if not files:
        return None
    for word in prefer:
        for f in files:
            if word in f["name"].lower():
                return f
    return files[0] if len(files) == 1 else None


def find_profile_files(drive: Drive, name: str, folder_id: str) -> ProfileFiles:
    kids = drive.children(folder_id)
    briefs = [f for f in kids if f["mimeType"] == MIME_DOC or f["mimeType"] in TEXT_MIMES or f["name"].lower().endswith((".txt", ".md"))]
    all_sheets = [f for f in kids if f["mimeType"] == MIME_SHEET]
    log_sheet = next((f for f in all_sheets if re.search(r"\blogs?\b", f["name"], re.I)), None)
    sheets = [f for f in all_sheets if f is not log_sheet]
    brief = _pick(briefs, ("brief", "search", "looking", "criteria", "what"))
    if briefs and brief is None:
        raise GoogleSetupError(f"folder {name!r} has several text files; name the one to use 'Brief'")
    sheet = _pick(sheets, ("job",))
    if sheets and sheet is None:
        raise GoogleSetupError(f"folder {name!r} has several sheets; put 'Jobs' in the name of the one to use")
    resumes = next((f for f in kids if f["mimeType"] == MIME_FOLDER and re.search(r"resume|résumé|\bcv", f["name"], re.I)), None)
    return ProfileFiles(name=name, folder_id=folder_id, brief=brief, sheet=sheet, resumes_folder=resumes, log_sheet=log_sheet)


def discover_profiles(drive: Drive, root_folder_id: str) -> list[tuple[str, str]]:
    """Each subfolder of the root folder is a profile: [(folder name, folder id)]."""
    root = drive.get(root_folder_id)
    if root.get("mimeType") != MIME_FOLDER:
        raise GoogleSetupError("JOBSCOUT_ROOT_FOLDER_ID does not point to a folder")
    return [(f["name"].strip(), f["id"]) for f in drive.children(root_folder_id)
            if f["mimeType"] == MIME_FOLDER and not f["name"].startswith(("_", "."))]


# ---------- sheet formatting ----------

def ensure_sheet_layout(gc: gspread.Client, sheet_id: str) -> list[str]:
    """Create/repair Jobs, Log and hidden _state tabs. Never overwrites existing content."""
    sh = gc.open_by_key(sheet_id)
    done: list[str] = []
    existing = {ws.title: ws for ws in sh.worksheets()}

    # A brand-new sheet has one empty default tab ("Sheet1"); reuse it for Jobs.
    default = None
    if len(existing) == 1:
        (only,) = existing.values()
        if only.title not in (TAB_JOBS, TAB_LOG, TAB_STATE) and not any(any(r) for r in only.get_all_values()):
            default = only

    def ensure(title: str, cols: int) -> gspread.Worksheet:
        nonlocal default
        if title in existing:
            return existing[title]
        if default is not None:
            ws, default = default, None
            existing.pop(ws.title)
            ws.update_title(title)
            ws.resize(rows=1000, cols=cols)
            done.append(f"renamed default tab -> {title}")
        else:
            ws = sh.add_worksheet(title=title, rows=1000, cols=cols)
            done.append(f"created tab {title}")
        existing[title] = ws
        return ws

    jobs = ensure(TAB_JOBS, len(JOBS_COLUMNS))
    logws = ensure(TAB_LOG, len(LOG_COLUMNS))
    state = ensure(TAB_STATE, len(STATE_COLUMNS))

    fresh = False
    for ws, header in ((jobs, JOBS_COLUMNS), (logws, LOG_COLUMNS), (state, STATE_COLUMNS)):
        first = ws.row_values(1)
        if not any(first):
            ws.update([header], "A1", value_input_option="RAW")
            done.append(f"wrote {ws.title} header")
            fresh = fresh or ws is jobs
        elif first[: len(header)] == header:
            pass
        elif header[: len(first)] == first:  # older layout: add the new columns at the end
            missing = header[len(first):]
            if ws.col_count < len(header):
                ws.add_cols(len(header) - ws.col_count)
            start = gspread.utils.rowcol_to_a1(1, len(first) + 1)
            ws.update([missing], start, value_input_option="RAW")
            done.append(f"added {ws.title} columns: {', '.join(missing)}")
        else:
            done.append(f"WARNING: {ws.title} header differs from expected; left as is")

    if not fresh and not done:
        return done  # already set up; don't re-apply formatting every run

    status_col = JOBS_COLUMNS.index("Status")
    url_col = JOBS_COLUMNS.index("Job URL")
    requests = [
        *[{"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                                     "fields": "gridProperties.frozenRowCount"}} for ws in (jobs, logws, state)],
        *[{"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                          "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                          "fields": "userEnteredFormat.textFormat.bold"}} for ws in (jobs, logws, state)],
        {"setDataValidation": {
            "range": {"sheetId": jobs.id, "startRowIndex": 1, "startColumnIndex": status_col, "endColumnIndex": status_col + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in STATUS_VALUES]},
                     "strict": True, "showCustomUi": True}}},
        {"repeatCell": {"range": {"sheetId": jobs.id, "startRowIndex": 1, "startColumnIndex": url_col, "endColumnIndex": url_col + 2},
                        "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP"}}, "fields": "userEnteredFormat.wrapStrategy"}},
        {"updateSheetProperties": {"properties": {"sheetId": state.id, "hidden": True}, "fields": "hidden"}},
    ]
    sh.batch_update({"requests": requests})
    done.append("formatted (frozen headers, Status dropdown, hidden _state)")
    return done


def setup_profile(drive: Drive, name: str, folder_id: str) -> tuple[ProfileFiles, list[str]]:
    """Check the folder has a brief + a sheet, and lay out the sheet's tabs. Never overwrites content."""
    pf = find_profile_files(drive, name, folder_id)
    missing = []
    if pf.brief is None:
        missing.append("a Google Doc named 'Brief' (paste the template from the README)")
    if pf.sheet is None:
        missing.append("an empty Google Sheet named 'Jobs'")
    if missing:
        raise GoogleSetupError(f"folder {name!r} needs " + " and ".join(missing))
    done = ensure_sheet_layout(drive.gc, pf.sheet["id"])
    if pf.log_sheet:
        done += ensure_runlog_layout(drive.gc, pf.log_sheet["id"])
    return pf, done


def ensure_runlog_layout(gc: gspread.Client, sheet_id: str) -> list[str]:
    """Prepare the optional 'Log' spreadsheet: a 'Runs' tab with headers. Never overwrites content."""
    sh = gc.open_by_key(sheet_id)
    tabs = sh.worksheets()
    done: list[str] = []
    ws = next((w for w in tabs if w.title == RUNLOG_TAB), None)
    if ws is None:
        if len(tabs) == 1 and not any(any(r) for r in tabs[0].get_all_values()):
            ws = tabs[0]
            ws.update_title(RUNLOG_TAB)
        else:
            ws = sh.add_worksheet(title=RUNLOG_TAB, rows=1000, cols=len(RUNLOG_COLUMNS))
        done.append(f"Log sheet: created '{RUNLOG_TAB}' tab")
    first = ws.row_values(1)
    if not any(first):
        if ws.col_count < len(RUNLOG_COLUMNS):
            ws.add_cols(len(RUNLOG_COLUMNS) - ws.col_count)
        ws.update([RUNLOG_COLUMNS], "A1", value_input_option="RAW")
        sh.batch_update({"requests": [
            {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat.bold"}},
        ]})
        done.append("Log sheet: wrote header")
    elif RUNLOG_COLUMNS[: len(first)] == first and len(first) < len(RUNLOG_COLUMNS):
        missing = RUNLOG_COLUMNS[len(first):]
        if ws.col_count < len(RUNLOG_COLUMNS):
            ws.add_cols(len(RUNLOG_COLUMNS) - ws.col_count)
        ws.update([missing], gspread.utils.rowcol_to_a1(1, len(first) + 1), value_input_option="RAW")
        done.append(f"Log sheet: added columns {', '.join(missing)}")
    return done


# ---------- the store used by the pipeline ----------

class DriveProfileStore:
    """Brief = a doc/text file; Jobs/Log/_state = tabs in the profile's sheet."""

    def __init__(self, drive: Drive, files: ProfileFiles):
        self.drive = drive
        self.files = files
        self.sh = drive.gc.open_by_key(files.sheet["id"])

    # brief
    def read_brief(self) -> str:
        return self.drive.read_text(self.files.brief)

    def append_to_brief(self, text: str) -> None:
        self.drive.append_text(self.files.brief, text)

    def read_resumes(self) -> list[tuple[str, str]]:
        """[(file name, text)] from the person's Resumes folder, newest first. Empty if no folder."""
        folder = self.files.resumes_folder
        if not folder:
            return []
        files = sorted(self.drive.children(folder["id"]), key=lambda f: f.get("modifiedTime", ""), reverse=True)
        out = []
        for f in files:
            if f["mimeType"] == MIME_FOLDER:
                continue
            try:
                text = self.drive.resume_text(f)
            except Exception as e:
                log.warning("could not read a resume file: %s", type(e).__name__)
                continue
            if text and text.strip():
                out.append((f["name"], text.strip()))
        return out

    # sheet
    def _ws(self, title: str) -> gspread.Worksheet:
        return self.sh.worksheet(title)

    def read_jobs(self) -> list[dict]:
        values = self._ws(TAB_JOBS).get_all_values()
        if not values:
            return []
        header = values[0]
        out = []
        for n, row in enumerate(values[1:], start=2):
            if any(c.strip() for c in row):
                d = dict(zip(header, row + [""] * (len(header) - len(row))))
                d["_row"] = n
                out.append(d)
        return out

    def read_flagged_rows(self) -> set[int]:
        """Sheet row numbers you highlighted orange/red (any cell) = "not for me", even without a written reason."""
        r = self.drive.http.get(f"https://sheets.googleapis.com/v4/spreadsheets/{self.files.sheet['id']}",
                                params={"ranges": f"{TAB_JOBS}!A2:{gspread.utils.rowcol_to_a1(1, len(JOBS_COLUMNS))[:-1]}5000",
                                        "includeGridData": "true",
                                        "fields": "sheets(data(startRow,rowData(values(effectiveFormat/backgroundColorStyle))))"})
        if r.status_code != 200:
            return set()
        flagged = set()
        for data in r.json().get("sheets", [{}])[0].get("data", []):
            start = data.get("startRow", 1)
            for i, row in enumerate(data.get("rowData", [])):
                for v in row.get("values", []):
                    c = ((v.get("effectiveFormat") or {}).get("backgroundColorStyle") or {}).get("rgbColor") or {}
                    red, green, blue = c.get("red", 0), c.get("green", 0), c.get("blue", 0)
                    if red >= 0.8 and green <= 0.75 and blue <= 0.45:  # orange / red / dark-orange fills
                        flagged.add(start + i + 1)
                        break
        return flagged

    def append_jobs(self, rows: list[list[str]]) -> None:
        if rows:
            # RAW so text from postings is never interpreted as a formula.
            self._ws(TAB_JOBS).append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS", table_range="A1")

    def fill_empty_cells(self, column: str, values: dict[int, str], replace: tuple[str, ...] = ()) -> int:
        """Write values into EMPTY cells of one agent-owned column (or cells holding one of the agent's own
        placeholders listed in `replace`). values: {sheet row number: value}."""
        ws = self._ws(TAB_JOBS)
        header = ws.row_values(1)
        col = header.index(column) + 1
        current = ws.col_values(col)
        def writable(r: int) -> bool:
            return r > len(current) or not current[r - 1].strip() or current[r - 1].strip() in replace

        cells = [gspread.Cell(r, col, v) for r, v in values.items() if v and writable(r)]
        if cells:
            ws.update_cells(cells, value_input_option="RAW")
        return len(cells)

    def append_run_log(self, row: list) -> None:
        """Detailed run row in the optional separate 'Log' spreadsheet (no-op if there isn't one)."""
        if not self.files.log_sheet:
            return
        ws = self.drive.gc.open_by_key(self.files.log_sheet["id"]).worksheet(RUNLOG_TAB)
        # USER_ENTERED so numbers/cost are real numbers you can sum; values are all ours (no posting text).
        ws.append_row(row, value_input_option="USER_ENTERED", table_range="A1")

    def append_log(self, row: list) -> None:
        self._ws(TAB_LOG).append_row([str(c) for c in row], value_input_option="RAW", table_range="A1")

    def read_state(self) -> list[dict]:
        values = self._ws(TAB_STATE).get_all_values()
        return [dict(zip(STATE_COLUMNS, r + [""] * (len(STATE_COLUMNS) - len(r)))) for r in values[1:]] if values else []

    LOCK_MINUTES = 30

    def acquire_lock(self, owner: str) -> bool:
        """Simple run lock (cells F1:G1 of the hidden _state tab) so a local run and the cloud run never overlap."""
        from datetime import datetime, timedelta, timezone

        ws = self._ws(TAB_STATE)
        if ws.col_count < 7:
            ws.add_cols(7 - ws.col_count)
        cur = ws.get("F1:G1")
        if cur and cur[0] and len(cur[0]) >= 2 and cur[0][1]:
            try:
                since = datetime.fromisoformat(cur[0][1])
                if datetime.now(timezone.utc) - since < timedelta(minutes=self.LOCK_MINUTES):
                    return False
            except ValueError:
                pass
        ws.update([[f"running: {owner}", datetime.now(timezone.utc).isoformat(timespec="seconds")]], "F1:G1", value_input_option="RAW")
        return True

    def release_lock(self) -> None:
        self._ws(TAB_STATE).update([["", ""]], "F1:G1", value_input_option="RAW")

    def append_state(self, rows: list[list[str]]) -> None:
        if rows:
            self._ws(TAB_STATE).append_rows(rows, value_input_option="RAW", table_range="A1")
