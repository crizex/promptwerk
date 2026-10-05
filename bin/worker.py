#!/usr/bin/env python3
"""Work through approved plans in <data>/queue/.

    worker.py            one pass over what can start
    worker.py --loop     forever, every [worker] poll_seconds

Rules:
  * A run starts only when every run named in 'needs' is finished.
  * At most one run per working directory at a time (lock).
  * At most [worker] parallel runs at once.
  * Above [budgets] daily_cap_usd per day nothing starts by itself.
  * After the last run of a plan: the finish line (optional commit, push, deploy), then the
    summary. Commit and push are off by default.
"""
import json
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import notify  # noqa: E402

C = config.C
QUEUE, RUNS = config.QUEUE, config.RUNS
LOCKS = os.path.join(RUNS, ".locks")
BIN = os.path.dirname(os.path.abspath(__file__))
PARALLEL = int(C["worker"]["parallel"])
DAILY_CAP = float(C["budgets"]["daily_cap_usd"])
USAGE_LIMIT = float(C["worker"]["usage_limit"])
# 'discarded' is a final state like any other; without it a plan never counts as complete.
FINISHED = {"done", "incomplete", "discarded"}
ACTIVE = ("running", "checking", "waiting_for_answer")


def say(text):
    """Latest worker line for the UI; nobody reads the journal."""
    print(text, flush=True)
    try:
        os.makedirs(config.LOGS, exist_ok=True)
        config.write_json(os.path.join(config.LOGS, "worker.json"),
                          {"text": text, "time": time.time()})
    except OSError:
        pass


def retryable(m, rest=900, most=6):
    """May the worker resume this failed run by itself?

    529 (overloaded) is not over in seconds: several tries, with a pause. Any other error
    gets exactly one immediate retry.
    """
    if m.get("status") == "budget_exhausted":
        return False
    tries = m.get("resumes") or []
    if str(m.get("note")) != "529":
        return not tries
    if len(tries) >= most:
        return False
    last = os.path.getmtime(os.path.join(RUNS, m["run_id"], "meta.json"))
    return time.time() - last > rest


def all_metas():
    """All usable run metadata. Broken files drop out here, not later with a KeyError."""
    out = []
    for d in os.listdir(RUNS) if os.path.isdir(RUNS) else []:
        m = config.read_json(os.path.join(RUNS, d, "meta.json"))
        if isinstance(m, dict) and m.get("status") and m.get("cwd") and m.get("run_id"):
            out.append(m)
    return out


CHILDREN = []


def spawn(*args):
    """Start run.py in the background and return at once. Reap finished children."""
    CHILDREN[:] = [k for k in CHILDREN if k.poll() is None]
    CHILDREN.append(subprocess.Popen([sys.executable, os.path.join(BIN, "run.py"), *args]))


def day_cost(metas, day=None):
    day = day or time.strftime("%Y-%m-%d")
    return sum(float(m.get("cost_usd") or 0) for m in metas
               if str(m.get("started") or "").startswith(day))


def overlap(a, b):
    a, b = os.path.normpath(a), os.path.normpath(b)
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def lock(path):
    os.makedirs(LOCKS, exist_ok=True)
    try:
        os.mkdir(os.path.join(LOCKS, config.slug(path)))  # mkdir is atomic
        return True
    except FileExistsError:
        return False


def others_in_dir(meta, metas):
    """Other runs that work or wait for an answer in the directory of 'meta'."""
    return [m for m in metas if m["run_id"] != meta.get("run_id")
            and m["status"] in ACTIVE and config.slug(m["cwd"]) == config.slug(meta["cwd"])]


def clean_locks(metas):
    """A lock belongs to the run, not to the worker pass: it falls only when no run in that
    directory is running, checking or waiting for an answer."""
    busy = {config.slug(m["cwd"]) for m in metas if m["status"] in ACTIVE}
    for name in os.listdir(LOCKS) if os.path.isdir(LOCKS) else []:
        if name not in busy:
            try:
                os.rmdir(os.path.join(LOCKS, name))
            except OSError:
                pass


def alive(m):
    """Is there still a process behind 'running'/'checking'? The pid alone is not enough,
    it may have been reused, so the run must also be on its command line."""
    pid = m.get("watcher_pid")
    if not pid:
        return True
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            cmd = f.read().decode("utf-8", "replace").replace("\0", " ")
    except (OSError, ValueError):
        return False
    if "run.py" not in cmd:
        return False
    return m["run_id"] in cmd or (m["plan"] in cmd and m["key"] in cmd)


def reap_dead(metas):
    for m in metas:
        if m["status"] not in ("running", "checking") or alive(m):
            continue
        m.update(status="failed", note="Process is gone, probably ended by a restart.")
        try:
            config.write_json(os.path.join(RUNS, m["run_id"], "meta.json"), m)
        except OSError:
            continue
        say(f"{m['key']}: process gone, set to 'failed'")


def run_deploy(script, cwd, prefix):
    """Run a deploy script and report every line at once as the worker line."""
    # ponytail: the deadline is checked per output line; a fully silent script runs on.
    deadline = time.time() + 1800
    lines = []
    p = subprocess.Popen(["bash", "-l", script], cwd=cwd, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in p.stdout:
        lines.append(line)
        if line.strip():
            say(f"{prefix}: {line.strip()[:200]}")
        if time.time() > deadline:
            p.kill()
            lines.append("Aborted: 1800 s limit exceeded.\n")
            break
    p.wait()
    return p.returncode, "".join(lines).strip()


def changed_files(repo, path, since):
    """Changed files under <path> touched since <since>. File by file, never a folder add:
    other sessions may share the index and their leftovers do not belong in this commit.

    ponytail: the time filter is an approximation; the next step is a private index per run
    (GIT_INDEX_FILE), not a finer clock.
    """
    p = subprocess.run(["git", "-C", repo, "status", "--porcelain", "-z", "-uall", "--", path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    fields, files, i = p.stdout.split("\0"), [], 0
    while i < len(fields):
        entry, i = fields[i], i + 1
        if len(entry) < 4:
            continue
        status, name = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # rename: the source name is in the next field
        full = os.path.join(repo, name)
        try:
            if not os.path.exists(full) or os.path.getmtime(full) >= since:
                files.append(name)
        except OSError:
            files.append(name)
    return files


def repo_root(cwd):
    p = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def plan_start(plan_file, metas):
    times = []
    for m in metas:
        if m.get("plan") == os.path.basename(plan_file):
            try:
                times.append(os.path.getmtime(os.path.join(RUNS, m["run_id"], "prompt.txt")))
            except OSError:
                pass
    return min(times) if times else 0.0


def finish_line(plan, plan_file, metas):
    """Commit, push, deploy: serially, after the whole plan. Each step is opt-in in config."""
    fin = C["finish"]
    dirs = {r["cwd"] for r in plan["runs"] if r["mode"] != "read"}
    log = []
    since = plan_start(plan_file, metas)
    unfinished = sorted(m["key"] for m in metas if m.get("plan") == os.path.basename(plan_file)
                        and m.get("status") == "incomplete")

    def step(where, cmd, code, out):
        log.append({"dir": where, "command": cmd, "code": code, "ok": code in (0, None),
                    "output": out})
        if code not in (0, None):
            say(f"{os.path.basename(where)}: {cmd} failed (code {code})")

    for cwd in sorted(dirs):
        repo = repo_root(cwd) if (fin["auto_commit"] or fin["auto_push"]) else None
        if fin["auto_commit"] and repo:
            files = changed_files(repo, os.path.relpath(cwd, repo), since)
            if files is None:
                step(repo, "git status", 1, "git status did not answer, nothing committed")
            elif files:
                title = str(plan.get("topic") or "plan")[:70]
                quoted = " ".join(shlex.quote(f) for f in files)
                for cmd in [f"git add -- {quoted}",
                            f"git commit -q -m {shlex.quote('Promptwerk: ' + title)} -- {quoted}"]:
                    p = subprocess.run(["bash", "-c", cmd], cwd=repo, capture_output=True, text=True)
                    step(repo, cmd.split(" --")[0][:60], p.returncode,
                         (p.stdout + p.stderr).strip()[-800:])
                    if p.returncode:
                        break
        if fin["auto_push"] and repo:
            p = subprocess.run(["git", "push", "-q"], cwd=repo, capture_output=True, text=True)
            step(repo, "git push", p.returncode, (p.stdout + p.stderr).strip()[-800:])

        script = os.path.join(os.path.expanduser(fin["deploy_dir"]), config.slug(cwd) + ".sh") \
            if fin["deploy_dir"] else ""
        if not script:
            continue
        if unfinished:
            step(cwd, "deploy", None, "Not deployed: incomplete runs in this plan ("
                 + ", ".join(unfinished) + ").")
        elif os.path.isfile(script):
            code, out = run_deploy(script, cwd, "deploy/" + os.path.basename(script))
            step(cwd, "deploy", code, out[-1500:])
        else:
            step(cwd, "deploy", None, f"No {os.path.basename(script)} in deploy_dir, skipped.")

    config.write_json(plan_file.replace(".json", ".finish.json"), log)
    if not all(e["ok"] for e in log):
        say(f"{os.path.basename(plan_file)}: finish line had errors")
    return log


def find_cycle(runs):
    import check_plan
    return check_plan.find_cycle(runs)


def one_pass():
    if not os.path.isdir(QUEUE):
        return
    metas = all_metas()
    reap_dead(metas)
    clean_locks(metas)
    active = [m for m in metas if m["status"] in ("running", "checking")]

    # Waiting only holds back new runs. Finish lines cost no usage and must still happen,
    # otherwise finished work sits unshipped for hours.
    finish_only = False
    if len(active) >= PARALLEL:
        say(", ".join(f"{m['key']} {m['status']}" for m in active))
        finish_only = True
    if (total := day_cost(metas)) >= DAILY_CAP:
        say(f"Daily cap reached ({total:.0f} of {DAILY_CAP:.0f} USD), nothing new today")
        mark = os.path.join(config.LOGS, "daily-cap-" + time.strftime("%Y-%m-%d"))
        if not os.path.exists(mark):
            open(mark, "w").close()
            notify.send(f"Promptwerk: daily cap reached ({total:.0f} USD).", "daily_cap")
        finish_only = True

    paused = [m for m in metas if m["status"] == "ratelimit"]
    if paused:
        def free_at(m):
            for v in (m.get("ratelimit_until"), (m.get("rate_limit") or {}).get("resetsAt")):
                if v:
                    return float(v)
            return os.path.getmtime(os.path.join(RUNS, m["run_id"], "meta.json")) + 3600
        first = min(free_at(m) for m in paused)
        if time.time() < first:
            say(f"Usage limit, resuming from {time.strftime('%H:%M', time.localtime(first))}")
            finish_only = True
        for m in sorted(paused, key=free_at) if not finish_only else []:
            if free_at(m) > time.time() or not lock(m["cwd"]):
                continue
            say(f"resuming '{m['key']}' after the usage limit")
            spawn("resume", m["run_id"])
            return

    for m in metas:
        info = m.get("rate_limit") or {}
        if str(info.get("status", "")).startswith("allowed_") \
                and time.time() < float(info.get("resetsAt") or 0) \
                and float(info.get("utilization") or 1.0) >= USAGE_LIMIT:
            say(f"Usage at {info.get('utilization')}, no new runs until "
                f"{time.strftime('%H:%M', time.localtime(float(info['resetsAt'])))}")
            finish_only = True
            break

    if not finish_only:
        for m in metas:
            # Raised budget (run.py raise_budget or the UI): continue at once.
            if m["status"] == "budget_exhausted" \
                    and float(m.get("budget_usd") or 0) - float(m.get("cost_usd") or 0) > 0.5 \
                    and lock(m["cwd"]):
                say(f"resuming '{m['key']}' with the raised cap ({m.get('budget_usd')} USD)")
                spawn("resume", m["run_id"])
                return
        for m in metas:
            # A server error (500, broken stream) is usually temporary: one automatic retry.
            if m["status"] == "failed" and retryable(m) and lock(m["cwd"]):
                say(f"retrying '{m['key']}' once after an error ({m.get('note')})")
                spawn("resume", m["run_id"])
                return

    # Two rounds: first all finish lines, then starts, so an earlier plan in the same dir
    # cannot keep the finish line of a later, long finished plan waiting.
    waiting = set()
    for rnd, name in [(r, d) for r in (1, 2) for d in sorted(os.listdir(QUEUE))]:
        # side files (<plan>.finish.json, <plan>.summary.json) have two dots
        if not name.endswith(".json") or name.count(".") != 1:
            continue
        path = os.path.join(QUEUE, name)
        if os.path.exists(path.replace(".json", ".closed.json")):
            continue  # closed by hand in the UI
        plan = config.read_json(path)
        if not isinstance(plan, dict) or not isinstance(plan.get("runs"), list):
            say(f"{name}: unreadable, skipped")
            continue
        state = {}
        for r in plan["runs"]:
            mine = sorted((m for m in metas if m["plan"] == name and m["key"] == r["id"]),
                          key=lambda m: m.get("started") or "")
            state[r["id"]] = mine[-1]["status"] if mine else None
        missing = sorted({n for r in plan["runs"] for n in r.get("needs") or [] if n not in state})
        if missing:
            say(f"{name}: needs {', '.join(missing)}, which is not in the plan. Plan halted.")
            continue
        if (c := find_cycle(plan["runs"])):
            say(f"{name}: cycle in needs: {' -> '.join(c)}. Plan halted.")
            continue

        if all(s in FINISHED for s in state.values()):
            if rnd == 1 and not os.path.exists(path.replace(".json", ".finish.json")):
                busy = [m["cwd"] for m in metas if m["status"] in ACTIVE]
                own = [r["cwd"] for r in plan["runs"]]
                if any(overlap(a, b) for a in busy for b in own):
                    say(f"{name}: finish line waits, another run works in the same directory")
                    waiting.update(own)
                    continue
                say(f"{name}: all runs finished, finish line")
                finish_line(plan, path, metas)
                say(f"{name}: writing summary")
                subprocess.run([sys.executable, os.path.join(BIN, "summary.py"), path], check=False)
                notify.plan_done(config.read_json(path.replace(".json", ".summary.json"),
                                                  {"topic": plan.get("topic")}))
            continue
        if finish_only or rnd == 1:
            continue
        for r in plan["runs"]:
            if state[r["id"]] is not None:
                continue
            if not all(state.get(n) in FINISHED for n in r.get("needs") or []):
                continue
            if any(overlap(r["cwd"], w) for w in waiting):
                continue
            if not lock(r["cwd"]):
                continue
            say(f"{name}: starting '{r['id']}' ({r['mode']}, {r['effort']})")
            spawn("start", path, r["id"])
            return
    if not finish_only:
        say("ready, nothing to start")


if __name__ == "__main__":
    config.ensure_dirs()
    if "--loop" in sys.argv:
        while True:
            try:
                one_pass()
            except Exception as e:  # one broken plan must not kill the worker
                print("error in pass:", e, file=sys.stderr, flush=True)
            time.sleep(int(C["worker"]["poll_seconds"]))
    else:
        one_pass()
