#!/usr/bin/env python3
"""PreToolUse hook for read runs: writes are allowed only to the run's artifacts.

Hooked on Write, NotebookEdit and Bash. The allowed paths are absolute and colon separated
in PROMPTWERK_ARTIFACTS. Unset means the hook runs outside a Promptwerk run. Set but empty
means the run has no artifacts, so every write is blocked, not every write allowed.
"""
import json
import os
import re
import sys

# A shell line cannot be parsed safely. This recognizes what happens in practice when a read
# run "quickly fixes something on the side", not a run that sets out to evade the guard.
# ponytail: denylist instead of shell analysis; next level is a user without write access.
HARD = re.compile(
    r"(?:^|[|;&(\n]|\bsudo\s+|\bxargs\s+|\bdo\s+|\bthen\s+|\belse\s+)\s*"
    r"(?:rm|mv|chmod|chown|dd|truncate|shred)\b"
    r"|\bgit\s+(?:add|commit|push|checkout|switch|reset|restore|clean|rm|mv|stash"
    r"|apply|am|revert|merge|rebase|cherry-pick|tag|branch\s+-)\b"
    r"|\b(?:npm|pnpm|yarn|bun)\s+(?:i|install|ci|add|remove|uninstall|update|link)\b"
    r"|\bpip3?\s+(?:install|uninstall)\b"
    r"|\bsystemctl\s+(?:start|stop|restart|reload|enable|disable)\b"
    r"|\bdocker\s+(?:run|rm|restart|stop|compose)\b", re.M)

# Writing, but harmless as long as the target is an artifact, /tmp or /dev/null.
SOFT = re.compile(r">>?(?![&|])"
                  r"|\btee\b"
                  r"|\b(?:sed|perl)\s+[^|;&]*-i\b"
                  r"|\b(?:cp|touch|ln|mkdir)\b", re.M)


def decide(data, env):
    """Block reason, or None."""
    if env.get("PROMPTWERK_ARTIFACTS") is None:
        return None
    allowed = {os.path.realpath(p) for p in env["PROMPTWERK_ARTIFACTS"].split(":") if p}
    listed = ", ".join(sorted(allowed)) or "none"
    tail = " Describe the needed change in the artifact instead of making it."
    if data.get("tool_name") == "Bash":
        cmd = (data.get("tool_input") or {}).get("command") or ""
        if HARD.search(cmd):
            return ("Read-only run: changing shell commands (delete, move, git, package "
                    "installs, services) are blocked." + tail)
        if SOFT.search(cmd):
            targets = {"/dev/null", "/tmp"} | allowed | {os.path.basename(p) for p in allowed}
            if not any(t in cmd for t in targets):
                return (f"Read-only run: writing is allowed only to the agreed artifacts "
                        f"({listed}), plus /tmp and /dev/null." + tail)
        return None
    target = (data.get("tool_input") or {}).get("file_path")
    if not target or os.path.realpath(target) in allowed:
        return None
    return f"Read-only run: writing is allowed only to the agreed artifacts ({listed})." + tail


if __name__ == "__main__":
    try:
        why = decide(json.load(sys.stdin), os.environ)
    except (ValueError, AttributeError):
        why = "Read-only run: the hook could not read the tool call, so it is blocked."  # fail closed
    if why:
        print(json.dumps({"decision": "block", "reason": why}))
    sys.exit(0)
