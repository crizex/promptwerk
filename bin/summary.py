#!/usr/bin/env python3
"""Write the summary of a whole plan.

    summary.py <data>/queue/<plan>.json

Writes <plan>.summary.json next to the plan, in two parts:

  * mechanical: run counts, totals, what the finish line did. Costs nothing and is there
    even when the second part fails.
  * narrated: one small call reads the run reports and answers what no counter can: is it
    done, what is missing, what can I try myself.
"""
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

C = config.C
RUNS = config.RUNS
REPORT_MAX = 14000  # per report; the summary needs the findings, not every checked line

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["goal_met", "verdict", "touch", "next_step", "conclusion", "happened",
                 "new_findings", "test", "acceptance", "missing", "not_happened", "follow_up"],
    "properties": {
        "goal_met": {
            "type": "object", "additionalProperties": False, "required": ["state", "reason"],
            "description": "The verdict against the sentence the operator typed, not against the "
                           "acceptance points the planner derived from it.",
            "properties": {
                "state": {"enum": ["yes", "partial", "no"], "description":
                          "yes = the operator needs no further task. partial = part of the "
                          "sentence is done, part is not. no = the goal is not reached."},
                "reason": {"type": "string", "description":
                           "One to three sentences. For partial or no: exactly which part of "
                           "the sentence is open and why."},
            }},
        "acceptance": {"type": "array", "description":
                       "Exactly the acceptance points promised in the plan, each answered, in "
                       "the same order, none added, none dropped.",
                       "items": {"type": "object", "additionalProperties": False,
                                 "required": ["criterion", "met", "evidence"],
                                 "properties": {
                                     "criterion": {"type": "string"},
                                     "met": {"enum": ["yes", "no", "unproven"], "description":
                                             "unproven = done but not shown (no access, no "
                                             "test account). Not the same as no."},
                                     "evidence": {"type": "string", "description":
                                                  "One sentence: where the report shows it, or "
                                                  "why proof is missing."}}}},
        "verdict": {"type": "string", "description": "One honest sentence: is this done?"},
        "touch": {"type": "string", "description":
                  "Where the operator sees the change with their own eyes, without a terminal. "
                  "Empty string if nothing is visible."},
        "next_step": {"type": "string", "description":
                      "The ONE next action if something is unusable without it. Else empty."},
        "conclusion": {"type": "string", "description": "Three to five sentences."},
        "happened": {"type": "array", "items": {"type": "string"}},
        "new_findings": {"type": "array", "items": {"type": "string"}},
        "test": {"type": "array", "items": {"type": "string"}, "description":
                 "Concrete things the operator can try without a terminal."},
        "missing": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["what", "who"],
            "properties": {"what": {"type": "string"},
                           "who": {"enum": ["run", "you", "not_requested"], "description":
                                   "run = promised, doable, not done. you = only the operator "
                                   "can do it. not_requested = never asked for. When in "
                                   "doubt, not 'run'."}}}},
        "not_happened": {"type": "array", "items": {"type": "string"}},
        "follow_up": {"type": "string", "description":
                      "One paragraph phrased as a task that covers the open points. It is "
                      "offered to the operator as the next input."},
    },
}


def plan_runs(plan_file):
    """Metadata of all runs of this plan, in plan order; the latest attempt wins."""
    name = os.path.basename(plan_file)
    found = [m for m in (config.read_json(p) for p in glob.glob(os.path.join(RUNS, "*", "meta.json")))
             if m and m.get("plan") == name]
    metas = {m["key"]: m for m in sorted(found, key=lambda m: m.get("started") or "")}
    plan = config.read_json(plan_file) or {}
    return [metas[r["id"]] for r in plan.get("runs", []) if r["id"] in metas]


def mechanical(plan_file):
    plan = config.read_json(plan_file) or {}
    pw = plan.get("_promptwerk") or {}
    metas = plan_runs(plan_file)
    fin = config.read_json(plan_file.replace(".json", ".finish.json"), []) or []
    # deployed: True green, False red, None = no deploy script for this project (not a flaw)
    deploys = [s for s in fin if s["command"] == "deploy" and s["code"] is not None]
    budgets = {r["id"]: r.get("budget_usd") for r in plan.get("runs", [])}
    return {
        "plan": os.path.basename(plan_file),
        "topic": plan.get("topic"),
        "rationale": plan.get("rationale"),
        "constraints": pw.get("constraints") or [],
        "goal": pw.get("topic_raw") or "",  # the operator's own sentence is the yardstick
        "clarification_answers": pw.get("clarification_answers") or [],
        "cwd": sorted({r["cwd"] for r in plan.get("runs", [])}),
        "runs": len(plan.get("runs", [])),
        "finished": sum(1 for m in metas if m["status"] in ("done", "incomplete")),
        "cost_usd": round(sum(float(m.get("cost_usd") or 0) for m in metas), 2),
        "budget_usd": round(sum(float(r.get("budget_usd") or 0) for r in plan.get("runs", [])), 2),
        "duration_s": sum(int(m.get("duration_s") or 0) for m in metas),
        "turns": sum(int(m.get("turns") or 0) for m in metas),
        "over_budget": [m["key"] for m in metas
                        if float(m.get("cost_usd") or 0) > float(budgets.get(m["key"]) or 0)],
        "check_red": [m["key"] for m in metas if (m.get("check") or {}).get("code") not in (0, None)],
        "denials": sum(len(m.get("denials") or []) for m in metas),
        "started": min((m.get("started") or "" for m in metas), default=""),
        "ended": max((m.get("ended") or "" for m in metas), default=""),
        "finish": fin,
        "deployed": all(s["code"] == 0 for s in deploys) if deploys else None,
        "promises": [{"run": r["id"], "point": a}
                     for r in plan.get("runs", []) for a in (r.get("acceptance") or [])],
    }


def reports(metas):
    out = []
    for m in metas:
        d = os.path.join(RUNS, m["run_id"], "artifacts")
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if name.endswith((".md", ".txt")):
                with open(os.path.join(d, name), encoding="utf-8", errors="replace") as f:
                    out.append(f"## {m['key']}: {m.get('title', '')} ({name})\n\n{f.read(REPORT_MAX)}")
    return out


def build_prompt(mech, metas):
    lines = [
        "You write the summary of a finished run plan. The reader is the operator: not a",
        "developer of this code, wants to know in thirty seconds whether it is done, what to",
        "try and what is still open. Write in the language of the operator's goal.",
        "",
        "Rules:",
        "- Honest before friendly. If something is only partly done, the verdict says so.",
        "- 'test' only lists what the operator can try without a terminal.",
        "- Invent nothing. What is not in the reports does not appear.",
        "- 'next_step' only when something is unusable without it. No nice-to-haves.",
        "- 'acceptance' answers the promised points below, each once, none more.",
        "- 'missing' separates by 'who'. Only what was promised AND doable and still not done",
        "  is 'run'. What only the operator can do is 'you'. What no task asked for is",
        "  'not_requested', even if it would be useful.",
        "- 'goal_met' measures against the operator's sentence, not the acceptance points.",
        "",
        f"# TASK\n\n{mech['topic']}\n\n{mech.get('rationale') or ''}",
    ]
    if mech["goal"]:
        lines.append("\n# OPERATOR GOAL (verbatim, the top yardstick)\n\n" + mech["goal"])
    if mech["clarification_answers"]:
        lines.append("\n# ANSWERED CLARIFICATIONS\n\n" + "\n".join(
            f"- {a['question']} -> {a['answer']}" for a in mech["clarification_answers"]))
    if mech["constraints"]:
        lines.append("\n# OPERATOR CONSTRAINTS\n\n" + "\n".join(f"- {v}" for v in mech["constraints"]))
    lines.append("\n# PROMISED ACCEPTANCE POINTS\n\n" + ("\n".join(
        f"- [{p['run']}] {p['point']}" for p in mech["promises"]) or "None. 'acceptance' stays empty."))
    lines.append("\n# MECHANICAL FINDINGS\n\n```json\n" + json.dumps(
        {k: mech[k] for k in ("runs", "finished", "cost_usd", "budget_usd", "duration_s",
                              "over_budget", "check_red", "denials", "deployed", "finish")},
        ensure_ascii=False, indent=1) + "\n```\n'deployed': null means no deploy is configured "
        "for this project. That is not a flaw.")
    lines.append("\n# RUN REPORTS\n\n" + "\n\n---\n\n".join(reports(metas)))
    return "\n".join(lines)


def narrated(mech, metas):
    p = subprocess.run(
        [C["claude_bin"], "-p", "--model", C["model"], "--effort", "medium",
         "--output-format", "json", "--max-budget-usd", str(C["budgets"]["summary_usd"]),
         "--tools", "", "--strict-mcp-config", "--json-schema", json.dumps(SCHEMA)],
        input=build_prompt(mech, metas), capture_output=True, text=True, timeout=900)
    env = json.loads(p.stdout)
    if env.get("subtype") != "success":
        raise RuntimeError(str(env.get("result") or p.stderr)[-400:])
    res = env["result"]
    out = res if isinstance(res, dict) else json.loads(res)
    out["_cost_usd"] = round(float(env.get("total_cost_usd") or 0), 4)
    return out


def generate(plan_file):
    mech = mechanical(plan_file)
    try:
        mech |= narrated(mech, plan_runs(plan_file))
    except Exception as e:  # the mechanical part must always land: numbers plus a visible error
        mech["error"] = str(e)[:400]
    target = plan_file.replace(".json", ".summary.json")
    config.write_json(target, mech)
    return target


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    plan = os.path.abspath(sys.argv[1])
    # Lock in the file system: the worker and the UI button may both ask for the same plan.
    lockfile = plan.replace(".json", ".summary.lock")
    try:
        fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - os.path.getmtime(lockfile) < 1000:  # a call is capped at 900 s
            sys.exit(f"Summary already running: {lockfile}")
        os.unlink(lockfile)
        fd = os.open(lockfile, os.O_CREAT | os.O_WRONLY)
    os.close(fd)
    try:
        print(generate(plan))
    finally:
        os.unlink(lockfile)
