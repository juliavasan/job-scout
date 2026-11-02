"""Job Scout CLI.

  python -m jobscout setup                      check access, list profiles, prepare each sheet's tabs
  python -m jobscout run --profile NAME|all     search, judge, append new jobs
  python -m jobscout run --profile all --dry-run
  python -m jobscout template                   print the brief template
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError, Profile, require_env, root_folder_id, service_account_info

# In GitHub Actions on a public repo, logs are public: print counts only, never
# brief text, job details or people's folder names.
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true"


def _drive():
    from .google_io import Drive, credentials_from_service_account

    info = service_account_info()
    return Drive(credentials_from_service_account(info)), info.get("client_email", "?")


def _profiles(drive, wanted: str) -> list[tuple[str, str]]:
    from .google_io import discover_profiles

    found = discover_profiles(drive, root_folder_id())
    if not found:
        raise ConfigError("the Job Scout folder has no subfolders — create one per person")
    if wanted == "all":
        return found
    match = [(n, i) for n, i in found if n.lower() == wanted.lower()]
    if not match:
        raise ConfigError(f"no folder named {wanted!r}. Found: {[n for n, _ in found]}")
    return match


RUN_LOG = Path(os.environ.get("JOBSCOUT_RUN_LOG", "logs/runs.log"))
RUN_LOG_HEADER = "# Job Scout run log (local only, gitignored). Times in UTC. One line per profile per run.\n"


def write_run_log(line: str) -> None:
    """Append one line to the local run log. Skipped in CI (the sheet's Log tab covers cloud runs)."""
    if IN_CI:
        return
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    new = not RUN_LOG.exists()
    with RUN_LOG.open("a") as f:
        if new:
            f.write(RUN_LOG_HEADER)
        f.write(line.rstrip() + "\n")


def format_run_line(when: datetime, name: str, mode: str, rep=None, searches: int = 0, usage: dict | None = None,
                    seconds: float = 0.0, failure: str = "") -> str:
    head = f"{when:%Y-%m-%d %H:%M} UTC | {name} | {mode}"
    if failure:
        return f"{head} | FAILED: {failure}"
    u = usage or {}
    added = "would add" if mode == "dry-run" else "added"
    return (f"{head} | found {rep.found} | {added} {rep.added} | dup {rep.skipped_dup} | excluded {rep.skipped_excluded}"
            f" | over cap {rep.skipped_over_cap} | learned {len(rep.learned)} | errors {len(rep.errors)}"
            f" | serpapi {searches} | claude {u.get('calls', 0)} calls, {u.get('input_tokens', 0)/1000:.1f}k in /"
            f" {u.get('output_tokens', 0)/1000:.1f}k out, {u.get('web_searches', 0)} web searches, ~${u.get('cost_usd', 0):.2f}"
            f" | {seconds:.0f}s")


def run_log_row(when: datetime, mode: str, rep=None, searches: int = 0, usage: dict | None = None,
                seconds: float = 0.0, note: str = "") -> list:
    """Row for the 'Log' spreadsheet (see models.RUNLOG_COLUMNS)."""
    u = usage or {}
    where = "cloud (GitHub Actions)" if IN_CI else "local"
    counts = ([rep.found, rep.added, rep.skipped_dup, rep.skipped_excluded, rep.skipped_over_cap, len(rep.learned)]
              if rep else ["", "", "", "", "", ""])
    errors = "; ".join(rep.errors)[:500] if rep else ""
    return [when.strftime("%Y-%m-%d %H:%M"), mode, where, *counts, searches, u.get("calls", 0), u.get("input_tokens", 0),
            u.get("output_tokens", 0), u.get("web_searches", 0), round(u.get("cost_usd", 0.0), 2), round(seconds),
            "; ".join(x for x in (errors, note) if x)]


def _print_rows(name: str, rep) -> None:
    from .models import JOBS_COLUMNS

    print(f"\n=== {name}: would add {len(rep.rows)} rows ===")
    for row in rep.rows:
        d = dict(zip(JOBS_COLUMNS, row))
        print(f"\n[{d['Fit score (1–10)']}/10] {d['Job title']} — {d['Company']} ({d['Location']}, {d['Work mode']})")
        print(f"  salary: {d['Salary range']} | equity: {d['Equity mentioned']} | posted: {d['Posted date']} | {d['Employment type']} | {d['Seniority']}")
        print(f"  company: {d['Company type / industry']} | {d['Company size (employees)']} | {d['Funding stage / public']} | HQ {d['HQ']}")
        print(f"  about: {d['Company description']}")
        print(f"  why: {d['Why it fits']}")
        print(f"  concerns: {d['Concerns / red flags']}")
        print(f"  {d['Job URL']}  [{d['Source']}]")
    if rep.excluded_samples:
        print(f"\n--- excluded ({len(rep.excluded_samples)}) ---")
        for t, c, why in rep.excluded_samples:
            print(f"  x {t} — {c}: {why}")
    if rep.learned:
        print("\n--- new learned exclusions ---")
        for r in rep.learned:
            print(f"  + {r}")


def cmd_setup(args) -> int:
    from .google_io import DriveProfileStore, GoogleSetupError, setup_profile

    drive, email = _drive()
    print(f"Service account: {email}\n(share your 'Job Scout' folder with this email as Editor)\n")
    try:
        profiles = _profiles(drive, "all")
    except GoogleSetupError as e:
        print(f"Can't open the root folder: {e}", file=sys.stderr)
        return 2
    bad = 0
    for name, fid in profiles:
        try:
            files, steps = setup_profile(drive, name, fid)
            print(f"✓ {name}: " + ("; ".join(steps) if steps else "sheet ready"))
            if files.resumes_folder:
                resumes = DriveProfileStore(drive, files).read_resumes()
                print(f"    resume library '{files.resumes_folder['name']}': {len(resumes)} readable file(s)"
                      + "".join(f"\n      - {n} ({len(t):,} chars)" for n, t in resumes))
            else:
                print("    no 'Resumes' subfolder (optional)")
        except GoogleSetupError as e:
            bad += 1
            print(f"✗ {name}: {e}")
    return 1 if bad else 0


def cmd_run(args) -> int:
    from .google_io import DriveProfileStore, setup_profile
    from .llm import Claude
    from .pipeline import run_profile
    from .sources import AtsBoards, SerpApiGoogleJobs

    serp_key = require_env("SERPAPI_API_KEY")
    require_env("ANTHROPIC_API_KEY")
    drive, _ = _drive()

    failures = 0
    for idx, (name, folder_id) in enumerate(_profiles(drive, args.profile), 1):
        label = f"profile-{idx}" if IN_CI else name
        p = Profile.from_env(label, folder_id)
        store = None
        started, t0 = datetime.now(timezone.utc), time.monotonic()
        mode = "dry-run" if args.dry_run else "run"
        claude, sources = None, []
        try:
            files, _ = setup_profile(drive, name, folder_id)
            store = DriveProfileStore(drive, files)
            claude = Claude()
            sources = [SerpApiGoogleJobs(serp_key, p.max_queries, p.pages_per_query), AtsBoards()]
            rep = run_profile(p, store, claude, sources, dry_run=args.dry_run)
        except Exception as e:  # isolate profiles from each other
            failures += 1
            write_run_log(format_run_line(started, name, mode, failure=f"{type(e).__name__}: {str(e)[:200]}"))
            if store is not None and not args.dry_run:
                try:
                    store.append_run_log(run_log_row(started, mode, usage=claude.usage if claude else None,
                                                     searches=sources[0].searches_used if sources else 0,
                                                     seconds=time.monotonic() - t0, note=f"run failed: {type(e).__name__}"))
                except Exception:
                    pass
            logging.error("[%s] run failed: %s: %s", label, type(e).__name__, "" if IN_CI else str(e)[:300])
            if store is not None and not args.dry_run:
                try:
                    store.append_log(["", "", "", "", "", "", "", f"run failed: {type(e).__name__}: {str(e)[:300]}"])
                except Exception:
                    pass
            continue
        seconds = time.monotonic() - t0
        write_run_log(format_run_line(started, name, mode, rep, searches=sources[0].searches_used,
                                      usage=claude.usage, seconds=seconds))
        if not args.dry_run:
            try:
                store.append_run_log(run_log_row(started, mode, rep, sources[0].searches_used, claude.usage, seconds))
            except Exception as e:
                logging.warning("[%s] could not write the Log sheet: %s", label, type(e).__name__)
        if args.dry_run:
            if IN_CI:
                print(f"[{label}] dry run: would add {rep.added} (details hidden in CI logs)")
            else:
                _print_rows(name, rep)
        if rep.errors:
            logging.warning("[%s] %d non-fatal errors (see the Log tab)", label, len(rep.errors))
            if not IN_CI:
                for e in rep.errors:
                    logging.warning("  %s", e)
    return 1 if failures else 0


def cmd_backfill_linkedin(args) -> int:
    """Fill the (new) LinkedIn URL column for existing rows. Touches only empty cells in that column."""
    from concurrent.futures import ThreadPoolExecutor

    from . import linkedin
    from .google_io import DriveProfileStore, setup_profile
    from .llm import Claude

    drive, _ = _drive()
    for name, folder_id in _profiles(drive, args.profile):
        files, _ = setup_profile(drive, name, folder_id)  # adds the column header if missing
        store = DriveProfileStore(drive, files)
        rows = store.read_jobs()
        todo = {i + 2: r for i, r in enumerate(rows) if not (r.get("LinkedIn URL") or "").strip()}
        claude = Claude() if linkedin.SEARCH_ENABLED else None

        def resolve(item):
            rownum, r = item
            return rownum, linkedin.resolve(claude, r.get("Job title", ""), r.get("Company", ""), r.get("Location", ""), [r.get("Job URL", "")])

        with ThreadPoolExecutor(4) as pool:
            results = dict(pool.map(resolve, todo.items()))
        kinds = [k for _, k in results.values()]
        started_bf = datetime.now(timezone.utc)
        written = store.fill_empty_cells("LinkedIn URL", {r: u for r, (u, _) in results.items()})
        label = f"profile-{list(dict(_profiles(drive, 'all'))).index(name) + 1}" if IN_CI else name
        print(f"{label}: filled {written} LinkedIn URL cell(s): {kinds.count('posting')} from known links, "
              f"{kinds.count('found')} found by search, {kinds.count('search')} LinkedIn search links"
              + (f" (~${claude.usage['cost_usd']:.2f})" if claude else ""))
        if written:
            store.append_run_log(run_log_row(started_bf, "backfill-linkedin", usage=claude.usage if claude else None,
                                             note=f"filled {written} LinkedIn URL cells"))

    return 0


def cmd_template(args) -> int:
    from .google_io import BRIEF_TEMPLATE

    print(BRIEF_TEMPLATE)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jobscout", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="check access and prepare every person's sheet")
    s.set_defaults(func=cmd_setup)

    r = sub.add_parser("run", help="search, judge and append new jobs")
    r.add_argument("--profile", required=True, help="person's folder name, or 'all'")
    r.add_argument("--dry-run", action="store_true", help="print what would be added; write nothing")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("backfill-linkedin", help="fill the LinkedIn URL column for existing rows (empty cells only)")
    b.add_argument("--profile", required=True, help="person's folder name, or 'all'")
    b.set_defaults(func=cmd_backfill_linkedin)

    t = sub.add_parser("template", help="print the brief template to paste into a Brief doc")
    t.set_defaults(func=cmd_template)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "google", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from .google_io import GoogleSetupError

    try:
        return args.func(args)
    except (ConfigError, GoogleSetupError) as e:
        print(f"setup error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
