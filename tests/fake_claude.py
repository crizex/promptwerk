#!/usr/bin/env python3
"""Stand-in for the claude CLI, for tests and the demo. Never calls a model.

Set PROMPTWERK_CLAUDE_BIN to this file. It answers the three call shapes Promptwerk uses:
  * planner (--json-schema with the plan schema): the example todo plan, cwd = selected project
  * summary (--json-schema with the summary schema): a short summary
  * run (--output-format stream-json): a few stream events, writes the requested artifacts

A run prompt containing FAKE_QUESTION asks one question first; FAKE_OVER_BUDGET ends with
error_max_budget_usd; FAKE_SLOW keeps the run busy for 15 minutes (demo screenshots).
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def arg(name):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else None


def envelope(result, cost=0.42):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": json.dumps(result), "total_cost_usd": cost, "num_turns": 3}))


def emit(e):
    print(json.dumps(e), flush=True)


schema = arg("--json-schema")
if schema is not None:
    prompt = sys.stdin.read()
    if '"goal_met"' in schema:
        envelope({"goal_met": {"state": "yes", "reason": "Both runs finished and every promised point is covered."},
                  "verdict": "Done: todos take an optional due date and overdue ones show in red.",
                  "touch": "Open the app, add a todo with yesterday's date: it appears in red.",
                  "next_step": "", "conclusion": "The feature is in and tested.",
                  "happened": ["Mapped the todo code", "Added due dates with tests"],
                  "new_findings": [], "test": ["Add a todo with yesterday's date"],
                  "acceptance": [], "missing": [], "not_happened": [], "follow_up": ""}, 0.08)
    else:
        with open(os.path.join(ROOT, "examples", "todo-plan.json"), encoding="utf-8") as f:
            plan = json.load(f)
        m = re.search(r"The user selected this project: `([^`]+)`", prompt)
        cwd = m.group(1) if m else os.getcwd()
        for r in plan["runs"]:
            r["cwd"] = cwd
        t = re.search(r"# (?:USER|ORIGINAL) TOPIC\n\n(.+)", prompt)
        if t:
            plan["topic"] = t.group(1).strip()[:120]
        envelope(plan, 1.1)
    sys.exit(0)

# run mode
prompt = arg("-p") or ""
resumed = "--resume" in sys.argv
sid = arg("--session-id") or arg("--resume") or "fake"
emit({"type": "system", "subtype": "init", "session_id": sid})
if "FAKE_QUESTION" in prompt and not resumed:
    text = ('I need one decision.\n```promptwerk:question\n{"question": "Day only or day and time?", '
            '"options": ["Day only", "Day and time"]}\n```')
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    emit({"type": "result", "subtype": "success", "result": text, "total_cost_usd": 0.1, "num_turns": 1})
    sys.exit(0)
for path in filter(None, os.environ.get("PROMPTWERK_ARTIFACTS", "").split(":")):
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": path}}]}})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {os.path.basename(path)}\n\nWritten by the fake claude for a demo run.\n")
if "FAKE_SLOW" in prompt:
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "python3 -m unittest discover tests"}}]}})
    import time
    time.sleep(900)
if "FAKE_OVER_BUDGET" in prompt:
    emit({"type": "result", "subtype": "error_max_budget_usd", "result": "", "total_cost_usd": 2.0, "num_turns": 4})
    sys.exit(1)
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done. Report written."}]}})
emit({"type": "result", "subtype": "success", "result": "Done. Report written.", "total_cost_usd": 0.37,
      "num_turns": 5, "duration_ms": 4200})
