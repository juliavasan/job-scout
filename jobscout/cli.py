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
    added = "would add" if mode in ("dry-run", "replay") else "added"
    return (f"{head} | found {rep.found} | {added} {rep.added} | dup {rep.skipped_dup} | excluded {rep.skipped_excluded}"
            f" | over cap {rep.skipped_over_cap} | learned {len(rep.learned)} | errors {len(rep.errors)}"
            f" | serpapi {searches} | claude {u.get('calls', 0)} calls, {u.get('input_tokens', 0)/1000:.1f}k in /"
            f" {u.get('output_tokens', 0)/1000:.1f}k out, {u.get('web_searches', 0)} web searches, ~${u.get('cost_usd', 0):.2f}"
            f" | {seconds:.0f}s")


CANDIDATES_DIR = Path(os.environ.get("JOBSCOUT_CANDIDATES_DIR", "logs"))


def save_candidates(name: str, raw_jobs: list) -> None:
    """Keep the postings this run fetched (local only) so `run --replay` can re-judge them for free."""
    if IN_CI or not raw_jobs:
        return
    import dataclasses
    import json

    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    rows = [{**dataclasses.asdict(j), "posted_date": j.posted_date.isoformat() if j.posted_date else None} for j in raw_jobs]
    (CANDIDATES_DIR / f"candidates_{name}.json").write_text(json.dumps(rows, indent=1))


def load_candidates(name: str) -> list:
    import json
    from datetime import date as _date

    from .models import RawJob

    path = CANDIDATES_DIR / f"candidates_{name}.json"
    if not path.exists():
        raise ConfigError(f"nothing to replay for {name!r}: run once without --replay first ({path} missing)")
    out = []
    for d in json.loads(path.read_text()):
        d["posted_date"] = _date.fromisoformat(d["posted_date"]) if d.get("posted_date") else None
        out.append(RawJob(**d))
    return out


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
        print(f"  company site: {d['Company job URL']} | opened: {d['Date opened']} | LinkedIn: {d['LinkedIn URL']}")
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
    from .sources import CompanySites, SerpApiGoogleJobs

    serp_key = require_env("SERPAPI_API_KEY")
    from .llm import resolve_backend
    backend = resolve_backend()
    if backend == "api":
        require_env("ANTHROPIC_API_KEY")
    logging.info("Claude backend: %s", "Claude Code (subscription)" if backend == "claude-code" else "Anthropic API (credit)")
    drive, _ = _drive()

    failures = 0
    for idx, (name, folder_id) in enumerate(_profiles(drive, args.profile), 1):
        label = f"profile-{idx}" if IN_CI else name
        p = Profile.from_env(label, folder_id)
        store = None
        started, t0 = datetime.now(timezone.utc), time.monotonic()
        mode = "replay" if args.replay else ("dry-run" if args.dry_run else "run")
        claude, sources = None, []
        from .google_io import GoogleSetupError
        try:
            files, _ = setup_profile(drive, name, folder_id)
        except GoogleSetupError as e:
            if "needs" in str(e):  # folder not set up yet (no Brief / Jobs sheet): skip, don't fail the run
                logging.warning("[%s] skipped: folder not set up yet%s", label, "" if IN_CI else f" ({e})")
                continue
            raise
        locked = False
        try:
            store = DriveProfileStore(drive, files)
            if not (args.dry_run or args.replay):
                if not store.acquire_lock("cloud" if IN_CI else "local"):
                    logging.warning("[%s] skipped: another run is in progress for this person", label)
                    continue
                locked = True
            claude = Claude()
            resolver = make_resolver(serp_key, claude)
            resolver.cache.update({st["Key"]: st.get("Value", "") for st in store.read_state()
                                   if st.get("Kind") == "ats" and st.get("Value")})
            serp_source = SerpApiGoogleJobs(serp_key, p.max_queries, p.pages_per_query)
            if args.replay:
                from .sources.base import Source as _Src

                class Replay(_Src):
                    name = "replay"

                    def __init__(self, jobs):
                        self.jobs = jobs

                    def fetch(self, plan, max_age_days):
                        return list(self.jobs)

                sources = [serp_source, Replay(load_candidates(name))]
                serp_source.max_queries = 0  # no searches at all
                rep = run_profile(p, store, claude, sources, replay=True)
            else:
                sources = [serp_source, CompanySites(resolver)]
                rep = run_profile(p, store, claude, sources, dry_run=args.dry_run, resolver=resolver)
                save_candidates(name, rep.raw_jobs)
        except Exception as e:  # isolate profiles from each other
            if locked:
                try:
                    store.release_lock()
                except Exception:
                    pass
            failures += 1
            write_run_log(format_run_line(started, name, mode, failure=f"{type(e).__name__}: {str(e)[:200]}"))
            if store is not None and not (args.dry_run or args.replay):
                try:
                    store.append_run_log(run_log_row(started, mode, usage=claude.usage if claude else None,
                                                     searches=sources[0].searches_used if sources else 0,
                                                     seconds=time.monotonic() - t0, note=f"run failed: {type(e).__name__}"))
                except Exception:
                    pass
            logging.error("[%s] run failed: %s: %s", label, type(e).__name__, "" if IN_CI else str(e)[:300])
            if store is not None and not (args.dry_run or args.replay):
                try:
                    store.append_log(["", "", "", "", "", "", "", f"run failed: {type(e).__name__}: {str(e)[:300]}"])
                except Exception:
                    pass
            continue
        if locked:
            try:
                store.release_lock()
            except Exception:
                pass
        seconds = time.monotonic() - t0
        write_run_log(format_run_line(started, name, mode, rep, searches=sources[0].searches_used + resolver.serp.searches_used,
                                      usage=claude.usage, seconds=seconds))
        if not (args.dry_run or args.replay):
            try:
                store.append_run_log(run_log_row(started, mode, rep, sources[0].searches_used + resolver.serp.searches_used,
                                                 claude.usage, seconds))
            except Exception as e:
                logging.warning("[%s] could not write the Log sheet: %s", label, type(e).__name__)
        if args.dry_run or args.replay:
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


def _lookup_budget(serp_key: str, wanted: int) -> int:
    """Extra SerpApi lookups allowed this run; keeps a reserve so the daily searches never run out."""
    try:
        import httpx
        acct = httpx.get("https://serpapi.com/account.json", params={"api_key": serp_key}, timeout=15).json()
        left = acct.get("total_searches_left", acct.get("plan_searches_left"))
        if isinstance(left, int):
            reserve = _int("JOBSCOUT_SERPAPI_RESERVE", 40)
            return max(0, min(wanted, left - reserve))
    except Exception:
        pass
    return wanted


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def make_resolver(serp_key: str, claude):
    from .careers import LinkResolver
    from .sources import SerpApiGoogleJobs

    serp = SerpApiGoogleJobs(serp_key, max_queries=0)
    return LinkResolver(serp=serp, claude=claude, max_lookups=_lookup_budget(serp_key, _int("JOBSCOUT_MAX_LOOKUPS", 0)))


def cmd_eval(args) -> int:
    from . import evaluation as E
    from .google_io import DriveProfileStore, setup_profile

    E.FLOW_DIR.mkdir(parents=True, exist_ok=True)
    E.check_harness(args.approve_harness)
    drive, _ = _drive()
    (name, folder_id), = _profiles(drive, args.profile)
    files, _ = setup_profile(drive, name, folder_id)
    store = DriveProfileStore(drive, files)
    s = E.run_eval(store, Profile.from_env(name, folder_id), variant=args.variant, reps=args.reps,
                   limit=args.limit, workers=args.workers, timeout_s=args.timeout_s,
                   only=args.cases.split(",") if args.cases else None)
    print(E.format_summary(s) + f"\n  wall clock      : {s['wall_clock_s']}s")
    return 0


def cmd_eval_cases(args) -> int:
    from . import eval_cases as EC
    from .google_io import DriveProfileStore, setup_profile

    drive, _ = _drive()
    (name, folder_id), = _profiles(drive, args.profile)
    src = args.source or str(CANDIDATES_DIR / f"candidates_{name}.json")
    cases = EC.build_cases(EC.load_candidates(src), n=args.n)
    files, _ = setup_profile(drive, name, folder_id)
    kept = EC.write_cases(DriveProfileStore(drive, files), cases)
    print(f"{len(cases)} cases written to the Eval tab ({kept} earlier labels kept). Label them Fit / Not fit / Unsure, "
          f"then run: python -m jobscout eval --profile {args.profile}")
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
    r.add_argument("--replay", action="store_true",
                   help="re-judge the postings from the last local run against the current brief: 0 SerpApi searches, writes nothing")
    r.set_defaults(func=cmd_run)


    e = sub.add_parser("eval", help="measure the judge against your own Fit / Not fit labels (Eval tab)")
    e.add_argument("--profile", required=True)
    e.add_argument("--variant", default="baseline", help="results folder name: baseline, v1, v2, ...")
    e.add_argument("--reps", type=int, default=1)
    e.add_argument("--limit", type=int, default=None, help="only the first N labelled cases (pilot)")
    e.add_argument("--cases", default="", help="comma-separated case ids to run")
    e.add_argument("--workers", type=int, default=4)
    e.add_argument("--timeout-s", type=int, default=600)
    e.add_argument("--approve-harness", action="store_true", help="approve the current eval runner (you, after reviewing it)")
    e.set_defaults(func=cmd_eval)

    c = sub.add_parser("eval-cases", help="freeze postings from a run into the eval set and write the Eval tab for labelling")
    c.add_argument("--profile", required=True)
    c.add_argument("--from", dest="source", default="", help="postings file [logs/candidates_<name>.json from the last local run]")
    c.add_argument("--n", type=int, default=45, help="how many cases [45]")
    c.set_defaults(func=cmd_eval_cases)

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
