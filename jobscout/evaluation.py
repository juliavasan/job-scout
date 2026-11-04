"""Eval: does the judge keep/skip the same postings a person would?

Cases:  .claude/hillclimb/judge/cases.jsonl   (frozen postings; private, gitignored)
Labels: the "Eval" tab in the person's Jobs sheet (Fit / Not fit / Unsure; Unsure and blank are left out)
Runs:   the real judge (llm.judge) with the person's CURRENT brief and resumes from Drive
Output: .claude/hillclimb/judge/<variant>/results.jsonl, traces/<case>_rep<k>.json, errors.jsonl

A case "keeps" when the judge says include AND fit_score >= the profile's min_fit_score — the same rule the
daily run uses. Positive class = "Fit".

Past decisions (memory.py) can be shown to the judge, as in the daily run:
  examples="none"   no examples (baseline)
  examples="sheet"  the person's Applied / Ignore ticks from the Jobs tab, minus any job that is also an eval case
  examples="split"  half of the labelled cases become examples, the other half is the test (stratified, fixed seed)
Eval cases are never shown to the judge as examples of themselves, so the score measures generalization.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date
from pathlib import Path

from . import llm as L
from . import memory
from .models import RawJob

log = logging.getLogger(__name__)

FLOW_DIR = Path(".claude/hillclimb/judge")
LABELS = {"fit": 1, "not fit": 0}
STATE = {
    "flow": "judge",
    "metrics": [
        {"id": "correct", "label": "Agrees w/ you", "kind": "binary"},
        {"id": "keep", "label": "Agent keeps", "kind": "binary"},
        {"id": "label_fit", "label": "You: Fit", "kind": "binary"},
    ],
    "perf_fields": ["latency_s", "fit_score"],
    "harness_paths": ["jobscout/evaluation.py"],
}


# ---------- harness integrity: the person approves the runner before it runs ----------

def _harness_sha() -> str:
    h = hashlib.sha256()
    for p in [Path(__file__)] + [Path(x) for x in STATE["harness_paths"] if Path(x) != Path(__file__)]:
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def check_harness(approve: bool) -> None:
    f = FLOW_DIR / "harness.sha"
    cur = _harness_sha()
    if approve:
        f.write_text(cur)
        return
    if not f.exists() or f.read_text().strip() != cur:
        raise SystemExit(f"eval runner changed or not yet approved (sha {cur}). Review jobscout/evaluation.py, then run once "
                         f"with --approve-harness.")


# ---------- inputs ----------

def load_cases() -> list[dict]:
    path = FLOW_DIR / "cases.jsonl"
    if not path.exists():
        raise SystemExit(f"{path} not found")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_labels(store) -> dict[str, dict]:
    ws = store.sh.worksheet("Eval")
    rows = ws.get_all_values()
    hdr = rows[0]
    out = {}
    for r in rows[1:]:
        d = dict(zip(hdr, r))
        lab = (d.get("Your label") or "").strip().lower()
        if d.get("Case") and lab in LABELS:
            out[d["Case"]] = {"label": LABELS[lab], "note": d.get("Note (optional)", "")}
    return out


def _job(c: dict) -> RawJob:
    fields = {k: c.get(k) for k in RawJob.__dataclass_fields__ if k in c}
    fields["posted_date"] = date.fromisoformat(c["posted_date"]) if c.get("posted_date") else None
    return RawJob(**fields)


# ---------- running ----------

def pick_cases_and_examples(store, all_cases: list[dict], labels: dict, examples: str, test_half: bool):
    """(cases to test, examples to show). See the module docstring for the modes."""
    labelled = [c for c in all_cases if c["case_id"] in labels]
    if examples == "split" or test_half:
        train, test = memory.split_labelled(labelled, labels)
    else:
        train, test = [], labelled
    if examples == "split":
        past = memory.select(memory.from_eval_cases(train, labels))
    elif examples == "sheet":
        eval_keys = {memory.decision_key(c.get("company", ""), c.get("title", ""), c.get("location", "")) for c in all_cases}
        past = memory.select(memory.from_sheet_rows(store.read_jobs(), store.read_flagged_rows()), exclude_keys=eval_keys)
    elif examples == "none":
        past = []
    else:
        raise SystemExit(f"unknown --examples {examples!r} (none, sheet, split)")
    return test, past


def run_eval(store, profile, variant: str = "baseline", reps: int = 1, limit: int | None = None,
             workers: int = 4, timeout_s: int = 600, only: list[str] | None = None,
             examples: str = "none", test_half: bool = False) -> dict:
    vdir = FLOW_DIR / variant
    (vdir / "traces").mkdir(parents=True, exist_ok=True)
    state_file = FLOW_DIR / "_state.json"
    if not state_file.exists():
        state_file.write_text(json.dumps(STATE, indent=2))

    brief = store.read_brief()
    resumes = store.read_resumes()
    brief_sha = hashlib.sha256(brief.encode()).hexdigest()[:10]
    labels = load_labels(store)
    cases, past = pick_cases_and_examples(store, load_cases(), labels, examples, test_half)
    cases = [c for c in cases if not only or c["case_id"] in only]
    if limit:
        cases = cases[:limit]

    results_path, errors_path = vdir / "results.jsonl", vdir / "errors.jsonl"
    done = set()
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            r = json.loads(line)
            done.add((r["prompt_id"], r["rep"]))
    todo = [(c, k) for c in cases for k in range(reps) if (c["case_id"], k) not in done]
    lock = threading.Lock()
    print(f"eval '{variant}': {len(cases)} labelled cases × {reps} rep(s); {len(todo)} to run "
          f"({len(done)} already done); brief {brief_sha}, {len(resumes)} resumes, "
          f"{len(past)} past decisions shown ({examples})")

    def one(item):
        c, k = item
        job = _job(c)
        claude = L.Claude()
        t0 = time.monotonic()
        system, user = L.judge_prompts(brief, job, resumes, past)
        try:
            j = L.judge(claude, brief, job, resumes, past)
        except Exception as e:
            row = {"prompt_id": c["case_id"], "rep": k, "failure_class": "harness_or_serving_error",
                   "error": f"{type(e).__name__}: {str(e)[:300]}", "model": claude.last_model, "usage": claude.last_raw_usage}
            with lock, errors_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            return
        latency = time.monotonic() - t0
        served = claude.last_model or ""
        if served and not served.startswith(L.JUDGE_MODEL):
            row = {"prompt_id": c["case_id"], "rep": k, "failure_class": "served_model_mismatch",
                   "error": f"asked {L.JUDGE_MODEL}, served {served}", "model": served, "usage": claude.last_raw_usage}
            with lock, errors_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            return
        keep = int(j.include and j.fit_score >= profile.min_fit_score)
        lab = labels[c["case_id"]]["label"]
        trace = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": json.dumps(j.model_dump(), indent=2)},
        ]
        (vdir / "traces" / f"{c['case_id']}_rep{k}.json").write_text(json.dumps(trace, indent=1))
        row = {
            "prompt_id": c["case_id"], "rep": k,
            "prompt": f"{c['title']} — {c['company']} ({c['location']})",
            "tags": ["fit" if lab else "not fit", "careers site" if "careers site" in c.get("source", "") else "google jobs"],
            "status": "ok", "stop_reason": "end_turn",
            "grade": {"correct": int(keep == lab), "keep": keep, "label_fit": lab},
            "explanation": {"correct": (j.exclusion_reason or j.why_it_fits)[:400]},
            "fit_score": j.fit_score, "latency_s": round(latency, 2),
            "model": served or L.JUDGE_MODEL, "usage": claude.last_raw_usage,
            "cost_usd_list_price": round(claude.usage["cost_usd"], 5),
            "meta": {"brief_sha": brief_sha, "backend": claude.backend, "your_note": labels[c["case_id"]]["note"],
                     "examples": examples, "n_examples": len(past)},
        }
        with lock, results_path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    t_start = time.monotonic()
    with ThreadPoolExecutor(workers) as pool:
        futs = [(item, pool.submit(one, item)) for item in todo]
        for (c, k), fut in futs:
            try:
                fut.result(timeout=timeout_s)
            except FuturesTimeout:
                with lock, errors_path.open("a") as f:
                    f.write(json.dumps({"prompt_id": c["case_id"], "rep": k, "failure_class": "timeout",
                                        "error": f"no result within {timeout_s}s"}) + "\n")
    summary = summarize(variant)
    summary["wall_clock_s"] = round(time.monotonic() - t_start, 1)
    return summary


# ---------- scoring ----------

def _wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def summarize(variant: str = "baseline") -> dict:
    rows = [json.loads(x) for x in (FLOW_DIR / variant / "results.jsonl").read_text().splitlines() if x.strip()] \
        if (FLOW_DIR / variant / "results.jsonl").exists() else []
    errs = (FLOW_DIR / variant / "errors.jsonl")
    n_err = len(errs.read_text().splitlines()) if errs.exists() else 0
    tp = sum(1 for r in rows if r["grade"]["keep"] and r["grade"]["label_fit"])
    fp = sum(1 for r in rows if r["grade"]["keep"] and not r["grade"]["label_fit"])
    fn = sum(1 for r in rows if not r["grade"]["keep"] and r["grade"]["label_fit"])
    tn = sum(1 for r in rows if not r["grade"]["keep"] and not r["grade"]["label_fit"])
    n = len(rows)

    def ratio(k, m):
        lo, hi = _wilson(k, m)
        return {"value": round(k / m, 3) if m else None, "ci95": [round(lo, 3), round(hi, 3)], "n": m}

    return {
        "variant": variant, "cases": n, "errors": n_err,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "accuracy": ratio(tp + tn, n),
        "precision": ratio(tp, tp + fp),      # of the jobs it would add, how many you want
        "recall": ratio(tp, tp + fn),         # of the jobs you want, how many it keeps
        "specificity": ratio(tn, tn + fp),    # of the jobs you don't want, how many it skips
        "majority_baseline": round(max(tp + fn, tn + fp) / n, 3) if n else None,
        "mean_latency_s": round(sum(r["latency_s"] for r in rows) / n, 1) if n else None,
        "list_price_usd": round(sum(r.get("cost_usd_list_price", 0) for r in rows), 3),
    }


def format_summary(s: dict) -> str:
    def f(m):
        v = s[m]["value"]
        return "n/a" if v is None else f"{v:.0%} (95% CI {s[m]['ci95'][0]:.0%}–{s[m]['ci95'][1]:.0%}, n={s[m]['n']})"
    c = s["confusion"]
    return (f"[{s['variant']}] {s['cases']} cases, {s['errors']} errors\n"
            f"  agrees with you : {f('accuracy')}   (always-guess-majority would get {s['majority_baseline']:.0%})\n"
            f"  precision       : {f('precision')}\n"
            f"  recall          : {f('recall')}\n"
            f"  specificity     : {f('specificity')}\n"
            f"  confusion       : kept&fit {c['tp']} · kept&not-fit {c['fp']} · skipped&fit {c['fn']} · skipped&not-fit {c['tn']}\n"
            f"  latency         : {s['mean_latency_s']}s/case avg · list-price equivalent ${s['list_price_usd']}")


def format_delta(a: dict, b: dict) -> str:
    """b minus a, in percentage points, for the headline metrics."""
    def pt(m):
        va, vb = a[m]["value"], b[m]["value"]
        return "n/a" if va is None or vb is None else f"{(vb - va) * 100:+.0f} pts ({va:.0%} → {vb:.0%})"
    return (f"[{b['variant']} vs {a['variant']}] same {b['cases']} judgments\n"
            f"  agreement   : {pt('accuracy')}\n  precision   : {pt('precision')}\n"
            f"  recall      : {pt('recall')}\n  specificity : {pt('specificity')}\n"
            "  (small sets swing a lot: check the confidence intervals above before calling it a win)")
