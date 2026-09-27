#!/usr/bin/env python3
"""PreToolUse hook for build runs: blocks the irreversible, lets everything else through.

Build runs work with bypassPermissions. A rule like "ask before irreversible steps" in the
system prompt enforces nothing; this hook does.

Blocked is what the worker does itself (git add/commit/push) and what cannot be undone. A hit
does not abort the run: it gets the reason back and writes the step into its report as an
operator task.

ponytail: a denylist on the command text, no shell parsing. A run that deliberately wants to
get around it will (python -c, a script file); against that stands the system prompt. What
this catches is the well-meant reflex. Next step if needed: run as a user without write
access outside the project.
"""
import json
import os
import re
import shlex
import sys

BLOCKED = [
    (re.compile(r"\bgit\s+(?:add|commit|push|stash|rebase|filter-branch|filter-repo)\b"
                r"|\bgit\s+reset\s+[^;&|\n]*--hard|\bgit\s+clean\s+-\S*f"
                r"|\bgit\s+branch\s+-D\b"),
     "The worker records git state after the plan, file by file. Leave the working tree as is."),
    (re.compile(r"\b(?:DROP\s+(?:DATABASE|SCHEMA|TABLE)|TRUNCATE\s+TABLE)"
                r"\s+(?:IF\s+EXISTS\s+)?[\w\"`]", re.I),
     "Dropping databases or tables cannot be undone."),
    (re.compile(r"\bdocker\s+(?:volume\s+(?:rm|prune)|system\s+prune)\b"
                r"|\bdocker\s+compose\s+[^;&|\n]*\bdown\b[^;&|\n]*(?:\s-v\b|--volumes)"),
     "Docker volumes hold data; deleting them cannot be undone."),
    (re.compile(r"\bsystemctl\s+(?:stop|disable|mask)\b"
                r"|(?:^|[;&|\n]|\bssh\b[^;&|\n]*['\"])\s*(?:sudo\s+)?(?:reboot|shutdown|poweroff|halt)\b"),
     "Stopping services for good or rebooting machines is an operator decision."),
    (re.compile(r"\bmkfs\b|\bdd\s+[^;&|\n]*\bof=/dev/"),
     "Writing to devices cannot be undone."),
]

RM = re.compile(r"(?:^|[;&|(\n]|\bsudo\s+|\bxargs\s+)\s*rm\s+([^;&|\n]*)")


def rm_too_wide(cmd):
    """rm -r on a path close to the root (/, /home, /home/<user>/<project>).

    Deeper paths like node_modules, dist or build/ stay allowed, /tmp anyway.
    """
    for m in RM.finditer(cmd):
        try:
            parts = shlex.split(m.group(1))
        except ValueError:
            parts = m.group(1).split()
        if not any(p.startswith("-") and re.search(r"[rR]", p) or p == "--recursive"
                   for p in parts):
            continue
        for p in parts:
            if p.startswith("-"):
                continue
            path = os.path.normpath(os.path.expanduser(p.replace("$HOME", "~")))
            if not path.startswith("/") or path == "/tmp" or path.startswith("/tmp/"):
                continue
            if len([x for x in path.split("/") if x and x != "*"]) <= 3:
                return True
    return False


def reason(cmd):
    """Why the command is blocked, or None."""
    for pattern, why in BLOCKED:
        if pattern.search(cmd):
            return why
    if rm_too_wide(cmd):
        return "rm -r on a whole project or a system level cannot be undone."
    return None


if __name__ == "__main__":
    try:
        data = json.load(sys.stdin)
        why = reason((data.get("tool_input") or {}).get("command") or "")
    except (ValueError, AttributeError):
        # fail closed: a blocked call is retried by the run, an unchecked one cannot be undone
        why = "the hook could not read the command."
    if why:
        print(json.dumps({"decision": "block", "reason":
                          "Blocked for Promptwerk runs: " + why + " Do not run this step; "
                          "write it into your report as a task for the operator, with the reason."}))
    sys.exit(0)
