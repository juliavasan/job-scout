# Job Scout

A small agent that runs every morning. It finds new job postings, judges each one against a
**free-text brief** you write, looks up basic facts about the company, and appends the good
ones to **your Google Sheet**, ready to triage over coffee. It supports several people, each
with their own brief and sheet.

```
Brief (in your sheet) ─┐
"Not a fit" feedback ──┼─► learned exclusions ─► Claude writes search queries
                       │                              │
                       │       SerpApi Google Jobs (LinkedIn, Indeed, company sites…)
                       │       + optional Greenhouse / Lever / Ashby boards
                       │                              │
                       │              dedup (URL → company+title+location)
                       │                              │
                       └──────────► Claude judges fit (JSON, validated) ─► company facts (web search)
                                                      │
                                       append ≤25 rows to Jobs, 1 row to Log
```

- **No LinkedIn login, no cookies, no scraping.** LinkedIn postings come in through Google for
  Jobs (via [SerpApi](https://serpapi.com/google-jobs-api)). When a posting has a LinkedIn link,
  that link goes in the sheet.
- **Your private data never enters this repo.** Briefs, salary expectations and feedback live
  in your sheet. The repo holds only code. Spreadsheet IDs and API keys are GitHub Secrets.
- **Append-only.** The agent never edits or deletes your rows. It only appends rows, plus the
  dated "Learned exclusions" lines at the bottom of your brief.

## Your sheet

| Tab | What's in it |
|---|---|
| `Brief` | **A1**: your search brief, editable from your phone. **B2** (optional): ATS watchlist, one board per line, e.g. `greenhouse:anthropic`, `lever:mistral`, `ashby:openai` |
| `Jobs` | One row per job: Date added · Fit score · Job title · Company · Why it fits · Concerns · Location · Work mode · Employment type · Seniority · Salary range · Equity mentioned · Posted date · Company type/industry · Company size · Funding stage · HQ · Company description · Job URL · Company website · Source · **Status** · **My feedback** |
| `Log` | One row per run: found / added / skipped (duplicate, excluded, over cap) / new learned exclusions / errors |
| `_state` (hidden) | Bookkeeping: which feedback has been processed, and which postings were already judged and rejected (so they aren't re-judged daily). Safe to clear. |

Company facts come from a web search by Claude. Anything it can't confirm is written as `unknown`, never guessed.

## Setup (one time, ~20 minutes)

### 1. Install

```bash
git clone https://github.com/<you>/job-scout && cd job-scout
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env              # fill in keys below
cp profiles.example.yaml profiles.yaml
```

### 2. API keys

- **Anthropic**: create a key at <https://console.anthropic.com/settings/keys> and put it in `.env` as `ANTHROPIC_API_KEY`.
- **SerpApi**: sign up at <https://serpapi.com> (the free plan gives 250 searches a month) and put the key in `.env` as `SERPAPI_API_KEY`.
  By default each profile uses up to 4 searches per run, so 2 people × 4 × 30 days = 240 a month. Lower
  `max_queries` in `profiles.yaml` if you add more people, or upgrade (Starter is $25/mo for 1,000 searches).

### 3. Google service account (lets the agent run unattended)

1. Go to <https://console.cloud.google.com/projectcreate> and create a project (e.g. `job-scout`).
2. Enable the **Google Sheets API**: <https://console.cloud.google.com/apis/library/sheets.googleapis.com>, then click **Enable**.
3. Go to **IAM & Admin → Service Accounts** (<https://console.cloud.google.com/iam-admin/serviceaccounts>), click **Create service account**,
   name it `job-scout`, then **Create and continue** → skip roles → **Done**.
4. Click the new account → **Keys** → **Add key → Create new key → JSON**. A `.json` file downloads.
5. Move it to `./secrets/service-account.json`. The `secrets/` folder is gitignored, and `.env.example` already points there.
6. Copy the account's email (`job-scout@<project>.iam.gserviceaccount.com`). You'll share each sheet with it.

> If your Google Workspace org blocks service-account key creation (policy
> `iam.disableServiceAccountKeyCreation`), use a personal Google account's project instead.

### 4. Create a sheet for each person

1. Create a blank Google Sheet. Click **Share**, add the service-account email as **Editor**, and untick "Notify".
2. Copy the sheet ID from the URL: `docs.google.com/spreadsheets/d/`**`<THIS_PART>`**`/edit`.
3. Run:
   ```bash
   python -m jobscout init-sheet --sheet-id <THIS_PART>
   ```
   This creates the `Brief`, `Jobs` and `Log` tabs (plus a hidden `_state` tab), writes the headers, freezes them, adds the Status dropdown and puts a brief template in `Brief!A1`.
   You can run it again safely: it never overwrites existing content.
4. Add the sheet to `profiles.yaml`:
   ```yaml
   profiles:
     julia:
       sheet_id: "<THIS_PART>"
   ```
5. Write the brief in `Brief!A1` (see below), then test:
   ```bash
   python -m jobscout run --profile julia --dry-run   # prints what it would add; writes nothing
   python -m jobscout run --profile julia             # writes to the sheet
   python -m jobscout run --profile all
   ```

### 5. Schedule it (GitHub Actions)

In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Anthropic key |
| `SERPAPI_API_KEY` | your SerpApi key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of the service-account `.json` file |
| `PROFILES_YAML` | the **entire contents** of your `profiles.yaml` |

Or from a terminal with the GitHub CLI:

```bash
gh secret set ANTHROPIC_API_KEY
gh secret set SERPAPI_API_KEY
gh secret set GOOGLE_SERVICE_ACCOUNT_JSON < secrets/service-account.json
gh secret set PROFILES_YAML < profiles.yaml
```

`.github/workflows/daily.yml` runs `--profile all` every day. Each profile runs on its own, so one person's
failure doesn't block the others. You can also start a run by hand: **Actions → Daily job scout → Run workflow**
(it takes a profile name and has a dry-run checkbox).

**About the time.** GitHub's cron runs on UTC and **does not follow daylight saving time**. The schedule is
`0 14 * * *`, which is **6:00 AM PST** in winter and **7:00 AM PDT** in summer. To get 6 AM in summer instead,
switch to `0 13 * * *` (which means 5 AM in winter). GitHub can also start scheduled runs 5–30 minutes late,
and it pauses schedules in repos with no activity for 60 days. A push or a manual run re-enables them.

**Public logs.** This repo is public, so its Actions logs are too. In CI the agent logs only counts and error
types, never your brief, the search queries or job details.

## Adding a person (5 minutes)

1. They create a blank sheet and share it with the service-account email (Editor).
2. `python -m jobscout init-sheet --sheet-id <their sheet id>`
3. They write their brief in `Brief!A1`.
4. Add `name: { sheet_id: ... }` to `profiles.yaml`, then update the secret: `gh secret set PROFILES_YAML < profiles.yaml`.

Optional settings for each profile in `profiles.yaml`: `max_new_rows` (25), `max_age_days` (7), `min_fit_score` (6),
`max_queries` (4), `pages_per_query` (1).

## Writing a good brief

The brief is passed **word for word** to Claude as the judging criteria, so write it the way you'd brief a
recruiter friend. The template has these sections:

- **Target roles.** List the actual titles, and also say what the work is ("customer-facing engineering,
  building with customers' data"). Claude also turns this section into the search queries.
- **Seniority, location / remote, employment type.** Be specific: "Remote (US), or hybrid ≤2 days in SF".
- **Compensation.** State the hard rules explicitly. For example: *"Base salary required, $180k+. Salary + equity
  is great. Equity-only does NOT count — exclude."* A posting that doesn't list a salary is kept and flagged in
  Concerns, not dropped. A posted range whose top is below your minimum is excluded.
- **Company preferences.** Stage, size, industry, and anything that should raise the score.
- **Don't show me.** Hard exclusions, one per line: "staffing agencies", "on-site 5 days in SF", "crypto".

Tips:
- Say what's a **must** and what's a **nice to have**. Claude scores on the nice-to-haves and excludes on the musts.
- Short bullets work better than long paragraphs.
- If results are too broad, add lines to "Don't show me" or give feedback (below). If they're too narrow, loosen the musts.

## How feedback works

1. In `Jobs`, set **Status** to `Not a fit` and write a reason in **My feedback**, e.g. `too sales-heavy`, `no agencies`, `needs clearance`.
2. On the next run, before searching, the agent reads every `Not a fit` row with feedback it hasn't seen yet.
   Claude turns that feedback into short, general rules (merging duplicates and skipping anything your brief already covers)
   and appends them to the bottom of `Brief!A1`, dated:
   ```
   === Learned exclusions (auto-added from your feedback — edit or delete freely) ===
   - [2026-11-03] Exclude roles that are mostly quota-carrying sales
   ```
3. The brief plus the learned exclusions become the filter for that run and every run after it.
   You can edit or delete any learned line. If you delete one, it won't come back unless you write new feedback that says the same thing.
4. Jobs marked `Not a fit` are never re-added, even if the same company reposts the same title in another city.

Other statuses (`New`, `Interested`, `Applied`) are just for you. The agent sets `New` on the rows it adds.

## Dedup rules

A posting is skipped if any of these is true:
1. Its canonical URL is already in the sheet. Tracking parameters are stripped, and LinkedIn URLs are reduced to `linkedin.com/jobs/view/<id>`.
   Every apply link of a posting is checked, so the same job reached through LinkedIn and through the company's site still matches.
2. Its normalized **company + title + location** is already in the sheet ("Acme, Inc. / Sr. Solutions Engineer (Remote)" = "ACME / Senior Solutions Engineer / Anywhere").
3. Its company + title matches a row you marked `Not a fit`.
4. It was already judged and rejected in an earlier run (recorded in `_state`).

## Sources

The source layer is pluggable. Subclass `jobscout.sources.base.Source`, implement
`fetch(plan, max_age_days) -> list[RawJob]`, and add it to the list in `cli.py`.

| Source | Notes |
|---|---|
| `SerpApiGoogleJobs` (default) | Google for Jobs covers LinkedIn, Indeed, Glassdoor, Built In and company career pages. 10 results per search. Postings older than `max_age_days` are dropped using the "N days ago" metadata. |
| `AtsBoards` | Public Greenhouse / Lever / Ashby JSON boards for companies in your `Brief!B2` watchlist. Free, no key needed. Only titles that match the brief's role keywords are judged. |

Options I decided against: searching `site:linkedin.com/jobs` and fetching the public pages is unreliable (rate limits,
login walls) and goes against LinkedIn's terms. JSearch (RapidAPI) returns similar Google-for-Jobs data and could be added as another `Source`.

## Models and cost

- `claude-sonnet-5-5` judges fit, extracts the fields and researches companies (with web search). `claude-haiku-5-5` writes the search queries.
  Override with `JOBSCOUT_JUDGE_MODEL` / `JOBSCOUT_FAST_MODEL`.
- All outputs use structured JSON and are validated with Pydantic. Postings are treated as data, not instructions.
- Sonnet calls turn on Anthropic's server-side refusal fallback (`fallbacks: "default"`).
- Rough cost: up to 80 postings judged per profile per run. Typically a few cents to ~$0.30 per profile per day, and
  less after the first week, because rejected postings and known companies are remembered.

## Development

```bash
pytest -q
python -m jobscout -v run --profile julia --dry-run
```
