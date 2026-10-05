#!/usr/bin/env python3
"""Turn the findings list of a finished read run into an implementation draft. No model.

    findings_draft.py <run id>

Reads the run's *-findings.json (knowledge/conventions.md), groups the findings by severity
into chained build runs (CRITICAL and HIGH first, then MEDIUM, then LOW) and writes a draft
to <data>/drafts/. The draft goes through the normal approval like any planned one.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_plan  # noqa: E402
import config  # noqa: E402
import planner  # noqa: E402
import project_facts  # noqa: E402

C = config.C
WAVES = [("fix-critical-high", "Fix CRITICAL and HIGH findings", ("CRITICAL", "HIGH")),
         ("fix-medium", "Fix MEDIUM findings", ("MEDIUM",)),
         ("fix-low", "Fix LOW findings", ("LOW",))]
BASE_USD, PER_FINDING_USD = 2.0, 1.0


def findings_of(rid):
    """(artifact name, list) of the run's first usable findings file, else (None, [])."""
    for path in sorted(glob.glob(os.path.join(config.RUNS, rid, "artifacts", "*findings.json"))):
        data = config.read_json(path)
        rows = data if isinstance(data, list) else (data or {}).get("findings") if isinstance(data, dict) else None
        rows = [r for r in rows or [] if isinstance(r, dict)]
        if rows:
            return os.path.basename(path), rows
    return None, []


def line(f):
    where = f"{f.get('file')}:{f.get('line')}" if f.get("file") else "no file given"
    return (f"- **{f.get('id', '?')}** [{f.get('severity', '?')}] {f.get('title', '')} ({where})\n"
            f"  Problem: {f.get('description', '')}\n  Fix: {f.get('remediation', '')}")


def build(rid):
    meta = config.read_json(os.path.join(config.RUNS, rid, "meta.json")) or {}
    if meta.get("status") != "done" or meta.get("mode") != "read":
        raise ValueError("only a finished read run can be turned into an implementation draft")
    name, rows = findings_of(rid)
    if not rows:
        raise ValueError("this run has no findings list (*-findings.json)")
    cwd = meta["cwd"]
    check = project_facts.check_command(cwd)
    cap = float(C["budgets"]["run_cap_usd"])
    runs = []
    for key, title, levels in WAVES:
        part = [f for f in rows if str(f.get("severity", "")).upper() in levels]
        if not part:
            continue
        prompt = "\n".join([
            "# Task", "",
            f"Fix the findings below in `{cwd}`. They come from the {meta.get('title')} run "
            f"({name}); severity {', '.join(levels)}.", "",
            "# Findings", "", *map(line, part), "",
            "# Scope limit", "",
            "Change only what these findings need. No refactoring beyond them, no new "
            "dependencies unless a fix cannot do without one.", "",
            "# Not fixed", "",
            "A finding that turns out wrong, already fixed or needs the operator: leave the code "
            "alone and list it with a one-line reason in your final report.", "",
            "# Done when", "",
            ("The check command passes: `" + check + "`.") if check else
            "Every finding is fixed or listed under Not fixed with a reason."])
        runs.append({"id": key, "title": f"{title} ({len(part)})", "cwd": cwd,
                     "mode": "build" if check else "free", "effort": "high",
                     "budget_usd": min(BASE_USD + PER_FINDING_USD * len(part), cap),
                     "needs": [runs[-1]["id"]] if runs else [], "artifacts": [],
                     "acceptance": [f"Every listed {'/'.join(levels)} finding is fixed or named "
                                    "under Not fixed with a reason in the run report.",
                                    f"The check command `{check}` exits 0." if check else
                                    "No change outside the files the findings name."],
                     "check": check, "post_run": None, "personas": [], "tools": [],
                     "prompt": prompt})
    if not runs:
        raise ValueError("no finding has a severity of CRITICAL, HIGH, MEDIUM or LOW")
    plan = {"topic": f"Implement the findings of '{meta.get('title')}'",
            "rationale": f"{len(rows)} finding(s) from {name}, in {len(runs)} chained run(s) by "
                         "severity, so the worst are fixed and checked first. Drafted without a "
                         "model; review the prompts before approval.",
            "clarifications": [], "runs": runs}
    did = planner.new_id()
    plan["_promptwerk"] = {"id": did, "topic_raw": plan["topic"], "project": cwd,
                           "open_findings": check_plan.check(plan), "attachments": [],
                           "from_plan": meta.get("plan")}
    os.makedirs(config.DRAFTS, exist_ok=True)
    config.write_json(os.path.join(config.DRAFTS, did + ".json"), plan)
    return did + ".json"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        print(build(sys.argv[1]))
    except ValueError as e:
        sys.exit(str(e))
