# Job Scout

A small agent that runs in the cloud every morning. It finds new job postings, judges each one
against a **free-text brief** you write, looks up basic facts about the company, and appends the
good ones to a **Google Sheet**, ready to triage over coffee. It supports several people, each with
their own folder, brief and sheet.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for diagrams and design details.

```
Google Drive (shared with a service account)          GitHub Actions, daily
Job Scout/                                             ┌──────────────────────────────────────┐
├── julia/                                             │ read brief + feedback                │
│   ├── Brief   (Google Doc) ◄──── learned rules ───── │ Claude: brief → search queries       │
│   └── Jobs    (Google Sheet) ◄── new rows ────────── │ SerpApi Google Jobs (+ ATS boards)   │
└── friend/                                            │ dedup → Claude judges fit → company  │
    ├── Brief                                          │ facts (web search) → append rows     │
    └── Jobs                                           └──────────────────────────────────────┘
```

- **No LinkedIn login, no cookies, no scraping.** LinkedIn postings come in through Google for Jobs
  (via [SerpApi](https://serpapi.com/google-jobs-api)). When a posting has a LinkedIn link, that link goes in the sheet.
- **Your private data never enters this repo.** Briefs, salary expectations, feedback and results live in your
  Drive. The repo holds only code. Keys and the folder ID are GitHub Secrets.
- **Least privilege.** The agent uses a Google *service account* that can only see the one folder you share with it.
- **Append-only.** The agent never edits or deletes your rows. It only appends rows to the sheet, plus dated
  "Learned exclusions" lines at the end of the brief.

## What's in each person's folder

| File | What it is |
|---|---|
| **Brief** (Google Doc, or a `.txt`) | Your search brief in plain language. Edit it any time, including from your phone. Learned exclusions are added at the bottom. |
| **Jobs** (Google Sheet) | You create it empty. The agent adds three tabs: **Jobs** (results), **Log** (one row per run) and a hidden **_state** tab (bookkeeping). |

**Jobs tab columns:** Date added · Fit score (1–10) · Job title · Company · Why it fits · Concerns / red flags ·
Location · Work mode · Employment type · Seniority · Salary range · Equity mentioned · Posted date ·
Company type / industry · Company size · Funding stage / public · HQ · Company description · Job URL ·
Company website · Source · **Status** (dropdown) · **My feedback**

Company facts come from a web search by Claude. Anything it can't confirm is written as `unknown`, never guessed.

## Setup (one time, ~20 minutes)

### 1. Install

```bash
git clone https://github.com/juliavasan/job-scout && cd job-scout
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

### 2. API keys (put them in `.env`)

- **Anthropic.** Create a key at <https://console.anthropic.com/settings/keys> and put it in `.env` as `ANTHROPIC_API_KEY`.
- **SerpApi.** Sign up at <https://serpapi.com> (the free plan gives 250 searches a month) and copy your key from
  <https://serpapi.com/manage-api-key> into `.env` as `SERPAPI_API_KEY`.
  By default each person uses up to 4 searches per run, so 2 people × 4 × 30 days = 240 a month.

### 3. Google service account (the robot that edits your sheets)

A service account is not a Google account. It's a robot identity with an email address. It can only open
what you share with it. You can create it from your personal Google account or from a separate one.

1. **Create a project:** go to <https://console.cloud.google.com/projectcreate>, name it `job-scout` and click **Create**. Make sure
   it's selected in the project picker at the top of the page.
2. **Enable 3 APIs.** Open each link and click **Enable**:
   - Google Drive API: <https://console.cloud.google.com/apis/library/drive.googleapis.com>
   - Google Sheets API: <https://console.cloud.google.com/apis/library/sheets.googleapis.com>
   - Google Docs API: <https://console.cloud.google.com/apis/library/docs.googleapis.com>
3. **Create the service account:** go to <https://console.cloud.google.com/iam-admin/serviceaccounts>, click
   **+ Create service account** and name it `job-scout`. Click **Create and continue**, skip the roles step, then click **Done**.
4. **Download its key:** click the new account's email, open the **Keys** tab, click **Add key → Create new key → JSON**, then **Create**.
   Move the downloaded file to `secrets/service-account.json` in this project (the `secrets/` folder is gitignored).
5. **Copy the service account's email** (`job-scout@<project>.iam.gserviceaccount.com`). You'll need it next.

> If Google says key creation is disabled by an organization policy, you're using a work/Workspace account.
> Use a personal Google account instead.

### 4. Drive folders

1. In Google Drive, create a folder called **Job Scout**.
2. **Share it** with the service account email as **Editor**, and untick "Notify people".
3. Inside it, create **one subfolder per person** (e.g. `julia`, `alex`). The folder name is the profile name.
4. In each person's subfolder, create:
   - a **Google Doc named `Brief`**. Paste the template from `python -m jobscout template` and fill it in.
   - an **empty Google Sheet named `Jobs`**.
5. Copy the **Job Scout** folder's ID from its URL (`drive.google.com/drive/folders/`**`<THIS_PART>`**) into `.env`
   as `JOBSCOUT_ROOT_FOLDER_ID`.
6. Check everything:
   ```bash
   python -m jobscout setup
   ```
   This lists every person's folder, confirms it can read the brief, and adds the Jobs / Log / _state tabs, headers,
   frozen header row and Status dropdown to each sheet. You can run it again safely: it never overwrites content.

### 5. First run

```bash
python -m jobscout run --profile julia --dry-run   # prints what it would add; writes nothing
python -m jobscout run --profile julia             # writes to the sheet
python -m jobscout run --profile all
```

### 6. Schedule it (GitHub Actions)

Add four repository secrets: **Settings → Secrets and variables → Actions**, or from a terminal:

```bash
gh secret set ANTHROPIC_API_KEY
gh secret set SERPAPI_API_KEY
gh secret set GOOGLE_SERVICE_ACCOUNT_JSON < secrets/service-account.json
gh secret set JOBSCOUT_ROOT_FOLDER_ID
```

`.github/workflows/daily.yml` runs every person in the folder daily. Each person's run is separate, so one failure
doesn't block the others. You can start a run by hand under **Actions → Daily job scout → Run workflow**, or with
`gh workflow run "Daily job scout"`.

**About the time.** GitHub's cron runs on UTC and **does not follow daylight saving time**. The schedule
`0 14 * * *` is **6:00 AM PST** in winter and **7:00 AM PDT** in summer. To get 6 AM in summer instead, use
`0 13 * * *` (which means 5 AM in winter). GitHub can start scheduled runs 5–30 minutes late, and it pauses
schedules in repos with no activity for 60 days. A push or a manual run re-enables them.

**Public logs.** This repo is public, so its Actions logs are too. In CI the agent logs only counts and error
types, and refers to people as `profile-1`, `profile-2`. It never prints briefs, search queries, job details or folder names.

## Adding a person (5 minutes)

1. Create a subfolder for them in **Job Scout**, with a `Brief` doc and an empty `Jobs` sheet.
2. Share their subfolder with them, so they can edit their own brief and sheet.
3. Run `python -m jobscout setup`. That's it. No config change and no secret update; the next daily run includes them.

## Writing a good brief

The brief is passed **word for word** to Claude as the judging criteria, so write it the way you'd brief a
recruiter friend. The template's sections:

- **About me.** Paste your resume, or a few lines on your experience, skills and location. Claude uses it to judge
  whether *you* are a strong candidate. It cites the matching experience in "Why it fits", and when a posting has a hard
  requirement you clearly lack (clearance, license, far more years) it scores the job low and names the gap in Concerns.
- **Target roles.** List the actual titles, and also say what the work is ("customer-facing engineering, building
  with customers' data"). Claude also turns this section into the search queries.
- **Seniority, location / remote, employment type.** Be specific: "Remote (US), or hybrid ≤2 days in SF".
- **Compensation.** State hard rules explicitly. For example: *"Base salary required, $180k+. Salary + equity is
  great. Equity-only does NOT count — exclude."* A posting that doesn't list a salary is kept and flagged in Concerns,
  not dropped. A posted range whose top is below your minimum is excluded.
- **Company preferences.** Stage, size, industry, and anything that should raise the score.
- **Don't show me.** Hard exclusions, one per line: "staffing agencies", "on-site 5 days in SF", "crypto".
- **Companies to watch (optional).** Lines like `greenhouse:anthropic`, `lever:mistral` or `ashby:openai` make the agent also
  check those companies' job boards directly. Lines starting with `#` are ignored.

Tips:
- Say what's a **must** and what's a **nice to have**. Claude excludes on the musts and scores on the nice-to-haves.
- Short bullets work better than long paragraphs.
- If results are too broad, add to "Don't show me" or give feedback (below). If they're too narrow, loosen the musts.

## How feedback works

1. In the **Jobs** tab, set **Status** to `Not a fit` and write a reason in **My feedback**, e.g. `too sales-heavy`, `no agencies`.
2. On the next run, before searching, the agent reads every `Not a fit` row with feedback it hasn't seen yet. Claude turns
   that feedback into short, general rules (merging duplicates and skipping anything your brief already covers) and
   **appends** them, dated, to the end of your Brief doc:
   ```
   === Learned exclusions (auto-added from your feedback — edit or delete freely) ===
   - [2026-11-03] Exclude roles that are mostly quota-carrying sales
   ```
3. The brief plus the learned exclusions become the filter for that run and every run after it. You can edit or delete any
   learned line. If you delete one, it won't come back unless you write new feedback that says the same thing.
4. Jobs marked `Not a fit` are never re-added, even if the same company reposts the same title in another city.

## Dedup rules

A posting is skipped if any of these is true:
1. Its canonical URL is already in the sheet. Tracking parameters are stripped, and LinkedIn URLs are reduced to
   `linkedin.com/jobs/view/<id>`. Every apply link of a posting is checked, so the same job reached through LinkedIn and
   through the company's site still matches.
2. Its normalized **company + title + location** is already in the sheet ("Acme, Inc. / Sr. Solutions Engineer (Remote)" = "ACME / Senior Solutions Engineer / Anywhere").
3. Its company + title matches a row you marked `Not a fit`.
4. It was already judged and rejected in an earlier run (recorded in `_state`, so it isn't paid for twice).

## Design decisions

| Decision | Why |
|---|---|
| **Google Jobs via SerpApi, not LinkedIn scraping** | LinkedIn has no public job-search API, and automated access to logged-in pages breaks its terms and risks the account. Google for Jobs already indexes LinkedIn, Indeed, Glassdoor and company career pages, and SerpApi returns it as structured JSON. |
| **The brief is free text, not a settings form** | Real preferences are messy ("salary + equity is fine, equity-only isn't"). The brief is passed word for word to the model as the rubric, so there's no settings schema to maintain. |
| **Data lives in Drive, not the repo** | The repo is public. Briefs and salary expectations are personal. Drive is also the UI: editable from a phone, with no app to build. |
| **Service account limited to one shared folder** | Unattended runs need a credential that doesn't expire. A service account sees only what's shared with it, and un-sharing revokes access. |
| **Structured outputs + Pydantic** | Every Claude call returns JSON constrained by a schema and validated in code. A response that fails validation is retried once, then reported in the Log. |
| **Cheap filtering before expensive reasoning** | Dedup and "already rejected" checks run before any model call. Company facts are reused across rows. Per-run caps (4 searches, 80 judged, 25 added) keep cost predictable. |
| **Feedback becomes text in the brief, not hidden weights** | Learned rules are readable, dated and editable. You can see exactly why something stopped showing up. |
| **Append-only writes** | The agent never edits your rows. The only change to the brief is an append at the end, and the brief is re-read just before writing so it doesn't overwrite an edit you made from your phone. |
| **Pluggable sources** | A source is one class with one method. Adding a job board doesn't touch dedup, judging or output. |
| **Public-repo hygiene** | Secrets live only in GitHub Secrets. CI logs show counts only and refer to people as `profile-N`. The diff is scanned for secrets before every commit. |

## Sources

| Source | Notes |
|---|---|
| `SerpApiGoogleJobs` (default) | Google for Jobs covers LinkedIn, Indeed, Glassdoor, Built In and company career pages. 10 results per search. Postings older than 7 days are dropped using the "N days ago" metadata. |
| `AtsBoards` | Public Greenhouse / Lever / Ashby JSON boards for the "Companies to watch" in your brief. Free, no key needed. Only titles that match the brief's role keywords are judged. |

To add a source, subclass `jobscout.sources.base.Source`, implement `fetch(plan, max_age_days) -> list[RawJob]`, and add it in `cli.py`.

## Models and cost

- `claude-sonnet-5-5` judges fit, extracts the fields and researches companies (with web search). `claude-haiku-5-5` writes the search queries.
  Override with `JOBSCOUT_JUDGE_MODEL` / `JOBSCOUT_FAST_MODEL`.
- Postings, search results and feedback are treated as data, never as instructions.
- Sonnet calls turn on Anthropic's server-side refusal fallback (`fallbacks: "default"`).
- Rough cost: typically a few cents to ~$0.30 per person per day, and less after the first week, because rejected postings and known companies are remembered.

Optional limits (env vars / secrets): `JOBSCOUT_MAX_NEW_ROWS` (25), `JOBSCOUT_MAX_AGE_DAYS` (7), `JOBSCOUT_MIN_FIT_SCORE` (6),
`JOBSCOUT_MAX_QUERIES` (4), `JOBSCOUT_PAGES_PER_QUERY` (1).

## Development

```bash
pytest -q                                              # unit + end-to-end pipeline tests (fake Drive, fake Claude)
python -m jobscout -v run --profile julia --dry-run
```
