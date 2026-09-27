#!/usr/bin/env python3
"""Stage B: mechanical check of a run plan. No model, no cost.

Catches exactly the class of error meta-prompting reliably produces: convincingly worded
references to things that do not exist.

    check_plan.py <plan.json>      prints a JSON list of findings, exit 1 if any
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

# Kinds that make a plan unrunnable. The server refuses approval on these; everything else
# is shown as a warning and left to the operator.
FATAL = {"chain", "structure", "path"}

# Paths in the prompt text that look like source files. Only with an extension, otherwise
# prose like "src/lib" would trigger.
PATH_RE = re.compile(r"(?<![\w/.])((?:src|app|lib|scripts|tests?)/[\w./()\[\]-]+"
                     r"\.(?:tsx?|jsx?|mjs|py|sql|json|md|css|html))")


def known_tools():
    p = os.path.join(config.KNOW, "tools.json")
    if not os.path.isfile(p):
        return None
    t = config.read_json(p, {})
    return {n for k in ("tools", "skills", "plugins", "agents") for n in t.get(k, [])}


def find_cycle(runs):
    ids = {r["id"] for r in runs}
    edges = {r["id"]: set(r.get("needs") or []) & ids for r in runs}

    def walk(k, path):
        if k in path:
            return path[path.index(k):] + [k]
        for n in edges.get(k, ()):
            if (c := walk(n, path + [k])):
                return c
        return None

    for k in edges:
        if (c := walk(k, [])):
            return c
    return None


def check(plan, projects=None):
    projects = config.C["projects"] if projects is None else projects
    tools = known_tools()
    out = []

    def f(rid, kind, text):
        out.append({"run": rid, "kind": kind, "problem": text})

    runs = plan.get("runs") or []
    if not runs:
        f("*", "structure", "plan has no runs")
    ids = [r.get("id") for r in runs]
    if len(ids) != len(set(ids)):
        f("*", "structure", f"duplicate run ids: {sorted({i for i in ids if ids.count(i) > 1})}")

    for r in runs:
        rid = r.get("id") or "?"
        if not re.fullmatch(r"[a-z0-9-]+", rid):
            f(rid, "structure", "id may only contain a-z, 0-9 and hyphen")
        cwd = r.get("cwd") or ""
        if not os.path.isdir(cwd):
            f(rid, "path", f"cwd does not exist: {cwd}")
            continue
        if projects and os.path.realpath(cwd) not in {os.path.realpath(p) for p in projects}:
            f(rid, "path", f"cwd is not a configured project: {cwd}")

        mode, prompt = r.get("mode"), r.get("prompt") or ""
        if mode == "build" and not r.get("check"):
            f(rid, "mode", "mode=build without a check command: success would be unknowable")
        if mode == "read" and r.get("check"):
            f(rid, "mode", "mode=read with a check command: reading changes nothing")
        if mode == "read" and not r.get("artifacts"):
            f(rid, "mode", "mode=read without artifacts: the run would have no result")
        if len(r.get("acceptance") or []) < 2:
            f(rid, "acceptance", "fewer than 2 acceptance points: the summary has nothing to "
                                 "judge 'done' against")

        for n in r.get("needs") or []:
            if n not in ids:
                f(rid, "chain", f"needs '{n}', which does not exist in the plan")
            if n == rid:
                f(rid, "chain", "depends on itself")

        for a in r.get("artifacts") or []:
            if os.path.isabs(a) or ".." in a.split("/"):
                f(rid, "artifact", f"artifact path must be relative and without '..': {a}")

        if tools is not None:
            for t in r.get("tools") or []:
                if t not in tools:
                    f(rid, "tool", f"'{t}' is not available on this machine (tools.json)")

        for hit in set(PATH_RE.findall(prompt)):
            if hit not in (r.get("artifacts") or []) and not os.path.exists(os.path.join(cwd, hit)):
                f(rid, "invented", f"prompt refers to '{hit}', which does not exist in {cwd}")

        if mode in ("build", "free"):
            if "# Scope limit" not in prompt:
                f(rid, "scope", "changing run without a '# Scope limit' section: it will reach "
                                "further than intended")
            if "Not fixed" not in prompt:
                f(rid, "scope", "no place for deliberately excluded defects ('Not fixed'): "
                                "they end up in the code")
            fed = any(a.endswith("-findings.json") for n in r.get("needs") or []
                      for a in next((x.get("artifacts") or [] for x in runs if x.get("id") == n), []))
            if fed and not any(w in prompt for w in ("CRITICAL", "HIGH", "severity", "cap")):
                f(rid, "scope", "run consumes a findings list but names no selection by "
                                "severity: 50 findings are not an order for 50 changes")

        effort = r.get("effort")
        if effort == "max":
            f(rid, "effort", "effort=max is reserved for the planner's critic")
        if mode == "build" and effort in ("xhigh", "max"):
            f(rid, "effort", f"mode=build with effort={effort}: implementing is mechanical, "
                             "at most high")
        if mode == "build" and len(r.get("personas") or []) > 1:
            f(rid, "effort", "mode=build with several personas multiplies cost for nothing")
        try:
            ok = 0 < float(r.get("budget_usd")) <= 100
        except (TypeError, ValueError):
            ok = False
        if not ok:
            f(rid, "budget", f"budget_usd={r.get('budget_usd')} is implausible")

    if (c := find_cycle(runs)):
        f("*", "chain", "cycle in needs: " + " -> ".join(c))
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    found = check(config.read_json(sys.argv[1], {}))
    print(json.dumps(found, ensure_ascii=False, indent=2))
    sys.exit(1 if found else 0)
