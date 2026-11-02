"""CLI: `python -m jobscout run --profile NAME|all [--dry-run]` and `python -m jobscout init-sheet --sheet-id ID`."""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import ConfigError, load_profiles, require_env, service_account_info

# In GitHub Actions on a public repo, logs are public: print counts only, never brief text or job details.
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true"


def _print_rows(rep) -> None:
    from .models import JOBS_COLUMNS

    print(f"\n=== {rep.profile}: would add {len(rep.rows)} rows ===")
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


def cmd_run(args) -> int:
    from .llm import Claude
    from .pipeline import run_profile
    from .sheets import SheetStore, client_from_info
    from .sources import AtsBoards, SerpApiGoogleJobs

    profiles = load_profiles()
    names = list(profiles) if args.profile == "all" else [args.profile]
    missing = [n for n in names if n not in profiles]
    if missing:
        raise ConfigError(f"unknown profile(s): {missing}. Known: {sorted(profiles)}")

    gc = client_from_info(service_account_info())
    serp_key = require_env("SERPAPI_API_KEY")
    require_env("ANTHROPIC_API_KEY")

    failures = 0
    for name in names:
        p = profiles[name]
        claude = Claude()
        sources = [SerpApiGoogleJobs(serp_key, p.max_queries, p.pages_per_query), AtsBoards()]
        try:
            store = SheetStore(gc, p.sheet_id)
            rep = run_profile(p, store, claude, sources, dry_run=args.dry_run)
        except Exception as e:  # isolate profiles from each other
            failures += 1
            logging.error("[%s] run failed: %s: %s", name, type(e).__name__, str(e)[:300])
            try:
                if not args.dry_run:
                    SheetStore(gc, p.sheet_id).append_log(["", "", "", "", "", "", "", f"run failed: {type(e).__name__}: {str(e)[:300]}"])
            except Exception:
                pass
            continue
        if args.dry_run:
            if IN_CI:
                print(f"[{name}] dry run: would add {rep.added} (details hidden in CI logs)")
            else:
                _print_rows(rep)
        if rep.errors:
            logging.warning("[%s] %d non-fatal errors (see Log tab)", name, len(rep.errors))
            if not IN_CI:
                for e in rep.errors:
                    logging.warning("  %s", e)
    return 1 if failures else 0


def cmd_init_sheet(args) -> int:
    from .sheets import client_from_info, init_sheet

    info = service_account_info()
    gc = client_from_info(info)
    try:
        steps = init_sheet(gc, args.sheet_id)
    except Exception as e:
        if "PERMISSION_DENIED" in str(e) or "403" in str(e) or "404" in str(e):
            print(f"Can't open the sheet. Share it (Editor) with: {info.get('client_email')}", file=sys.stderr)
            return 2
        raise
    for s in steps:
        print(" -", s)
    print("\nDone. Next: open the sheet, write your brief in Brief!A1, then add the sheet ID to profiles.yaml / PROFILES_YAML.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jobscout", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="search, judge and append new jobs")
    r.add_argument("--profile", required=True, help="profile name from profiles.yaml, or 'all'")
    r.add_argument("--dry-run", action="store_true", help="print what would be added; write nothing")
    r.set_defaults(func=cmd_run)

    i = sub.add_parser("init-sheet", help="create Brief/Jobs/Log tabs + headers in a spreadsheet")
    i.add_argument("--sheet-id", required=True)
    i.set_defaults(func=cmd_init_sheet)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore", "anthropic", "google", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
