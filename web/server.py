#!/usr/bin/env python3
"""Promptwerk web UI: stdlib HTTP server, one page, JSON API.

    python3 web/server.py            (host, port and user come from config)

Security model:
  * binds 127.0.0.1 by default; put a TLS reverse proxy in front if you expose it
  * HTTP Basic auth, always on. No default password: one is generated on first start,
    stored with mode 600 in <data_dir>/web-password and printed once
  * every POST needs the header X-Promptwerk: 1 (a cross-site form cannot set it)
  * nothing runs without an approved draft; the worker only reads the queue
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin")
sys.path.insert(0, BIN)
import attachment_text  # noqa: E402
import check_plan  # noqa: E402
import config  # noqa: E402
import constraints  # noqa: E402

C = config.C
DRAFTS, QUEUE, RUNS, LOGS, ATTACH = (config.DRAFTS, config.QUEUE, config.RUNS,
                                     config.LOGS, config.ATTACH)
MAX_BODY = 32 * 1024 * 1024
MAX_FILES, MAX_FILES_BYTES = 10, 20 * 1024 * 1024
ATTACH_EXT = {".txt", ".md", ".json", ".csv", ".log", ".yaml", ".yml", ".xml", ".html",
              ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
              ".xlsx", ".xlsm", ".docx", ".pptx", ".odt", ".ods", ".odp"}
RESUMABLE = {"failed", "check_failed", "ratelimit", "budget_exhausted", "incomplete"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
STATIC = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css",
          "/favicon.svg": "favicon.svg"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2"}
# Raster images from runs and attachments are shown inline. SVG is left out on purpose:
# it can carry script, so it stays a download like every other unknown type.
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".gif": "image/gif", ".webp": "image/webp"}
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")
SUMMARIES = set()  # plans whose summary is being written right now


# ---------------------------------------------------------------- password

def password():
    """Configured password, or the generated one. Never a built-in default."""
    pw = C["web"]["password"]
    if pw:
        if len(pw) < 12:
            sys.exit("web.password is shorter than 12 characters. Use a longer one or leave "
                     "it empty to have one generated.")
        return pw
    path = os.path.join(config.DATA, "web-password")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        pass
    pw = secrets.token_urlsafe(18)
    os.makedirs(config.DATA, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pw + "\n")
    print(f"Generated web password (user '{C['web']['user']}'), stored in {path}:\n  {pw}",
          flush=True)
    return pw


# ---------------------------------------------------------------- state

def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def listing(d, suffix=".json"):
    return sorted(n for n in os.listdir(d) if n.endswith(suffix)) if os.path.isdir(d) else []


def all_metas():
    out = []
    for d in os.listdir(RUNS) if os.path.isdir(RUNS) else []:
        m = config.read_json(os.path.join(RUNS, d, "meta.json"))
        if isinstance(m, dict) and m.get("run_id"):
            out.append(m)
    return out


def forget_generation(gid):
    for suffix in (".json", ".log"):
        try:
            os.unlink(os.path.join(LOGS, f"gen-{gid}{suffix}"))
        except FileNotFoundError:
            pass


def generations():
    """Planner calls that have not produced a draft yet: running or failed."""
    out = []
    for n in listing(LOGS):
        if not n.startswith("gen-"):
            continue
        g = config.read_json(os.path.join(LOGS, n)) or {}
        if not g.get("id"):
            continue
        if any(os.path.exists(os.path.join(d, g["id"] + ".json")) for d in (DRAFTS, QUEUE)):
            forget_generation(g["id"])  # it produced its draft; the record has done its job
            continue
        try:
            with open(os.path.join(LOGS, f"gen-{g['id']}.log"), encoding="utf-8",
                      errors="replace") as f:
                log = f.read()[-3000:]
        except OSError:
            log = ""
        stage = [z for z in log.splitlines() if z.startswith("Stage ")]
        out.append({"id": g["id"], "topic": g.get("topic", "")[:300],
                    "project": g.get("project", ""), "started": g.get("started"),
                    "running": pid_alive(g.get("pid")), "stage": stage[-1] if stage else "",
                    "log": log})
    return out


def draft_card(name):
    d = config.read_json(os.path.join(DRAFTS, name)) or {}
    pw = d.get("_promptwerk") or {}
    return {"name": name, "topic": d.get("topic"), "rationale": d.get("rationale"),
            "project": pw.get("project", ""), "findings": pw.get("open_findings") or [],
            "clarifications": d.get("clarifications") or [],
            "runs": [{k: r.get(k) for k in ("id", "title", "mode", "effort", "budget_usd",
                                             "needs", "personas", "acceptance", "check", "cwd",
                                             "artifacts")} for r in d.get("runs") or []]}


def plan_card(name, metas):
    path = os.path.join(QUEUE, name)
    plan = config.read_json(path) or {}
    latest = {}
    for m in sorted((m for m in metas if m.get("plan") == name), key=lambda m: m.get("started") or ""):
        latest[m["key"]] = m
    runs = []
    for r in plan.get("runs") or []:
        m = latest.get(r["id"]) or {}
        runs.append({"id": r["id"], "title": r.get("title"), "mode": r.get("mode"),
                     "effort": r.get("effort"), "budget_usd": r.get("budget_usd"),
                     "needs": r.get("needs") or [], "status": m.get("status") or "queued",
                     "run_id": m.get("run_id"), "cost_usd": m.get("cost_usd") or 0,
                     "note": m.get("note") or "", "question": m.get("question"),
                     "last_tool": m.get("last_tool"), "last_target": m.get("last_target")})
    side = lambda s: config.read_json(path.replace(".json", s))  # noqa: E731
    return {"name": name, "topic": plan.get("topic"), "rationale": plan.get("rationale"),
            "goal": (plan.get("_promptwerk") or {}).get("topic_raw", ""), "runs": runs,
            "summary": side(".summary.json"), "finish": side(".finish.json"),
            "closed": os.path.exists(path.replace(".json", ".closed.json")),
            "summarizing": name in SUMMARIES}


def state():
    metas = all_metas()
    plans = [plan_card(n, metas) for n in listing(QUEUE) if n.count(".") == 1]
    return {"worker": config.read_json(os.path.join(LOGS, "worker.json")) or {},
            "projects": C["projects"], "generations": generations(),
            "drafts": [draft_card(n) for n in listing(DRAFTS)], "plans": plans,
            "today_usd": round(sum(float(m.get("cost_usd") or 0) for m in metas
                                   if str(m.get("started", "")).startswith(time.strftime("%Y-%m-%d"))), 2),
            "daily_cap_usd": C["budgets"]["daily_cap_usd"]}


def fingerprint():
    """Cheap change detector for the event stream: names and mtimes of everything the UI shows."""
    h = hashlib.sha256()
    for d in (DRAFTS, QUEUE, LOGS):
        for n in listing(d, ""):
            try:
                h.update(f"{n}{os.path.getmtime(os.path.join(d, n))}".encode())
            except OSError:
                pass
    for d in os.listdir(RUNS) if os.path.isdir(RUNS) else []:
        try:
            h.update(f"{d}{os.path.getmtime(os.path.join(RUNS, d, 'meta.json'))}".encode())
        except OSError:
            pass
    return h.hexdigest()


def run_detail(rid):
    meta = config.read_json(os.path.join(RUNS, rid, "meta.json"))
    if not meta:
        return None
    texts = []
    try:
        with open(os.path.join(RUNS, rid, "stream.jsonl"), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") == "assistant":
                    for b in e.get("message", {}).get("content", []):
                        if b.get("type") == "text" and b.get("text", "").strip():
                            texts.append(b["text"].strip())
                        elif b.get("type") == "tool_use":
                            i = b.get("input") or {}
                            texts.append(f"> {b.get('name')} {str(i.get('file_path') or i.get('command') or i.get('pattern') or '')[:160]}")
    except OSError:
        pass
    try:
        with open(os.path.join(RUNS, rid, "prompt.txt"), encoding="utf-8") as f:
            prompt = f.read()
    except OSError:
        prompt = ""
    art = os.path.join(RUNS, rid, "artifacts")
    return {"meta": meta, "prompt": prompt, "log": texts[-80:],
            "artifacts": sorted(os.listdir(art)) if os.path.isdir(art) else []}


# ---------------------------------------------------------------- actions

class Refused(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


def safe_name(name):
    name = os.path.basename(str(name or ""))
    if not NAME_RE.match(name):
        raise Refused(400, "invalid name")
    return name


def spawn(args, log=None):
    out = open(log, "a", encoding="utf-8") if log else subprocess.DEVNULL
    p = subprocess.Popen([sys.executable, *args], stdout=out, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    threading.Thread(target=p.wait, daemon=True).start()  # reap, no zombies
    return p


def start_planner(gid, topic, project, files):
    for d in (LOGS, DRAFTS):
        os.makedirs(d, exist_ok=True)
    args = [os.path.join(BIN, "planner.py"), "--project", project, "--id", gid]
    if files:
        args += ["--attach", *files]
    args += ["--", topic]  # a topic starting with "-" must not be read as an option
    log = os.path.join(LOGS, f"gen-{gid}.log")
    open(log, "w").close()
    p = spawn(args, log)
    config.write_json(os.path.join(LOGS, f"gen-{gid}.json"),
                      {"id": gid, "topic": topic, "project": project, "files": files,
                       "pid": p.pid, "started": time.time()})


def do_generate(body):
    topic = str(body.get("topic") or "").strip()
    if len(topic) < 8:
        raise Refused(400, "Describe the task in at least one sentence.")
    project = os.path.abspath(os.path.expanduser(str(body.get("project") or "")))
    if project not in C["projects"]:
        raise Refused(400, "Pick a project from the configured list (config: projects).")
    files = body.get("attachments") or []
    if len(files) > MAX_FILES:
        raise Refused(400, f"At most {MAX_FILES} attachments.")
    import planner
    gid = planner.new_id()
    decoded = []
    for a in files:
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(str(a.get("name") or "")))[:100]
        ext = os.path.splitext(name)[1].lower()
        if not name or ext not in ATTACH_EXT:
            raise Refused(400, f"Attachment type not allowed: {name or '?'}")
        try:
            decoded.append((name, base64.b64decode(str(a.get("data") or ""), validate=True)))
        except ValueError:
            raise Refused(400, f"Attachment is not valid base64: {name}")
    if sum(len(b) for _, b in decoded) > MAX_FILES_BYTES:
        raise Refused(400, "Attachments are larger than 20 MB together.")
    paths = []
    if decoded:
        folder = os.path.join(ATTACH, gid)
        os.makedirs(folder, mode=0o700)
        for name, data in decoded:
            p = os.path.join(folder, name)
            with open(p, "wb") as f:
                f.write(data)
            paths.append(p)
            t = attachment_text.text(p)
            if t is not None:
                with open(p + ".txt", "w", encoding="utf-8") as f:
                    f.write(t)
    start_planner(gid, topic, project, paths)
    return {"id": gid}


def do_approve(body):
    name = safe_name(body.get("name"))
    src = os.path.join(DRAFTS, name)
    plan = config.read_json(src)
    if not plan:
        raise Refused(404, "draft not found")
    dst = os.path.join(QUEUE, name)
    if os.path.exists(dst):
        raise Refused(409, "a plan with this name is already queued")
    if not C["projects"]:
        raise Refused(400, "No projects configured. Runs may only work inside configured projects.")
    fatal = [f for f in check_plan.check(plan) if f["kind"] in check_plan.FATAL]
    roots = {os.path.realpath(p) for p in C["projects"]}
    for r in plan.get("runs") or []:
        if os.path.realpath(r.get("cwd") or "") not in roots:
            fatal.append({"run": r.get("id"), "kind": "path", "problem": "cwd is not a configured project"})
    if fatal:
        raise Refused(400, "Cannot approve: " + "; ".join(f"{f['run']}: {f['problem']}" for f in fatal))
    answers_in = body.get("answers") or {}
    answers = []
    for i, c in enumerate(plan.get("clarifications") or []):
        given = str(answers_in.get(str(i)) or "").strip()
        if c.get("blocking") and not given:
            raise Refused(400, f"Answer the blocking question first: {c.get('question')}")
        answers.append({"question": c.get("question"), "answer": given or c.get("assumption") or "",
                        "assumed": not given})
    lines = constraints.split_lines(body.get("constraints"))
    extra = lines + [f"Clarification: {a['question']} -> {a['answer']}" for a in answers]
    goal = (plan.get("_promptwerk") or {}).get("topic_raw", "").strip()
    cap = float(C["budgets"]["run_cap_usd"])
    for r in plan["runs"]:
        r["prompt"] = constraints.insert(r["prompt"], extra)
        if goal:
            r["prompt"] = constraints.insert(r["prompt"], [goal], constraints.GOAL_HEAD, bullets=False)
        r["budget_usd"] = min(float(r["budget_usd"]), cap)
    pw = plan.setdefault("_promptwerk", {})
    pw.update(constraints=lines, clarification_answers=answers, approved=time.time())
    os.makedirs(QUEUE, exist_ok=True)
    config.write_json(dst, plan)
    os.unlink(src)
    return {"queued": name}


def meta_of(body):
    rid = safe_name(body.get("run_id"))
    meta = config.read_json(os.path.join(RUNS, rid, "meta.json"))
    if not meta:
        raise Refused(404, "run not found")
    return rid, meta


def do_cancel(body):
    rid, meta = meta_of(body)
    meta.update(status="discarded", note="Cancelled in the UI.")
    config.write_json(os.path.join(RUNS, rid, "meta.json"), meta)  # first, so nothing restarts it
    pid = meta.get("pid")
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            cmd = f.read().decode("utf-8", "replace")
        if "claude" in cmd and rid in cmd:  # never signal a reused pid
            os.kill(int(pid), signal.SIGINT)
    except (OSError, ValueError, TypeError):
        pass
    return {"ok": True}


def do_resume(body):
    rid, meta = meta_of(body)
    if meta.get("status") not in RESUMABLE:
        raise Refused(409, f"status '{meta.get('status')}' cannot be resumed")
    import worker
    if not worker.lock(meta["cwd"]):
        raise Refused(409, "Another run works in this directory. Try again when it is done.")
    spawn([os.path.join(BIN, "run.py"), "resume", rid])
    return {"ok": True}


def do_answer(body):
    rid, meta = meta_of(body)
    text = str(body.get("text") or "").strip()
    if meta.get("status") != "waiting_for_answer" or not text:
        raise Refused(409, "this run is not waiting for an answer")
    spawn([os.path.join(BIN, "run.py"), "answer", rid, text])
    return {"ok": True}


def do_budget(body):
    rid, meta = meta_of(body)
    try:
        usd = float(body.get("usd"))
    except (TypeError, ValueError):
        raise Refused(400, "budget must be a number")
    if not 0.5 <= usd <= 500:
        raise Refused(400, "budget must be between 0.5 and 500 USD")
    path = os.path.join(QUEUE, os.path.basename(meta["plan"]))
    plan = config.read_json(path)
    if not plan:
        raise Refused(404, "plan not found")
    for r in plan["runs"]:
        if r["id"] == meta["key"]:
            r["budget_usd"] = usd
    config.write_json(path, plan)
    meta["budget_usd"] = usd
    if meta["status"] == "budget_exhausted" or (meta["status"] == "incomplete"
                                                and "Budget cap" in str(meta.get("note"))):
        meta["status"] = "budget_exhausted"  # the worker resumes it on its next pass
    config.write_json(os.path.join(RUNS, rid, "meta.json"), meta)
    return {"ok": True}


def plan_path(body):
    name = safe_name(body.get("plan"))
    path = os.path.join(QUEUE, name)
    if name.count(".") != 1 or not os.path.exists(path):
        raise Refused(404, "plan not found")
    return name, path


def do_close(body, close=True):
    _, path = plan_path(body)
    mark = path.replace(".json", ".closed.json")
    if close:
        config.write_json(mark, {"closed": time.time()})
    elif os.path.exists(mark):
        os.unlink(mark)
    return {"ok": True}


def do_summary(body):
    name, path = plan_path(body)
    if name in SUMMARIES:
        raise Refused(409, "summary is already being written")
    SUMMARIES.add(name)

    def work():
        try:
            subprocess.run([sys.executable, os.path.join(BIN, "summary.py"), path],
                           capture_output=True, timeout=1000)
        finally:
            SUMMARIES.discard(name)
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


def do_discard(body):
    name = safe_name(body.get("name"))
    try:
        os.unlink(os.path.join(DRAFTS, name))
    except FileNotFoundError:
        raise Refused(404, "draft not found")
    return {"ok": True}


def gen_of(body):
    gid = safe_name(body.get("id"))
    g = config.read_json(os.path.join(LOGS, f"gen-{gid}.json"))
    if not g:
        raise Refused(404, "generation not found")
    return gid, g


def do_gen_retry(body):
    gid, g = gen_of(body)
    if pid_alive(g.get("pid")):
        raise Refused(409, "still running")
    start_planner(gid, g["topic"], g["project"], g.get("files") or [])
    return {"ok": True}


def do_gen_dismiss(body):
    gid, g = gen_of(body)
    if pid_alive(g.get("pid")):
        raise Refused(409, "still running")
    forget_generation(gid)
    return {"ok": True}


ACTIONS = {"generate": do_generate, "approve": do_approve, "discard": do_discard,
           "cancel": do_cancel, "resume": do_resume, "answer": do_answer, "budget": do_budget,
           "close": do_close, "reopen": lambda b: do_close(b, False), "summary": do_summary,
           "generation-retry": do_gen_retry, "generation-dismiss": do_gen_dismiss}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "promptwerk"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # no access log; nothing in it the operator needs

    def headers_out(self, code, ctype, length=None, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k, v in {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                     "Cache-Control": "no-store", "X-Frame-Options": "DENY",
                     "Content-Security-Policy": CSP, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()

    def send(self, code, data, ctype="application/json", extra=None):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.headers_out(code, ctype, len(body), extra)
        self.wfile.write(body)

    def authorized(self):
        head = self.headers.get("Authorization", "")
        if head.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(head[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                user, pw = "", ""
            ok_user = hmac.compare_digest(user.encode(), C["web"]["user"].encode())
            if hmac.compare_digest(pw.encode(), self.server.pw.encode()) and ok_user:
                return True
        time.sleep(0.5)  # ponytail: flat delay instead of a lockout table; enough on localhost
        self.send(401, {"error": "login required"},
                  extra={"WWW-Authenticate": 'Basic realm="Promptwerk", charset="UTF-8"'})
        return False

    def do_GET(self):
        if not self.authorized():
            return
        path = unquote(self.path.split("?", 1)[0])
        try:
            if path == "/api/state":
                return self.send(200, state())
            if path == "/api/events":
                return self.events()
            if path == "/api/projects":
                return self.send(200, C["projects"])
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "draft"]:
                d = config.read_json(os.path.join(DRAFTS, safe_name(parts[2])))
                return self.send(200, d) if d else self.send(404, {"error": "not found"})
            if len(parts) == 3 and parts[:2] == ["api", "run"]:
                d = run_detail(safe_name(parts[2]))
                return self.send(200, d) if d else self.send(404, {"error": "not found"})
            if len(parts) == 4 and parts[1] in ("artifact", "attachment"):
                base = (os.path.join(RUNS, safe_name(parts[2]), "artifacts")
                        if parts[1] == "artifact" else os.path.join(ATTACH, safe_name(parts[2])))
                return self.file(os.path.join(base, safe_name(parts[3])), download=True)
            if path in STATIC:
                return self.file(os.path.join(HERE, STATIC[path]))
            if path.startswith("/fonts/"):
                return self.file(os.path.join(HERE, "fonts", safe_name(path[7:])))
        except Refused as e:
            return self.send(e.code, {"error": str(e)})
        self.send(404, {"error": "not found"})

    def file(self, path, download=False):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self.send(404, {"error": "not found"})
        if download:  # user content: plain text, image or attachment, never rendered as a page
            ext = os.path.splitext(path)[1].lower()
            if ext in IMAGES:
                return self.send(200, data, IMAGES[ext])
            text = ext in (".md", ".txt", ".json", ".log", ".csv", ".yaml", ".yml")
            return self.send(200, data, "text/plain; charset=utf-8" if text else "application/octet-stream",
                             None if text else {"Content-Disposition": "attachment"})
        self.send(200, data, TYPES.get(os.path.splitext(path)[1], "application/octet-stream"))

    def events(self):
        self.headers_out(200, "text/event-stream")
        last = None
        try:
            while True:
                fp = fingerprint()
                self.wfile.write(b"data: change\n\n" if fp != last else b": ping\n\n")
                self.wfile.flush()
                last = fp
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        if not self.authorized():
            return
        if self.headers.get("X-Promptwerk") != "1":
            return self.send(403, {"error": "missing X-Promptwerk header"})
        action = self.path.strip("/").removeprefix("api/")
        fn = ACTIONS.get(action)
        if not fn:
            return self.send(404, {"error": "unknown action"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self.send(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
            return self.send(200, fn(body))
        except Refused as e:
            return self.send(e.code, {"error": str(e)})
        except ValueError:
            return self.send(400, {"error": "invalid JSON"})


def main():
    host, port = C["web"]["host"], int(os.environ.get("PROMPTWERK_PORT") or C["web"]["port"])
    config.ensure_dirs()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.pw = password()
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: listening on {host}. Basic auth over plain HTTP leaks the password; "
              "put a TLS reverse proxy in front.", flush=True)
    print(f"Promptwerk UI on http://{host}:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
