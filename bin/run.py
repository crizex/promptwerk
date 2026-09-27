#!/usr/bin/env python3
"""Execute exactly one run of an approved plan and watch it.

    run.py start  <queue/plan.json> <run key>
    run.py answer <run id> "<answer text>"
    run.py resume <run id>
    run.py check  <run id>          repeat only the check command

The run is headless. Its whole state is in <data>/runs/<run id>/meta.json; the UI only reads
that file and stream.jsonl and never needs to know the process.
"""
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import notify  # noqa: E402

C = config.C
B = C["budgets"]
RUNS, QUEUE = config.RUNS, config.QUEUE
BIN = os.path.dirname(os.path.abspath(__file__))

QUESTION_RE = re.compile(r"```promptwerk:question\s*(\{.*?\})\s*```", re.S)
# A run may correct its own check command once, when the command measures past the goal
# (typically a grep that hits backup or vendored dirs). Otherwise, on resume, the run repairs
# the code instead of the measurement.
CHECKCMD_RE = re.compile(r"```promptwerk:checkcmd\s*(\{.*?\})\s*```", re.S)

# In read mode the restriction is applied twice: via the hook and via the system prompt,
# because it is not guaranteed that Task subagents inherit the parent's hook.
READONLY_SYSTEM = (
    "You work in read-only mode. You may only create the artifact files named in the task. "
    "Never change existing code, configuration or data, not via subagents, shell commands or "
    "detours. If a change would be needed, describe it in the artifact instead of making it."
)

# Applies to every build run whether or not the planner thought of it: the operator wants a
# usable result, not a list of leftovers.
BUILD_SYSTEM = (
    "Done means done. What you notice within the task and can fix in the same run, you fix, "
    "with a test, instead of only reporting it. That holds especially for security holes: "
    "close them, do not document them. Prepared operational steps (migrations, scripts) you "
    "run on reachable instances and prove they took effect, as long as they are reversible. "
    "Only what is irreversible, needs someone else's credentials or is explicitly the "
    "operator's job stays open, with a reason in the report."
)

RESUME_TEXT = (
    "The run was interrupted (usage limit or a server error) and is resumed now. Look at what "
    "you already did and continue exactly there. Do not start over and do not repeat finished steps."
)


def now():
    return datetime.now().isoformat(timespec="seconds")


def meta_path(rid):
    return os.path.join(RUNS, rid, "meta.json")


def read_meta(rid):
    return config.read_json(meta_path(rid))


def write_meta(rid, meta):
    # 'discarded' wins over anything a cancelled process still writes afterwards. Without this
    # the dying run sets 'failed' and the worker picks it up again by itself.
    before = (config.read_json(meta_path(rid)) or {}).get("status")
    if before == "discarded" and meta.get("status") != "discarded":
        return
    config.write_json(meta_path(rid), meta)
    if before != meta.get("status"):
        notify.run_status(meta)


def set_worker_line(text):
    """Same file the worker writes; it feeds the status line in the UI during long runs."""
    try:
        os.makedirs(config.LOGS, exist_ok=True)
        config.write_json(os.path.join(config.LOGS, "worker.json"),
                          {"text": text, "time": datetime.now().timestamp()})
    except OSError:
        pass


def artifacts_inside(run):
    """Only artifacts that really lie under run['cwd'].

    'artifacts' comes from model text. An entry like '../../etc/cron.d/x' or an absolute path
    would otherwise turn the write guard into a permission outside the project.
    """
    root = os.path.realpath(run["cwd"])
    inside = []
    for a in run.get("artifacts") or []:
        full = os.path.realpath(os.path.join(run["cwd"], a))
        if full == root or full.startswith(root + os.sep):
            inside.append(a)
    return inside


def environment(run):
    return {**os.environ, "PROMPTWERK_ARTIFACTS":
            ":".join(os.path.join(run["cwd"], a) for a in artifacts_inside(run))}


def remaining_budget(run, meta):
    """What is left of the approved cap. At least 0.50 USD so a resume can still report back."""
    try:
        spent = float((meta or {}).get("cost_usd") or 0)
    except (TypeError, ValueError):
        spent = 0.0
    return max(0.5, round(float(run["budget_usd"]) - spent, 4))


def plan_path(meta):
    return os.path.join(QUEUE, os.path.basename(meta["plan"]))


def raise_budget(run, meta):
    """Write the next budget step into plan and meta. None when the cap is reached.

    Cap: raise_multiple times the planned budget, at most run_cap_usd. Written into the plan
    because remaining_budget() and the UI read the cap from there.
    """
    current = float(run["budget_usd"])
    start = float(meta.get("budget_start") or current)
    meta["budget_start"] = start
    cap = round(min(start * B["raise_multiple"], B["run_cap_usd"]), 2)
    if current >= cap - 0.01:
        return None
    new = round(min(current * B["raise_factor"], cap), 2)
    plan = config.read_json(plan_path(meta))
    hits = [r for r in (plan or {}).get("runs", []) if r.get("id") == meta.get("key")]
    if not hits:
        return None  # without the plan file the raise would have no effect
    for r in hits:
        r["budget_usd"] = new
    config.write_json(plan_path(meta), plan)
    run["budget_usd"] = meta["budget_usd"] = new
    meta.setdefault("budget_raises", []).append({"time": now(), "from": current, "to": new})
    return new


def hook(script, matcher):
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [
        {"type": "command", "command": f"{sys.executable} {os.path.join(BIN, script)}",
         "timeout": 10}]}]}})


def arguments(run, meta=None):
    """Tools and rights follow from the mode."""
    tools = [str(t).strip() for t in (run.get("tools") or []) if str(t).strip()]
    extra = ("\n\nPlanned for this run: " + ", ".join(tools) + ". Use them where they help."
             if tools else "")
    rules = config.house_rules()
    if rules:
        extra += ("\n\n# House rules\n\nThey apply to every run, even if the task below does "
                  "not repeat them.\n\n" + rules)
    args = ["--model", run.get("model") or C["model"], "--effort", run["effort"],
            "--max-budget-usd", str(remaining_budget(run, meta)),
            "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    if run["mode"] == "read":
        # No Edit; Write and Bash go through the write guard.
        args += ["--tools", "Read,Grep,Glob,Bash,Task,Write",
                 "--permission-mode", "bypassPermissions",
                 "--settings", hook("write_guard.py", "Write|NotebookEdit|Bash"),
                 "--append-system-prompt", READONLY_SYSTEM + extra]
    else:
        # rights.py blocks the irreversible; everything else stays open, or a build run
        # stalls on every small thing.
        args += ["--permission-mode", "bypassPermissions",
                 "--settings", hook("rights.py", "Bash"),
                 "--append-system-prompt", BUILD_SYSTEM + extra]
    return args


def stderr_tail(rid):
    """Last lines of stderr.log: the reason the CLI did not even start."""
    try:
        with open(os.path.join(RUNS, rid, "stderr.log"), encoding="utf-8", errors="replace") as f:
            lines = [z.strip() for z in f.read().splitlines() if z.strip()]
    except OSError:
        return ""
    return " | ".join(lines[-5:])[:600]


def start_process(rid, meta, run, argv):
    if not os.path.isdir(run["cwd"]):
        meta.update(status="failed", note=f"cwd does not exist: {run['cwd']}")
        write_meta(rid, meta)
        sys.exit(meta["note"])
    err = open(os.path.join(RUNS, rid, "stderr.log"), "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(argv, cwd=run["cwd"], stdout=subprocess.PIPE, stderr=err,
                                text=True, bufsize=1, env=environment(run))
    except OSError as e:
        err.close()
        meta.update(status="failed", note=f"process could not start: {e}")
        write_meta(rid, meta)
        sys.exit(meta["note"])
    meta["pid"] = proc.pid
    # The watcher is this process, not claude: during 'checking' claude has exited and
    # run.py is still alive. The worker uses this to tell a live run from a dead one.
    meta["watcher_pid"] = os.getpid()
    write_meta(rid, meta)
    result = watch(proc, rid, meta)
    proc.wait()
    err.close()
    return result


def watch(proc, rid, meta):
    """Read the NDJSON stream, store it and keep meta.json current."""
    stream = open(os.path.join(RUNS, rid, "stream.jsonl"), "a", encoding="utf-8")
    result, since_write = None, 0
    for line in proc.stdout:
        stream.write(line)
        stream.flush()
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = e.get("type")
        if kind == "rate_limit_event":
            info = e.get("rate_limit_info", {})
            meta["rate_limit"] = info
            # 'allowed_warning' is a threshold, not a stop: a running job keeps going.
            # The worker just does not start anything new in that state.
            if info.get("isUsingOverage") or not str(info.get("status", "")).startswith("allowed"):
                meta.update(status="ratelimit", ratelimit_until=info.get("resetsAt"),
                            note="Paused to avoid paid overage. Resumes after the reset.")
                write_meta(rid, meta)
                proc.send_signal(signal.SIGINT)
                stream.close()
                return None
        elif kind == "assistant":
            for block in e.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    meta["last_tool"] = block.get("name")
                    i = block.get("input") or {}
                    meta["last_target"] = (i.get("file_path") or i.get("pattern")
                                           or i.get("command") or "")[:160]
                elif block.get("type") == "text" and block.get("text", "").strip():
                    meta["last_text"] = block["text"].strip()[-600:]
        elif kind == "result":
            result = e
        since_write += 1
        if since_write >= 5 or kind == "result":
            write_meta(rid, meta)
            set_worker_line(f"{meta.get('key', rid)}: {meta.get('last_tool') or 'working'}"
                            + (f" - {meta['last_target'][:80]}" if meta.get("last_target") else ""))
            since_write = 0
    stream.close()
    return result


def run_command(cmd, cwd, limit=1800):
    """Exit code and output. A timeout counts as a red command (code 124, like timeout(1))."""
    try:
        p = subprocess.run(["bash", "-lc", cmd], cwd=cwd, capture_output=True, text=True,
                           timeout=limit)
    except subprocess.TimeoutExpired as e:
        parts = [t.decode("utf-8", "replace") if isinstance(t, bytes) else (t or "")
                 for t in (e.stdout, e.stderr)]
        return 124, (f"Aborted: {limit} s limit exceeded.\n" + "".join(parts).strip())[-4000:]
    return p.returncode, (p.stdout + p.stderr).strip()[-4000:]


def finish(rid, meta, run, result):
    """Detect a question, otherwise check and post-run."""
    if result is None:
        if meta.get("status") != "ratelimit":
            meta.update(status="failed",
                        note=stderr_tail(rid) or "No result event: the process ended early.")
        write_meta(rid, meta)
        return
    meta["cost_usd"] = round(meta.get("cost_usd", 0) + (result.get("total_cost_usd") or 0), 4)
    meta["turns"] = meta.get("turns", 0) + (result.get("num_turns") or 0)
    meta["duration_s"] = meta.get("duration_s", 0) + round((result.get("duration_ms") or 0) / 1000)
    meta["denials"] = result.get("permission_denials") or []
    text = result.get("result") or ""

    if result.get("subtype") == "error_max_budget_usd":
        old = run["budget_usd"]
        if (new := raise_budget(run, meta)):
            meta.update(status="budget_exhausted",
                        note=f"Cap of {old} USD used up ({meta['cost_usd']} USD billed), raised "
                             f"to {new} USD. The worker resumes by itself.")
        else:
            # 'incomplete' is a finished state: follow-up runs and the summary carry on.
            meta.update(status="incomplete",
                        note=f"Budget cap of {old} USD reached ({meta['cost_usd']} USD billed). "
                             "The plan continues without this run; raise the cap by hand and resume.")
        write_meta(rid, meta)
        return

    if result.get("is_error"):
        meta.update(status="failed", note=str(result.get("api_error_status") or text[-600:]))
        write_meta(rid, meta)
        return

    if (m := QUESTION_RE.search(text)):
        try:
            meta.update(question=json.loads(m.group(1)), status="waiting_for_answer")
            write_meta(rid, meta)
            return
        except json.JSONDecodeError:
            pass  # a broken question block is no reason to lose the run
    meta["question"] = None

    if (m := CHECKCMD_RE.search(text)):
        try:
            correct_check(meta, run, json.loads(m.group(1)))
        except (json.JSONDecodeError, KeyError, AttributeError):
            pass
    check_and_close(rid, meta, run)


def check_and_close(rid, meta, run):
    if run.get("check"):
        meta["status"] = "checking"
        write_meta(rid, meta)
        code, out = run_command(run["check"], run["cwd"])
        meta["check"] = {"command": run["check"], "code": code, "output": out}
        if code != 0:
            meta["status"] = "check_failed"
            write_meta(rid, meta)
            return
    if run.get("post_run"):
        code, out = run_command(run["post_run"], run["cwd"])
        meta["post_run"] = {"command": run["post_run"], "code": code, "output": out[-1500:]}

    target = os.path.join(RUNS, rid, "artifacts")
    os.makedirs(target, exist_ok=True)
    meta["artifacts_found"] = []
    for a in artifacts_inside(run):
        src = os.path.join(run["cwd"], a)
        if os.path.isfile(src):
            with open(src, "rb") as s, open(os.path.join(target, os.path.basename(a)), "wb") as d:
                d.write(s.read())
            meta["artifacts_found"].append(a)
    missing = [a for a in run.get("artifacts") or [] if a not in meta["artifacts_found"]]
    meta["status"] = "incomplete" if missing else "done"
    notes = ["Artifacts not produced: " + ", ".join(missing)] if missing else []
    if (meta.get("post_run") or {}).get("code"):
        notes.append(f"Post-run '{run['post_run']}' ended with code {meta['post_run']['code']}.")
    if notes:
        meta["note"] = " ".join(notes)
    meta["ended"] = now()
    write_meta(rid, meta)


def read_run(meta):
    plan = config.read_json(plan_path(meta)) or {}
    return next(r for r in plan["runs"] if r["id"] == meta["key"])


def correct_check(meta, run, data):
    """Take over a check command the run proposed. At most once per run; the old command
    stays in the record, so a run cannot talk its check down to 'true' in several steps."""
    if meta.get("check_correction"):
        return
    new, why = str(data["command"]).strip(), str(data["reason"]).strip()
    if not new or not why or new == run.get("check"):
        return
    meta["check_correction"] = {"time": now(), "before": run.get("check"), "after": new,
                                "reason": why}
    plan = config.read_json(plan_path(meta))
    for r in plan["runs"]:
        if r["id"] == meta["key"]:
            r["check"] = new
    config.write_json(plan_path(meta), plan)
    run["check"] = new


def check_failed_text(meta):
    c = meta.get("check") or {}
    return (
        "Your work is through, but the check command was red. It was:\n\n"
        f"    {c.get('command', '')}\n\nOutput (exit code {c.get('code')}):\n\n{c.get('output', '')}\n\n"
        "First read which part failed, then decide:\n"
        "If the work is incomplete, continue where you stopped, without repeating finished steps.\n"
        "If instead the check command itself measures past the goal (it hits backup, vendored or "
        "example dirs, checks a file that should not exist, or measures something the task did "
        "not ask for), do not change the code but output exactly one block:\n\n"
        "```promptwerk:checkcmd\n"
        '{"command": "<corrected command>", "reason": "<what it measured wrongly before>"}\n'
        "```\n\n"
        "The corrected command must still check everything the task requires: narrower yes, "
        "dropping requirements no. It is logged and shown to the operator. This works once."
    )


def start(plan_file, key):
    plan = config.read_json(plan_file) or {}
    run = next((r for r in plan.get("runs", []) if r["id"] == key), None)
    if run is None:
        sys.exit(f"Run '{key}' is not in the plan.")
    rid = str(uuid.uuid4())
    os.makedirs(os.path.join(RUNS, rid), exist_ok=True)
    with open(os.path.join(RUNS, rid, "prompt.txt"), "w", encoding="utf-8") as f:
        f.write(run["prompt"])
    meta = {"run_id": rid, "plan": os.path.basename(plan_file), "key": key,
            "title": run["title"], "cwd": run["cwd"], "mode": run["mode"],
            "effort": run["effort"], "budget_usd": run["budget_usd"],
            "personas": len(run.get("personas") or []), "artifacts": run.get("artifacts") or [],
            "status": "running", "started": now(), "cost_usd": 0, "turns": 0,
            "watcher_pid": os.getpid()}
    write_meta(rid, meta)
    print(rid, flush=True)
    result = start_process(rid, meta, run, [C["claude_bin"], "-p", run["prompt"],
                                            "--session-id", rid] + arguments(run, meta))
    finish(rid, meta, run, result)
    return rid


def resume(rid, text, kind="answer"):
    """Continue a run: after a question, a rate limit, an error or a red check.
    --resume with the same session id keeps the full context."""
    meta = read_meta(rid)
    run = read_run(meta)
    meta["status"] = "running"
    # Together with the status, or the dead pid of the previous process would sit in
    # meta.json and a worker pass in that window would declare the run dead.
    meta["watcher_pid"] = os.getpid()
    meta.pop("pid", None)
    if kind == "answer":
        meta["answers"] = meta.get("answers", []) + [
            {"time": now(), "question": meta.get("question"), "answer": text}]
        meta["question"] = None
    else:
        meta["resumes"] = meta.get("resumes", []) + [now()]
        meta.pop("ratelimit_until", None)
        meta["note"] = ""
    write_meta(rid, meta)
    result = start_process(rid, meta, run, [C["claude_bin"], "-p", text, "--resume", rid]
                           + arguments(run, meta))
    finish(rid, meta, run, result)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "start" and len(sys.argv) == 4:
        start(sys.argv[2], sys.argv[3])
    elif cmd == "answer" and len(sys.argv) == 4:
        resume(sys.argv[2], sys.argv[3])
    elif cmd == "resume":
        m = read_meta(sys.argv[2])
        resume(sys.argv[2], check_failed_text(m) if m.get("status") == "check_failed"
               else RESUME_TEXT, kind="resume")
    elif cmd == "check":
        m = read_meta(sys.argv[2])
        check_and_close(sys.argv[2], m, read_run(m))
        print(m["status"])
    else:
        sys.exit(__doc__)
