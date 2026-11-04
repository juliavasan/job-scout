from jobscout import memory
from jobscout.llm import judge_prompts
from jobscout.models import RawJob


def row(title, company="Acme", applied="FALSE", ignore="FALSE", status="New", note="", n=0):
    return {"Job title": title, "Company": company, "Location": "Austin, TX", "Salary range": "not listed",
            "Applied": applied, "Ignore": ignore, "Status": status, "My feedback": note, "_row": n}


def test_sheet_rows_become_yes_no_newest_first():
    rows = [row("Old yes", applied="TRUE", n=2), row("No by status", status="Not a fit", n=3),
            row("Untouched", n=4), row("Both ticked", applied="TRUE", ignore="TRUE", n=5),
            row("Highlighted", n=6), row("New yes", status="Applied", note="great team <b>", n=7)]
    d = memory.from_sheet_rows(rows, flagged={6})
    assert [(x.verdict, x.title) for x in d] == [("yes", "New yes"), ("no", "Highlighted"), ("no", "No by status"), ("yes", "Old yes")]
    assert d[0].note == "great team (b)"           # can't open tags in the prompt


def test_select_caps_per_side_and_excludes_eval_jobs():
    ds = [memory.Decision("yes", f"Role {i}", "Acme") for i in range(15)] + [memory.Decision("no", "Bad", "Beta")]
    kept = memory.select(ds, exclude_keys={memory.decision_key("Acme", "Role 0", "")}, per_side=10)
    assert sum(d.verdict == "yes" for d in kept) == 10 and "Role 0" not in {d.title for d in kept}
    assert sum(d.verdict == "no" for d in kept) == 1


def test_block_is_in_the_judge_prompt_only_when_there_are_examples():
    job = RawJob(title="Data Engineer", company="Acme", location="Austin", url="https://acme.com/1", source="test")
    sys_none, _ = judge_prompts("brief", job, [], None)
    assert "\n<past_decisions>\n" not in sys_none and "{past}" not in sys_none
    sys_ex, _ = judge_prompts("brief", job, [], [memory.Decision("no", "Sales Lead", "Beta", note="quota")])
    assert "<past_decisions>\n- NO (not for me): Sales Lead at Beta. Their note: quota\n</past_decisions>" in sys_ex


def test_split_is_stratified_disjoint_and_fixed():
    cases = [{"case_id": f"c{i:02d}"} for i in range(1, 21)]
    labels = {c["case_id"]: {"label": int(i < 6), "note": ""} for i, c in enumerate(cases)}   # 6 fit, 14 not fit
    train, test = memory.split_labelled(cases, labels)
    ids = lambda xs: {c["case_id"] for c in xs}
    assert not ids(train) & ids(test) and len(train) + len(test) == 20
    assert sum(labels[c["case_id"]]["label"] for c in train) == 3 == sum(labels[c["case_id"]]["label"] for c in test)
    assert memory.split_labelled(cases, labels) == (train, test)
