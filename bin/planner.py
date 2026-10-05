#!/usr/bin/env python3
"""Turn a rough sentence into a checked run plan. Nothing is executed.

    planner.py "check the signup form for accessibility" [--project DIR] [--attach FILE ...]
    echo "..." | planner.py

Three stages: draft (model) -> mechanical check (code) -> critic (model), then a final
mechanical check. The result lands in <data>/drafts/<id>.json and waits for approval.
"""
import argparse
import datetime
import json
import os
import secrets
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_plan  # noqa: E402
import config  # noqa: E402

C = config.C
SCHEMA = os.path.join(config.ROOT, "prompts", "plan.schema.json")
META = os.path.join(config.ROOT, "prompts", "meta-prompt.md")

CRITIC = """You review a Promptwerk run plan and return it revised.

Check strictly against this rubric:

1. Separation: does every persona find something no other finds? Merge overlaps, drop
   personas without a viewpoint of their own.
2. Verifiability: is every task worded so that one can tell afterwards whether it was met?
   Replace soft wording with checkable wording.
3. Completeness: is a perspective missing that this topic obviously needs? Add it.
4. Tool use: are available skills, plugins and agents used where they help? Does the prompt
   say WHEN to use them?
5. Cut: is the split into runs right? Merge tiny runs, split overloaded ones. Are the
   dependencies right?
6. Effort and budget: do effort and budget_usd fit the actual scope?
7. Artifacts: are name, structure and field schema unambiguous? Does the prompt say when the
   run is done?
8. Tone: a work order, not a pep talk. Remove fluff, superlatives and emoji.
9. Clarifications: does 'clarifications' hold every piece of data no run can find out itself?
   Look for silent assumptions (placeholders in legal texts, "if present", a chosen cap on a
   findings list, assumed credentials, tone and design decisions) and turn each into a
   question. Conversely, drop every question whose answer is in the code or the project
   knowledge. 'blocking' only without a reasonable assumption; otherwise 'blocking': false
   WITH 'assumption'.
10. Result, not intermediate state: does the plan end with something usable? A pure analysis
   plan is right only if the user explicitly asked for an analysis. Otherwise every 'read'
   run gets a 'build' run that takes it via 'needs' and fixes the findings.

The mechanical check found the findings listed below. Fix them all: they are references to
things that demonstrably do not exist, or rule violations.

Return only the revised run plan as JSON, in the same schema. Do not change what is already good.
"""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def read(path, limit=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read() if limit is None else "".join(f.readlines()[:limit])
    except OSError:
        return ""


def snapshot(project, readme_lines=80):
    """What the planner knows about a project: its profile, else a tiny automatic snapshot."""
    prof = os.path.join(config.KNOW, "projects", config.slug(project) + ".md")
    if os.path.isfile(prof):
        return read(prof)
    try:
        entries = sorted(e for e in os.listdir(project) if not e.startswith("."))[:60]
    except OSError:
        entries = []
    out = ["Top-level entries: " + ", ".join(entries)]
    for name in ("README.md", "CLAUDE.md", "AGENTS.md"):
        text = read(os.path.join(project, name), readme_lines)
        if text:
            out.append(f"Start of {name}:\n\n{text}")
            break
    return "\n\n".join(out)


def knowledge_block(project):
    out = ["# KNOWLEDGE", ""]
    for name in sorted(os.listdir(config.KNOW)) if os.path.isdir(config.KNOW) else []:
        if name.endswith(".md") and not name.startswith("house-rules"):
            out += [f"## File: knowledge/{name}", "", read(os.path.join(config.KNOW, name))]
    rules = config.house_rules()
    if rules:
        out += ["## File: knowledge/house-rules.md", "", rules]
    tools = os.path.join(config.KNOW, "tools.json")
    if os.path.isfile(tools):
        out += ["## File: knowledge/tools.json", "```json", read(tools), "```"]
    out += ["", "# PROJECTS", ""]
    for p in ([project] if project else C["projects"]):
        out += [f"## {p}", "", snapshot(p, 80 if project else 15), ""]
    if not project and not C["projects"]:
        out.append("No projects are configured. Use the directory the user names.")
    return "\n".join(out)


def project_block(project):
    if not project:
        return ""
    return (f"# SELECTED PROJECT\n\nThe user selected this project: `{project}`\n\n"
            f"Every run of this plan has `\"cwd\": \"{project}\"`, without exception. If the "
            "undertaking truly needs a second project, do not plan it; name it in the rationale.")


def attachment_block(files):
    if not files:
        return ""
    return ("# USER ATTACHMENTS\n\nThe user sent these files to explain the undertaking. "
            "**Read each one with the Read tool before planning.** For spreadsheets and "
            "documents a `.txt` version sits next to the original; read that one.\n\n"
            + "\n".join(f"- {f}" for f in files))


def claude(prompt, effort, tools):
    """One non-interactive call with structured output. Returns the plan dict."""
    cmd = [C["claude_bin"], "-p", "--model", C["model"], "--effort", effort,
           "--output-format", "json", "--max-budget-usd", str(C["budgets"]["planner_usd"]),
           "--tools", tools, "--strict-mcp-config", "--json-schema", read(SCHEMA)]
    # Prompt on stdin: an argv string is capped at 128 KiB on Linux, and knowledge plus
    # attachments can pass that.
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        sys.exit("  aborted: planner call took longer than 30 minutes")
    try:
        d = json.loads(p.stdout)
    except ValueError as e:
        sys.exit(f"  unreadable answer (exit {p.returncode}): {e}\n  start: "
                 f"{(p.stdout or p.stderr)[:400].strip() or '(empty)'}")
    log(f"  [{d.get('subtype')}] {d.get('num_turns')} turn(s), "
        f"{float(d.get('total_cost_usd') or 0):.2f} USD")
    # Decide on the JSON, not the exit code: the CLI has returned 1 next to a complete plan.
    if d.get("subtype") != "success" or not d.get("result"):
        sys.exit(f"  aborted: subtype={d.get('subtype')} is_error={d.get('is_error')} "
                 f"exit={p.returncode}\n  {str(d.get('result'))[:400]}")
    result = d["result"]
    return result if isinstance(result, dict) else json.loads(result)


def new_id():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def origin_block(name):
    """A follow-up knows what its origin plan wanted and what its summary left open."""
    if not name:
        return ""
    path = os.path.join(config.QUEUE, os.path.basename(name))
    plan = config.read_json(path) or {}
    s = config.read_json(path.replace(".json", ".summary.json")) or {}
    lines = [f"# FOLLOW-UP OF `{os.path.basename(name)}`", "",
             f"Its goal was: {(plan.get('_promptwerk') or {}).get('topic_raw') or plan.get('topic') or '?'}"]
    if s.get("verdict"):
        lines.append(f"Its summary: {s['verdict']}")
    lines += [f"- still missing ({m.get('who')}): {m.get('what')}" for m in s.get("missing") or []]
    lines.append("Plan only what is still open; do not redo what the origin plan finished.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("topic", nargs="?")
    ap.add_argument("--project", default="")
    ap.add_argument("--attach", nargs="*", default=[])
    ap.add_argument("--id", default="")
    ap.add_argument("--from-plan", default="", help="queued plan this task follows up on")
    a = ap.parse_args()
    log(f"PID: {os.getpid()}")
    topic = a.topic if a.topic is not None else sys.stdin.read()
    if not topic.strip():
        sys.exit("No topic given.")
    project = os.path.abspath(os.path.expanduser(a.project)) if a.project else ""
    if project and C["projects"] and project not in C["projects"]:
        sys.exit(f"Unknown project: {project}")
    draft_id = a.id or new_id()
    tools = "Read" if a.attach else ""

    log("Stage A: draft")
    context = "\n\n".join(x for x in (knowledge_block(project), project_block(project),
                                      origin_block(a.from_plan)) if x)
    plan = claude("\n\n".join(x for x in (read(META), context, "# USER TOPIC\n\n" + topic,
                                          attachment_block(a.attach)) if x), "xhigh", tools)

    log("Stage B: mechanical check")
    found = check_plan.check(plan)
    log(f"  {len(found)} finding(s)")

    log("Stage C: critic and revision")
    plan = claude("\n\n".join(x for x in (
        CRITIC, context, "# ORIGINAL TOPIC\n\n" + topic, attachment_block(a.attach),
        "# RUN PLAN\n```json\n" + json.dumps(plan, ensure_ascii=False, indent=2) + "\n```",
        "# FINDINGS OF THE MECHANICAL CHECK\n```json\n"
        + json.dumps(found, ensure_ascii=False, indent=2) + "\n```") if x), "max", tools)

    rest = check_plan.check(plan)
    plan["_promptwerk"] = {
        "id": draft_id, "topic_raw": topic, "project": project, "open_findings": rest,
        "from_plan": os.path.basename(a.from_plan) or None,
        # "<generation id>/<name>" so the UI can link /api/attachment/<gid>/<name>
        "attachments": ["%s/%s" % (os.path.basename(os.path.dirname(f)), os.path.basename(f))
                        for f in a.attach],
    }
    os.makedirs(config.DRAFTS, exist_ok=True)
    config.write_json(os.path.join(config.DRAFTS, draft_id + ".json"), plan)

    print(f"Draft: drafts/{draft_id}.json")
    print(f"Topic: {plan.get('topic')}\n\n{plan.get('rationale')}\n")
    for r in plan.get("runs", []):
        after = " after " + ",".join(r["needs"]) if r.get("needs") else ""
        print(f"  {r['id']:<14} {r['mode']:<6} {r['effort']:<7} {len(r.get('personas') or [])} "
              f"persona(s)  max {float(r['budget_usd']):.0f} USD{after}\n  {'':<14} {r['title']}")
    print(f"\nOpen findings: {len(rest)}")
    for f in rest:
        print(f"  [{f['kind']}] {f['run']}: {f['problem']}")


if __name__ == "__main__":
    main()
