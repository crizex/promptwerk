#!/usr/bin/env python3
"""Turn a rough sentence into one finished prompt to paste into any chat. Nothing runs.

    prompt.py "I need a spreadsheet for my utility bill" [--attach FILE ...] [--id ID]

One call instead of planner, check and critic: there is no plan to check. The result lands in
<data>/prompts/<id>.json.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import planner  # noqa: E402

C = config.C
BRIEF = os.path.join(config.ROOT, "prompts", "prompt-brief.md")
BUDGET_USD = 3


def unfence(text):
    """A fence around the whole answer is the most common misreading of 'only the prompt'."""
    text = text.strip()
    if text.startswith("```") and text.endswith("```") and "\n" in text:
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("topic", nargs="?")
    ap.add_argument("--attach", nargs="*", default=[])
    ap.add_argument("--id", default="")
    a = ap.parse_args()
    topic = a.topic if a.topic is not None else sys.stdin.read()
    if not topic.strip():
        sys.exit("No topic given.")
    pid = a.id or planner.new_id()
    print("Stage A: prompt", file=sys.stderr, flush=True)
    text = "\n\n".join(x for x in (planner.read(BRIEF), "# USER TOPIC\n\n" + topic,
                                   planner.attachment_block(a.attach)) if x)
    cmd = [C["claude_bin"], "-p", "--model", C["model"], "--effort", "high",
           "--output-format", "json", "--max-budget-usd", str(BUDGET_USD),
           "--tools", "Read" if a.attach else "", "--strict-mcp-config"]
    try:
        p = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=900)
        d = json.loads(p.stdout)
    except subprocess.TimeoutExpired:
        sys.exit("  aborted: the call took longer than 15 minutes")
    except ValueError as e:
        sys.exit(f"  unreadable answer (exit {p.returncode}): {e}")
    # Decide on the JSON, not the exit code, as in the planner.
    result = unfence(str(d.get("result") or ""))
    if d.get("subtype") != "success" or not result:
        sys.exit(f"  aborted: subtype={d.get('subtype')} exit={p.returncode} {str(d.get('result'))[:400]}")
    os.makedirs(config.PROMPTS, exist_ok=True)
    config.write_json(os.path.join(config.PROMPTS, pid + ".json"), {
        "id": pid, "topic": topic, "time": time.time(),
        "cost_usd": round(float(d.get("total_cost_usd") or 0), 4),
        "attachments": ["%s/%s" % (os.path.basename(os.path.dirname(f)), os.path.basename(f))
                        for f in a.attach],
        "prompt": result})
    print(f"Prompt: prompts/{pid}.json\n\n{result}")


if __name__ == "__main__":
    main()
