# Architecture

Job Scout is a small batch pipeline that runs once a day. It has no server, no database and no UI.
**Each person's Google Sheet is their UI and their storage.** GitHub Actions is the scheduler,
and Claude does the reasoning.

## System overview

```mermaid
flowchart LR
    subgraph GH["GitHub Actions — daily cron 14:00 UTC"]
        CLI["python -m jobscout run --profile all"]
    end

    subgraph SECRETS["GitHub Secrets"]
        direction TB
        S1["ANTHROPIC_API_KEY"]
        S2["SERPAPI_API_KEY"]
        S3["GOOGLE_SERVICE_ACCOUNT_JSON"]
        S4["PROFILES_YAML<br/>profile → sheet ID"]
    end

    subgraph SHEET["Google Sheet — one per person"]
        BRIEF["Brief tab<br/>A1: search brief + learned exclusions<br/>B2: ATS watchlist"]
        JOBS["Jobs tab<br/>23 columns, Status dropdown"]
        LOG["Log tab<br/>one row per run"]
        STATE["_state tab (hidden)<br/>processed feedback, rejected jobs"]
    end

    subgraph SOURCES["Job sources (pluggable)"]
        SERP["SerpApi Google Jobs<br/>LinkedIn, Indeed, Glassdoor,<br/>company career pages"]
        ATS["Greenhouse / Lever / Ashby<br/>public JSON boards"]
    end

    subgraph CLAUDE["Anthropic API"]
        HAIKU["Haiku 5.5<br/>brief → search queries"]
        SONNET["Sonnet 5.5<br/>judge fit · learn exclusions"]
        WEB["Sonnet 5.5 + web search<br/>company facts"]
    end

    SECRETS -.-> CLI
    CLI <-->|"service account<br/>read brief/rows, append rows"| SHEET
    CLI --> HAIKU
    CLI --> SERP
    CLI --> ATS
    CLI --> SONNET
    CLI --> WEB
    USER(["You, on phone or laptop"]) <-->|"edit brief, set Status,<br/>write feedback"| SHEET
```

## One run, step by step

```mermaid
flowchart TD
    A["Load profile → open sheet"] --> B["Read Brief!A1, all Jobs rows, _state"]
    B --> C{"New 'Not a fit' rows<br/>with feedback?"}
    C -- yes --> D["Claude: turn feedback into<br/>general exclusion rules"]
    D --> E["Append dated rules to<br/>'Learned exclusions' in Brief!A1"]
    C -- no --> F
    E --> F["Claude Haiku: brief → 3–4 search queries<br/>+ title keywords"]
    F --> G["Fetch: SerpApi Google Jobs<br/>+ ATS watchlist boards"]
    G --> H["Drop postings older than 7 days"]
    H --> I["Dedup<br/>1. canonical URL<br/>2. company + title + location<br/>3. company + title marked Not a fit<br/>4. rejected in an earlier run"]
    I --> J["Claude Sonnet: judge each candidate<br/>against brief + learned exclusions<br/>→ validated JSON"]
    J --> K{"include AND<br/>score ≥ min?"}
    K -- no --> L["Record in _state as rejected<br/>so it isn't judged again"]
    K -- yes --> M["Sort by score, keep top 25"]
    M --> N["Company facts: reuse from sheet,<br/>else Claude + web search<br/>('unknown' if not found)"]
    N --> O["Append rows to Jobs, Status = New"]
    O --> P["Append one row to Log"]
    L --> P
```

## Sequence (one profile)

```mermaid
sequenceDiagram
    autonumber
    participant CLI as jobscout CLI
    participant SH as Google Sheet
    participant C as Claude API
    participant SA as SerpApi
    participant ATS as ATS boards

    CLI->>SH: read Brief!A1, Jobs rows, _state
    opt new "Not a fit" feedback
        CLI->>C: learn_exclusions(brief, feedback)
        C-->>CLI: ["Exclude sales-heavy roles", ...]
        CLI->>SH: re-read Brief!A1, append dated rules
    end
    CLI->>C: plan_searches(brief) [Haiku]
    C-->>CLI: queries + title keywords
    loop each query (max 4)
        CLI->>SA: google_jobs search
        SA-->>CLI: ~10 postings
    end
    CLI->>ATS: watchlist boards (optional)
    ATS-->>CLI: postings matching title keywords
    Note over CLI: dedup against sheet + _state
    par up to 4 at a time
        CLI->>C: judge(brief, posting) [Sonnet]
        C-->>CLI: Judgment JSON
    end
    par once per new company
        CLI->>C: company_facts(company) [Sonnet + web_search]
        C-->>CLI: CompanyFacts JSON
    end
    CLI->>SH: append Jobs rows, _state rows, Log row
```

## Feedback loop

```mermaid
flowchart LR
    R["Row in Jobs"] -->|"you set Status = Not a fit<br/>+ My feedback = 'too sales-heavy'"| N["Next run"]
    N --> L["Claude generalizes:<br/>'Exclude roles that are mostly<br/>quota-carrying sales'"]
    L --> B["Brief!A1 gets<br/>- [2026-11-03] Exclude ..."]
    B --> J["Every later judge call<br/>sees the new rule"]
    B -.->|"you can edit or delete it"| B
```

- The feedback key is (row URL, company+title+location, feedback text), stored in `_state`. Each piece of feedback is processed once. Editing the feedback text counts as new feedback.
- If you delete a learned rule, it stays deleted, because the feedback that produced it is already marked processed.
- Rows are never edited. The only writes to existing content are appends to the end of `Brief!A1`, and the agent re-reads the cell just before writing so it doesn't overwrite an edit you made from your phone.

## Code map

```
jobscout/
├── cli.py              argparse: run, init-sheet. Isolates profiles; CI-safe logging
├── config.py           profiles (profiles.yaml or PROFILES_YAML), credentials from env
├── models.py           RawJob dataclass, sheet column layout, tab names
├── pipeline.py         run_profile(): the steps above, plus RunReport
├── llm.py              Claude wrapper (structured JSON, Pydantic validation, pause_turn,
│                       refusal fallback) and the 4 tasks: plan / judge / facts / learn
├── dedup.py            canonical_url(), job_key(), company_title_key()
├── sheets.py           SheetStore (gspread), init_sheet() formatting + brief template
└── sources/
    ├── base.py         Source ABC, SearchPlan, date parsing
    ├── serpapi_google_jobs.py   Google Jobs → RawJob; prefers LinkedIn apply link
    └── ats.py          Greenhouse / Lever / Ashby boards + watchlist parsing
```

| Module | Depends on | Talks to |
|---|---|---|
| `pipeline` | `llm`, `dedup`, `sources`, a store object | nothing directly (store + sources are injected, so tests use fakes) |
| `llm` | `anthropic`, `pydantic` | Anthropic Messages API |
| `sheets` | `gspread`, `google-auth` | Google Sheets API |
| `sources/*` | `httpx` | SerpApi, ATS public APIs |

## Data model

**`Jobs`** (append-only, read by header name so you can add your own columns on the right):
Date added · Fit score · Job title · Company · Why it fits · Concerns · Location · Work mode ·
Employment type · Seniority · Salary range · Equity mentioned · Posted date · Company type/industry ·
Company size · Funding stage · HQ · Company description · Job URL · Company website · Source ·
**Status** (New / Interested / Applied / Not a fit) · **My feedback**

**`Log`**: Run at (UTC) · Found · Added · Skipped: duplicate · Skipped: excluded/low score · Skipped: over cap · New learned exclusions · Errors

**`_state`** (hidden): Kind (`feedback_processed` / `rejected`) · Key · Date

## Claude calls

| Task | Model | Input | Output (validated) |
|---|---|---|---|
| `plan_searches` | Haiku 5.5 | brief | `QueryPlan{queries[], title_keywords[]}` |
| `judge` | Sonnet 5.5, effort low | brief (cached system prompt) + one posting | `Judgment{include, fit_score 1–10, exclusion_reason, why_it_fits, concerns, work_mode, salary_range, equity_mentioned, ...}` |
| `company_facts` | Sonnet 5.5 + `web_search` (≤4 searches) | company, job links, excerpt | `CompanyFacts{industry, size, funding, hq, description, website}`, "unknown" if not found |
| `learn_exclusions` | Sonnet 5.5 | brief + new feedback rows | `LearnedPatterns{new_exclusions[]}` |

All calls use structured outputs (`output_config.format` with a strict JSON schema generated from the
Pydantic model). A response that fails validation is retried once. Postings, search results and feedback
are framed as data in every prompt, never as instructions.

## Reliability and limits

| Concern | How it's handled |
|---|---|
| One profile fails | Caught per profile; error goes to that sheet's `Log`; others continue; exit code 1 |
| One source fails | Recorded in `Log.Errors`; other sources still used |
| One judge call fails | Skipped; the posting is not recorded as rejected, so it gets another chance tomorrow |
| Cost runaway | ≤4 searches, ≤80 judged, ≤25 added per profile per run; rejected postings and known companies remembered |
| Duplicate cron runs | `concurrency: jobscout` in the workflow; dedup makes reruns safe anyway |
| DST | Cron is UTC: 6 AM PST / 7 AM PDT (see README) |

## Privacy boundary

```mermaid
flowchart LR
    subgraph PUBLIC["Public: GitHub repo + Actions logs"]
        CODE["code, tests, README,<br/>profiles.example.yaml"]
        LOGS["CI logs: counts and<br/>error types only"]
    end
    subgraph PRIVATE["Private"]
        SEC["GitHub Secrets:<br/>API keys, service account,<br/>profile → sheet ID"]
        SHT["Google Sheets:<br/>briefs, salary, feedback, results"]
        LOCAL[".env, profiles.yaml,<br/>secrets/ (gitignored)"]
    end
    SEC -.->|"injected at runtime"| LOGS
```

## Extending

- **New source:** subclass `Source`, implement `fetch(plan, max_age_days) -> list[RawJob]`, and add it in `cli.py`. Dedup, judging and enrichment apply to it automatically.
- **New column:** add it to `JOBS_COLUMNS` (at the end, so existing sheets stay aligned) and fill it in `pipeline.py` step 6.
- **New person:** a new sheet, `init-sheet`, and one line in `PROFILES_YAML`. No code change.
