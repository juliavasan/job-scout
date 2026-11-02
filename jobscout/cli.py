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
        try:
            files, _ = setup_profile(drive, name, folder_id)
            store = DriveProfileStore(drive, files)
            claude = Claude()
            sources = [SerpApiGoogleJobs(serp_key, p.max_queries, p.pages_per_query), AtsBoards()]
            rep = run_profile(p, store, claude, sources, dry_run=args.dry_run)
        except Exception as e:  # isolate profiles from each other
            failures += 1
            logging.error("[%s] run failed: %s: %s", label, type(e).__name__, "" if IN_CI else str(e)[:300])
            if store is not None and not args.dry_run:
                try:
                    store.append_log(["", "", "", "", "", "", "", f"run failed: {type(e).__name__}: {str(e)[:300]}"])
                except Exception:
                    pass
            continue
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
