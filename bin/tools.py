#!/usr/bin/env python3
"""Write knowledge/tools.json from what the claude CLI really has on this machine.

    tools.py refresh [--force]

One throwaway call in an empty temp dir; its init event lists tools, MCP tools, skills,
plugins and agents. At most every 12 hours unless --force. The planner and the plan check
read the file: a plan may then only name tools that exist.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

C = config.C
PATH = os.path.join(config.KNOW, "tools.json")
MAX_AGE_S = 12 * 3600


def names(items):
    """Init lists are plain strings or objects with a name, depending on the CLI version."""
    return sorted({i if isinstance(i, str) else str(i.get("name") or "") for i in items or []} - {""})


def refresh(force=False):
    if not force and os.path.isfile(PATH) and time.time() - os.path.getmtime(PATH) < MAX_AGE_S:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        p = subprocess.run([C["claude_bin"], "-p", "ok", "--model", C["model"],
                            "--output-format", "stream-json", "--verbose", "--max-budget-usd", "0.5"],
                           cwd=tmp, capture_output=True, text=True, timeout=300)
    init = None
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "system" and e.get("subtype") == "init":
            init = e
            break
    if not init:
        sys.exit(f"No init event in the CLI output (exit {p.returncode}).")
    tools = names(init.get("tools"))
    data = {"_comment": "Written by bin/tools.py refresh. Do not edit; run it again.",
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "model": init.get("model"),
            "tools": [t for t in tools if not t.startswith("mcp__")],
            "mcp_tools": [t for t in tools if t.startswith("mcp__")],
            "skills": names(init.get("skills")),
            "plugins": names(init.get("plugins")),
            "agents": names(init.get("agents")),
            "slash_commands": names(init.get("slash_commands"))}
    config.write_json(PATH, data)
    return data


if __name__ == "__main__":
    if sys.argv[1:2] != ["refresh"]:
        sys.exit(__doc__)
    d = refresh("--force" in sys.argv)
    print("tools.json is younger than 12 h, nothing to do (use --force)." if d is None else
          f"{PATH}: {len(d['tools'])} tools, {len(d['mcp_tools'])} MCP tools, "
          f"{len(d['skills'])} skills, {len(d['plugins'])} plugins, {len(d['agents'])} agents")
