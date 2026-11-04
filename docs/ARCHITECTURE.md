# Architecture

Job Scout is a small batch pipeline that runs once a day. It has no server, no database and no UI.
**Each person's Drive folder is their UI and their storage**: a Brief doc to steer the search, and a
Jobs sheet that collects results and feedback. GitHub Actions is the scheduler, and Claude does the reasoning.

## At a glance

![Job Scout architecture](architecture.svg)

## System overview

```mermaid
flowchart LR
    subgraph GH["GitHub Actions — daily cron 14:17 UTC + 2 backup slots"]
        CLI["python -m jobscout run --profile all"]
    end

    subgraph SECRETS["GitHub Secrets"]
        direction TB
        S1["ANTHROPIC_API_KEY"]
        S2["SERPAPI_API_KEY"]
        S3["GOOGLE_SERVICE_ACCOUNT_JSON"]
        S4["JOBSCOUT_ROOT_FOLDER_ID"]
    end

    subgraph DRIVE["Google Drive — 'Job Scout' folder, shared with the service account only"]
        direction TB
        subgraph P1["alex/"]
            B1["Brief (Google Doc)<br/>search brief + learned exclusions"]
            R1["Resumes/ (optional)<br/>resumes already sent:<br/>Doc · PDF · docx · txt"]
            J1["Jobs (Google Sheet)<br/>Jobs · Log · _state tabs"]
        end
        subgraph P2["friend/"]
            B2["Brief"]
            J2["Jobs"]
        end
    end

    subgraph SOURCES["Job sources (pluggable)"]
        SERP["SerpApi Google Jobs<br/>LinkedIn, Indeed, Glassdoor,<br/>company career pages"]
        ATS["Company career sites<br/>Workday · Eightfold · Greenhouse<br/>Lever · Ashby · SmartRecruiters"]
    end

    subgraph CLAUDE["Anthropic API"]
        HAIKU["Haiku 5.5<br/>brief → search queries"]
        SONNET["Sonnet 5.5<br/>judge fit · learn exclusions"]
        WEB["Sonnet 5.5 + web search<br/>company facts"]
    end

    SECRETS -.-> CLI
    CLI <-->|"service account (Drive, Docs, Sheets APIs)<br/>read brief + rows, append rows"| DRIVE
    CLI --> HAIKU
    CLI --> SERP
    CLI --> ATS
    CLI --> SONNET
    CLI --> WEB
    USER(["You, on phone or laptop"]) <-->|"edit brief, set Status,<br/>write feedback"| DRIVE
```

**Profiles are folders.** Every subfolder of the root "Job Scout" folder is one person. To add someone, you add
a folder with a `Brief` doc and an empty `Jobs` sheet. There's no config file and no secret to change.

## One run, step by step

```mermaid
flowchart TD
    A["List subfolders of the root folder → one profile each"] --> A2["Find Brief doc + Jobs sheet;<br/>add missing tabs/headers"]
    A2 --> B["Read the brief, resume library, all Jobs rows, _state"]
    B --> C{"New 'Not a fit' rows<br/>with feedback?"}
    C -- yes --> D["Claude: turn feedback into<br/>general exclusion rules"]
    D --> E["Append dated rules to the end<br/>of the Brief doc"]
    C -- no --> F
    E --> F["Claude Haiku: brief + resumes → 3–4 search queries<br/>+ title keywords"]
    F --> G["Fetch: SerpApi Google Jobs<br/>+ watched companies' own career sites"]
    G --> H["Drop postings older than 7 days<br/>(30 for watched companies)"]
    H --> I["Dedup<br/>1. canonical URL<br/>2. company + title + location<br/>3. company + title marked Not a fit<br/>4. rejected in an earlier run"]
    I --> J["Claude Sonnet: judge each candidate<br/>against brief + learned exclusions + best-matching resume<br/>→ validated JSON incl. best_resume"]
    J --> K{"include AND<br/>score ≥ min?"}
    K -- no --> L["Record in _state as rejected<br/>so it isn't judged again"]
    K -- yes --> M["Sort by score, keep top 25"]
    M --> N["Company facts: reuse from sheet,<br/>else Claude + web search<br/>('unknown' if not found)"]
    N --> N2["Links: find the job on the company's<br/>careers system → exact URL + date opened;<br/>exact LinkedIn posting if Google lists one"]
    N2 --> O["Append rows to Jobs, Status = New"]
    O --> P["Append one row to Log"]
    L --> P
```

## Sequence (one profile)

```mermaid
sequenceDiagram
    autonumber
    participant CLI as jobscout CLI
    participant G as Google Drive / Docs / Sheets
    participant C as Claude API
    participant SA as SerpApi
    participant ATS as ATS boards

    CLI->>G: list profile folder, read Brief doc, Resumes/, Jobs rows, _state
    opt new "Not a fit" feedback
        CLI->>C: learn_exclusions(brief, feedback)
        C-->>CLI: ["Exclude sales-heavy roles", ...]
        CLI->>G: re-read Brief, append dated rules at the end
    end
    CLI->>C: plan_searches(brief) [Haiku]
    C-->>CLI: queries + title keywords
    loop each query (max 4)
        CLI->>SA: google_jobs search
        SA-->>CLI: ~10 postings
    end
    CLI->>ATS: watched companies' career sites (Workday, Eightfold, Greenhouse, ...)
    ATS-->>CLI: matching postings + exact dates + descriptions
    Note over CLI: dedup against sheet + _state
    par up to 4 at a time
        CLI->>C: judge(brief, posting) [Sonnet]
        C-->>CLI: Judgment JSON
    end
    par once per new company
        CLI->>C: company_facts(company) [Sonnet + web_search]
        C-->>CLI: CompanyFacts JSON
    end
    loop each accepted job
        CLI->>ATS: find this job on the company's careers system
        ATS-->>CLI: exact posting URL + date opened (or "not listed")
    end
    CLI->>G: append Jobs rows, _state rows (incl. ATS cache), Log rows
```

## Feedback loop

```mermaid
flowchart LR
    R["Row in Jobs"] -->|"you set Status = Not a fit<br/>+ My feedback = 'too sales-heavy'"| N["Next run"]
    N --> L["Claude generalizes:<br/>'Exclude roles that are mostly<br/>quota-carrying sales'"]
    L --> B["Brief doc gets<br/>- [2026-11-03] Exclude ..."]
    B --> J["Every later judge call<br/>sees the new rule"]
    B -.->|"you can edit or delete it"| B
```

- The feedback key is (row URL, company+title+location, feedback text), stored in `_state`. Each piece of feedback is processed once. Editing the feedback text counts as new feedback.
- If you delete a learned rule, it stays deleted, because the feedback that produced it is already marked processed.
- The brief is updated with an **insert at the end** (Docs API), not a rewrite, so your text and formatting are never touched.
  It is re-read just before writing.

## Code map

```
jobscout/
├── cli.py              setup / run / template commands; isolates profiles; CI-safe logging
├── config.py           env-based settings, service-account credentials, Profile
├── models.py           RawJob dataclass, sheet column layout, tab names
├── pipeline.py         run_profile(): the steps above, plus RunReport
├── llm.py              Claude wrapper (structured JSON, Pydantic validation, pause_turn,
│                       refusal fallback) and the 4 tasks: plan / judge / facts / learn
├── dedup.py            canonical_url(), job_key(), company_title_key()
├── google_io.py        Drive (list, read/append brief, resume text extraction), profile discovery,
│                       sheet layout, DriveProfileStore
└── sources/
    ├── base.py         Source ABC, SearchPlan, date parsing
    ├── serpapi_google_jobs.py   Google Jobs → RawJob; prefers LinkedIn apply link
    └── company_sites.py         watched companies' own career sites + watchlist parsing
careers.py              ATS detection (Workday, Eightfold, Greenhouse, Lever, Ashby, SmartRecruiters), ATS search,
                        LinkResolver: company job URL + date opened + exact LinkedIn link, link verification
linkedin.py             LinkedIn link helpers (exact /jobs/view/ only, employer check)
```

| Module | Depends on | Talks to |
|---|---|---|
| `pipeline` | `llm`, `dedup`, `sources`, a store object | nothing directly (store + sources are injected, so tests use fakes) |
| `llm` | `anthropic`, `pydantic` | Anthropic Messages API |
| `google_io` | `gspread`, `google-auth` | Google Drive, Docs and Sheets APIs |
| `sources/*` | `httpx` | SerpApi, ATS public APIs |

## Data model

**Resumes/** (optional subfolder): resumes already sent. Up to 8 newest files, text extracted (Docs export, `pypdf`,
.docx XML, plain text), each truncated to 8k chars, and passed as `<resume name="…">` blocks.

**Brief** (Google Doc or `.txt`): free text. Sections: about me, target roles, seniority, location, compensation,
company preferences, nice-to-haves, "Don't show me", optional "Companies to watch" (`greenhouse:acme`),
then the auto-maintained "Learned exclusions".

**Jobs tab** (append-only, read by header name so you can add your own columns on the right):
Date added · Fit score · Job title · Company · Why it fits · Concerns · Location · Work mode ·
Employment type · Seniority · Salary range · Equity mentioned · Posted date · Company type/industry ·
Company size · Funding stage · HQ · Company description · Job URL · Company website · Source ·
**Status** (New / Interested / Applied / Not a fit) · **My feedback** · **Best resume**
(new columns are only ever added at the end; `setup` adds them to existing sheets automatically)

**Log tab**: Run at (UTC) · Found · Added · Skipped: duplicate · Skipped: excluded/low score · Skipped: over cap · New learned exclusions · Errors

**_state tab** (hidden): Kind (`feedback_processed` / `rejected` / `ats`) · Key · Date · Value (the ATS cache maps company → careers system)

## Claude calls

| Task | Model | Input | Output (validated) |
|---|---|---|---|
| `plan_searches` | Haiku 5.5 | brief + resume library | `QueryPlan{queries[], title_keywords[]}` |
| `judge` | Sonnet 5.5, effort low | brief + resume library (cached system prompt) + one posting | `Judgment{include, fit_score 1–10, exclusion_reason, why_it_fits, concerns, work_mode, salary_range, equity_mentioned, best_resume, ...}` |
| `company_facts` | Sonnet 5.5 + `web_search` (≤4 searches) | company, job links, excerpt | `CompanyFacts{industry, size, funding, hq, description, website}`, "unknown" if not found |
| `learn_exclusions` | Sonnet 5.5 | brief + new feedback rows | `LearnedPatterns{new_exclusions[]}` |

All calls use structured outputs (`output_config.format` with a strict JSON schema generated from the
Pydantic model). A response that fails validation is retried once. Postings, search results and feedback
are framed as data in every prompt, never as instructions.

## Reliability and limits

| Concern | How it's handled |
|---|---|
| One profile fails (e.g. missing Brief) | Caught per profile; error goes to that sheet's Log; others continue; exit code 1 |
| One source fails | Recorded in Log → Errors; other sources still used |
| One judge call fails | Skipped; the posting is not recorded as rejected, so it gets another chance tomorrow |
| Cost runaway | ≤4 searches, ≤80 judged, ≤25 added per profile per run; rejected postings and known companies remembered |
| Duplicate cron runs | `concurrency: jobscout` in the workflow; dedup makes reruns safe anyway |
| DST | Cron is UTC: 6 AM PST / 7 AM PDT (see README) |

## Security and privacy

```mermaid
flowchart LR
    subgraph PUBLIC["Public: GitHub repo + Actions logs"]
        CODE["code, tests, docs"]
        LOGS["CI logs: counts and error types,<br/>people shown as profile-1, profile-2"]
    end
    subgraph PRIVATE["Private"]
        SEC["GitHub Secrets:<br/>API keys, service-account key,<br/>root folder ID"]
        DRV["Google Drive 'Job Scout' folder:<br/>briefs, salary, feedback, results"]
        LOCAL[".env, secrets/ (gitignored)"]
    end
    SEC -.->|"injected at runtime"| LOGS
    SA["Service account"] -->|"Editor on ONE folder only"| DRV
```

- **Least privilege:** the service account has no access to anything except the folder you share with it. Un-sharing the folder revokes access immediately.
- **No personal data in the repo:** profiles are discovered from Drive at runtime. There's no `profiles.yaml` and no names or IDs in code.
- **Prompt-injection hygiene:** postings and web pages are untrusted input. Prompts label them as data, outputs are schema-constrained, and the agent can only append rows (it can't delete anything).

## Extending

- **New source:** subclass `Source`, implement `fetch(plan, max_age_days) -> list[RawJob]`, and add it in `cli.py`. Dedup, judging and enrichment apply to it automatically.
- **New column:** add it to `JOBS_COLUMNS` (at the end, so existing sheets stay aligned) and fill it in `pipeline.py` step 6.
- **New person:** a new subfolder with `Brief` + `Jobs`. No code or config change.
