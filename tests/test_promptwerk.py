"""Promptwerk tests. Stdlib only, no model calls: tests/fake_claude.py stands in for the CLI.

    python3 -m unittest discover tests
"""
import base64
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fake_claude.py")
os.chmod(FAKE, 0o755)

# One temp world for the whole module. config.py reads the environment at import time.
TMP = tempfile.mkdtemp(prefix="promptwerk-test-")
DATA = os.path.join(TMP, "data")
PROJECT = os.path.join(TMP, "todo-app")
os.makedirs(PROJECT)
with open(os.path.join(PROJECT, "README.md"), "w") as f:
    f.write("# todo-app\n")
CONFIG = os.path.join(TMP, "config.toml")
with open(CONFIG, "w") as f:
    f.write(f'projects = ["{PROJECT}"]\n[notify]\nwebhook_url = ""\n')
ENV = {**os.environ, "PROMPTWERK_CONFIG": CONFIG, "PROMPTWERK_DATA_DIR": DATA,
       "PROMPTWERK_CLAUDE_BIN": FAKE}
ENV.pop("PROMPTWERK_PASSWORD", None)
os.environ.update({k: ENV[k] for k in ("PROMPTWERK_CONFIG", "PROMPTWERK_DATA_DIR", "PROMPTWERK_CLAUDE_BIN")})

sys.path.insert(0, os.path.join(ROOT, "bin"))
import check_plan  # noqa: E402
import config  # noqa: E402
import constraints  # noqa: E402
import model_choice  # noqa: E402
import project_facts  # noqa: E402
import register  # noqa: E402
import rights  # noqa: E402
import write_guard  # noqa: E402


def example_plan():
    with open(os.path.join(ROOT, "examples", "todo-plan.json")) as f:
        plan = json.load(f)
    for r in plan["runs"]:
        r["cwd"] = PROJECT
    return plan


def py(*args, timeout=60):
    return subprocess.run([sys.executable, *args], env=ENV, capture_output=True, text=True,
                          timeout=timeout, cwd=ROOT)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class Checks(unittest.TestCase):
    def test_data_dir_private_even_if_created_open(self):
        os.makedirs(DATA, exist_ok=True)
        os.chmod(DATA, 0o755)
        config.ensure_dirs()
        self.assertEqual(stat.S_IMODE(os.stat(DATA).st_mode), 0o700)

    def test_slug_unique_for_same_folder_name(self):
        self.assertEqual(config.slug(PROJECT), "todo-app")
        saved = config.C["projects"]
        try:
            config.C["projects"] = ["/work/a/backend", "/work/b/backend", "/work/web"]
            self.assertEqual(config.slug("/work/a/backend"), "a-backend")
            self.assertEqual(config.slug("/work/b/backend"), "b-backend")
            self.assertEqual(config.slug("/work/web"), "web")
        finally:
            config.C["projects"] = saved

    def test_example_plan_is_clean(self):
        self.assertEqual(check_plan.check(example_plan()), [])

    def test_cycle_and_missing_need_are_fatal(self):
        plan = example_plan()
        plan["runs"][0]["needs"] = ["add-due-dates"]
        kinds = {f["kind"] for f in check_plan.check(plan)}
        self.assertIn("chain", kinds)
        plan["runs"][0]["needs"] = ["nope"]
        self.assertTrue(any("nope" in f["problem"] for f in check_plan.check(plan)))

    def test_unknown_project_is_a_path_finding(self):
        plan = example_plan()
        plan["runs"][0]["cwd"] = TMP
        self.assertIn("path", {f["kind"] for f in check_plan.check(plan)})

    def test_build_without_scope_limit(self):
        plan = example_plan()
        plan["runs"][1]["prompt"] = "# Task\nDo it."
        self.assertIn("scope", {f["kind"] for f in check_plan.check(plan)})


class Constraints(unittest.TestCase):
    def test_insert_before_first_heading_and_append(self):
        text = constraints.insert("# Task\nx", ["a"])
        self.assertTrue(text.startswith("\n# Operator constraints"))
        self.assertLess(text.index("- a"), text.index("# Task"))
        text = constraints.insert(text, ["b"])
        self.assertEqual(text.count("# Operator constraints"), 1)
        self.assertIn("- b", text)

    def test_goal_block(self):
        text = constraints.insert("# Task\nx", ["Add due dates"], constraints.GOAL_HEAD, bullets=False)
        self.assertIn("# Operator goal", text)
        self.assertIn("Add due dates\n", text)


class Safety(unittest.TestCase):
    def test_rights_blocks_irreversible(self):
        for cmd in ("git push origin main", "git commit -m x", "rm -rf /", "systemctl stop nginx",
                    "docker volume prune -f", "psql -c 'DROP TABLE todos'"):
            self.assertIsNotNone(rights.reason(cmd), cmd)
        for cmd in ("ls -la", "python3 -m unittest", "rm -rf build/tmp", "git status"):
            self.assertIsNone(rights.reason(cmd), cmd)

    def test_write_guard(self):
        art = os.path.join(PROJECT, "docs", "map.md")
        env = {"PROMPTWERK_ARTIFACTS": art}
        w = lambda path: {"tool_name": "Write", "tool_input": {"file_path": path}}  # noqa: E731
        self.assertIsNone(write_guard.decide(w(art), env))
        self.assertIsNotNone(write_guard.decide(w(os.path.join(PROJECT, "app.py")), env))
        bash = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}}  # noqa: E731
        self.assertIsNotNone(write_guard.decide(bash("git commit -am x"), env))
        self.assertIsNotNone(write_guard.decide(bash("echo x > app.py"), env))
        self.assertIsNone(write_guard.decide(bash("grep -r todo ."), env))
        self.assertIsNotNone(write_guard.decide(w(art), {"PROMPTWERK_ARTIFACTS": ""}))

    def test_hook_output_blocks(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "rights.py")],
                           input=json.dumps({"tool_input": {"command": "git push"}}),
                           capture_output=True, text=True)
        self.assertEqual(json.loads(p.stdout)["decision"], "block")


class EndToEnd(unittest.TestCase):
    """Planner -> draft -> queue -> worker -> runs -> summary, all with the fake CLI."""

    def test_full_flow(self):
        p = py(os.path.join(ROOT, "bin", "planner.py"), "Add due dates to todos, overdue in red",
               "--project", PROJECT, "--id", "t1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Draft: drafts/t1.json", p.stdout)
        draft = config.read_json(os.path.join(DATA, "drafts", "t1.json"))
        self.assertEqual(draft["_promptwerk"]["topic_raw"], "Add due dates to todos, overdue in red")
        self.assertEqual(draft["_promptwerk"]["open_findings"], [])

        # approval is the server's job; here the file is moved by hand
        os.makedirs(os.path.join(DATA, "queue"), exist_ok=True)
        plan_file = os.path.join(DATA, "queue", "t1.json")
        os.replace(os.path.join(DATA, "drafts", "t1.json"), plan_file)

        def metas():
            out = {}
            runs = os.path.join(DATA, "runs")
            for d in os.listdir(runs) if os.path.isdir(runs) else []:
                m = config.read_json(os.path.join(runs, d, "meta.json"))
                if m:
                    out[m["key"]] = m
            return out

        def settle(key):
            for _ in range(100):
                m = metas().get(key)
                if m and m["status"] not in ("running", "checking"):
                    return m
                time.sleep(0.1)
            self.fail(f"{key} did not finish")

        worker = os.path.join(ROOT, "bin", "worker.py")
        py(worker)
        self.assertEqual(list(metas()), ["map-todos"])  # needs: the build run waits
        self.assertEqual(settle("map-todos")["status"], "done")
        py(worker)
        m = settle("add-due-dates")
        self.assertEqual(m["status"], "done", m.get("note"))
        self.assertEqual(m["check"]["code"], 0)
        self.assertTrue(os.path.isfile(os.path.join(DATA, "runs", m["run_id"], "artifacts",
                                                    "due-dates-report.md")))
        py(worker)
        summary = config.read_json(plan_file.replace(".json", ".summary.json"))
        self.assertEqual(summary["goal_met"]["state"], "yes")
        self.assertEqual(summary["finished"], 2)
        self.assertEqual(config.read_json(plan_file.replace(".json", ".finish.json")), [])
        item = next(it for it in register.open_items(PROJECT) if it["plan"] == "t1.json")
        self.assertEqual((item["kind"], item["who"]), ("open", "you"))
        import planner
        self.assertIn(item["id"], planner.knowledge_block(PROJECT))

    def test_question_and_budget(self):
        plan = example_plan()
        plan["runs"] = [plan["runs"][0], dict(plan["runs"][0], id="pricey")]
        plan["runs"][0]["prompt"] += "\nFAKE_QUESTION"
        plan["runs"][1]["prompt"] += "\nFAKE_OVER_BUDGET"
        os.makedirs(os.path.join(DATA, "queue"), exist_ok=True)
        path = os.path.join(DATA, "queue", "t2.json")
        config.write_json(path, plan)
        run = os.path.join(ROOT, "bin", "run.py")

        rid = py(run, "start", path, "map-todos").stdout.split()[0]
        meta = config.read_json(os.path.join(DATA, "runs", rid, "meta.json"))
        self.assertEqual(meta["status"], "waiting_for_answer")
        self.assertEqual(meta["question"]["question"], "Day only or day and time?")
        py(run, "answer", rid, "Day only")
        meta = config.read_json(os.path.join(DATA, "runs", rid, "meta.json"))
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["answers"][0]["answer"], "Day only")

        rid = py(run, "start", path, "pricey").stdout.split()[0]
        meta = config.read_json(os.path.join(DATA, "runs", rid, "meta.json"))
        self.assertEqual(meta["status"], "budget_exhausted")
        self.assertEqual(meta["budget_usd"], 3.0)  # 2 USD * raise_factor 1.5


class Knowledge(unittest.TestCase):
    def test_register_is_idempotent_and_closes_by_id(self):
        s = {"plan": "r1.json", "cwd": [PROJECT], "new_findings": ["Login has no rate limit"],
             "missing": [{"what": "Rotate the key", "who": "you"}, {"what": "Dark mode", "who": "not_requested"}]}
        register.add(s)
        register.add(s)
        mine = [it for it in register.open_items(PROJECT) if it["plan"] == "r1.json"]
        self.assertEqual(sorted(it["text"] for it in mine), ["Login has no rate limit", "Rotate the key"])
        register.add({"plan": "r2.json", "cwd": [PROJECT], "register_done": [mine[0]["id"]]})
        self.assertNotIn(mine[0]["id"], [it["id"] for it in register.open_items(PROJECT)])

    def test_check_command_derived_and_overridden(self):
        proj = os.path.join(TMP, "web-app")
        os.makedirs(proj, exist_ok=True)
        config.write_json(os.path.join(proj, "package.json"),
                          {"scripts": {"build": "x", "dev": "x", "test": "x"}, "dependencies": {"next": "15"}})
        self.assertEqual(project_facts.check_command(proj), "npm run test && npm run build")
        self.assertIn("Next.js", project_facts.facts(proj))
        self.assertIsNone(project_facts.check_command(PROJECT))
        over = os.path.join(config.KNOW, "check-commands.json")
        self.assertFalse(os.path.exists(over))  # never touch a real one
        try:
            config.write_json(over, {"web-app": "make check"})
            self.assertEqual(project_facts.check_command(proj), "make check")
        finally:
            os.unlink(over)

    def test_model_choice_forces_main_model_for_failing_kind(self):
        saved = config.C["models"]["cheap"]
        config.C["models"]["cheap"] = "claude-sonnet-5-5"
        try:
            ok = {"mode": "read", "model": "claude-sonnet-5-5", "status": "done"}
            bad = {"mode": "build", "model": "claude-sonnet-5-5", "status": "done",
                   "escalated": {"from": "claude-sonnet-5-5"}}
            self.assertFalse(model_choice.force_main("build", [bad] * 3))  # too few runs
            self.assertTrue(model_choice.force_main("build", [bad] * 2 + [dict(ok, mode="build")] * 2))
            self.assertFalse(model_choice.force_main("read", [ok] * 5))
            self.assertIn("| build | sonnet | 4 | 2 | use the main model |",
                          model_choice.table([bad] * 2 + [dict(ok, mode="build")] * 2))
        finally:
            config.C["models"]["cheap"] = saved


class CheapModel(unittest.TestCase):
    """Escalation and tools.json need their own config: a cheap model and a private knowledge dir."""

    @classmethod
    def setUpClass(cls):
        cls.know = os.path.join(TMP, "know")
        os.makedirs(cls.know, exist_ok=True)
        cls.cfg = os.path.join(TMP, "config-cheap.toml")
        with open(cls.cfg, "w") as f:
            f.write(f'projects = ["{PROJECT}"]\nknowledge_dir = "{cls.know}"\n'
                    '[models]\ncheap = "claude-sonnet-5-5"\n[notify]\nwebhook_url = ""\n')
        cls.env = {**ENV, "PROMPTWERK_CONFIG": cls.cfg}

    def py(self, *args):
        return subprocess.run([sys.executable, *args], env=self.env, capture_output=True, text=True,
                              timeout=60, cwd=ROOT)

    def test_red_check_escalates_once_to_main_model(self):
        plan = example_plan()
        run = dict(plan["runs"][1], id="cheap", needs=[], check="false", model="claude-sonnet-5-5")
        plan["runs"] = [run]
        path = os.path.join(DATA, "queue", "c1.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        config.write_json(path, plan)
        rid = self.py(os.path.join(ROOT, "bin", "run.py"), "start", path, "cheap").stdout.split()[0]
        meta = config.read_json(os.path.join(DATA, "runs", rid, "meta.json"))
        self.assertEqual(meta["status"], "check_failed")
        self.assertEqual(meta["escalated"]["from"], "claude-sonnet-5-5")
        self.assertEqual(meta["model"], "claude-opus-5-5")
        self.assertNotIn("resumes", meta)  # the worker's own retry is still available
        shutil.rmtree(os.path.join(DATA, "runs", rid))
        os.unlink(path)

    def test_tools_refresh_writes_tools_json(self):
        p = self.py(os.path.join(ROOT, "bin", "tools.py"), "refresh", "--force")
        self.assertEqual(p.returncode, 0, p.stderr)
        t = config.read_json(os.path.join(self.know, "tools.json"))
        self.assertEqual((t["tools"], t["mcp_tools"], t["plugins"]), (["Bash", "Read"], ["mcp__notes__search"], ["review"]))
        self.assertIn("nothing to do", self.py(os.path.join(ROOT, "bin", "tools.py"), "refresh").stdout)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "web", "server.py")],
            env={**ENV, "PROMPTWERK_PORT": str(cls.port)}, stdout=subprocess.PIPE, text=True)
        cls.pw = None
        for line in cls.proc.stdout:
            if line.startswith("  "):
                cls.pw = line.strip()
            if "UI on" in line:
                break
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.proc.stdout.close()

    def req(self, path, body=None, auth=True, header=True):
        r = urllib.request.Request(self.base + path, None if body is None else json.dumps(body).encode())
        if auth:
            r.add_header("Authorization", "Basic " + base64.b64encode(f"promptwerk:{self.pw}".encode()).decode())
        if header and body is not None:
            r.add_header("X-Promptwerk", "1")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    def test_password_generated_and_private(self):
        self.assertTrue(self.pw and len(self.pw) >= 20)
        mode = stat.S_IMODE(os.stat(os.path.join(DATA, "web-password")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_auth_required(self):
        self.assertEqual(self.req("/api/state", auth=False)[0], 401)
        code, state = self.req("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(state["projects"], [PROJECT])

    def test_post_needs_header(self):
        self.assertEqual(self.req("/api/discard", {"name": "x.json"}, header=False)[0], 403)

    def test_generate_rejects_unknown_project_and_bad_input(self):
        self.assertEqual(self.req("/api/generate", {"topic": "Add due dates please", "project": TMP})[0], 400)
        self.assertEqual(self.req("/api/generate", {"topic": "short", "project": PROJECT})[0], 400)
        bad = {"topic": "Add due dates please", "project": PROJECT,
               "attachments": [{"name": "x.exe", "data": ""}]}
        self.assertEqual(self.req("/api/generate", bad)[0], 400)

    def test_approve_refuses_foreign_cwd_and_needs_blocking_answer(self):
        plan = example_plan()
        plan["runs"][0]["cwd"] = TMP
        os.makedirs(os.path.join(DATA, "drafts"), exist_ok=True)
        config.write_json(os.path.join(DATA, "drafts", "s1.json"), plan)
        self.assertEqual(self.req("/api/approve", {"name": "s1.json"})[0], 400)

        plan = example_plan()
        plan["clarifications"][0]["blocking"] = True
        plan["_promptwerk"] = {"topic_raw": "Add due dates"}
        config.write_json(os.path.join(DATA, "drafts", "s2.json"), plan)
        self.assertEqual(self.req("/api/approve", {"name": "s2.json"})[0], 400)
        code, _ = self.req("/api/approve", {"name": "s2.json", "answers": {"0": "Day only"},
                                            "constraints": "No new dependencies"})
        self.assertEqual(code, 200)
        queued = config.read_json(os.path.join(DATA, "queue", "s2.json"))
        prompt = queued["runs"][1]["prompt"]
        self.assertIn("# Operator goal", prompt)
        self.assertIn("- No new dependencies", prompt)
        self.assertIn("Day only", prompt)

    def test_attachments_numbered_and_typed(self):
        b64 = base64.b64encode(b"print(1)\n").decode()
        body = {"topic": "Explain the script please", "project": PROJECT, "attachments": [
            {"name": "image.png", "data": b64}, {"name": "image.png", "data": b64},
            {"name": "tool.py", "data": b64}, {"name": "old.doc", "data": b64}]}
        code, out = self.req("/api/generate", body)
        self.assertEqual(code, 200, out)
        folder = os.path.join(DATA, "attachments", out["id"])
        self.assertEqual(sorted(os.listdir(folder)),
                         ["image-2.png", "image.png", "old.doc", "old.doc.txt", "tool.py"])

    def test_state_has_week_ui_and_deploy_hint(self):
        s = self.req("/api/state")[1]
        self.assertEqual(len(s["ui"]), 12)
        self.assertEqual(set(s["week"]), {"plans", "goal_met", "judged", "cost_usd"})
        self.assertEqual(s["without_deploy"], [])  # deploys are off in the test config

    def test_draft_shows_full_prompt(self):
        config.write_json(os.path.join(DATA, "drafts", "p1.json"), example_plan())
        card = next(d for d in self.req("/api/state")[1]["drafts"] if d["name"] == "p1.json")
        self.assertTrue(card["runs"][0]["prompt"])
        self.assertEqual(card["runs"][0]["cwd"], PROJECT)
        os.unlink(os.path.join(DATA, "drafts", "p1.json"))

    def test_withdraw_only_before_start_and_close_only_when_idle(self):
        os.makedirs(os.path.join(DATA, "queue"), exist_ok=True)
        config.write_json(os.path.join(DATA, "queue", "w1.json"), example_plan())
        self.assertEqual(self.req("/api/withdraw", {"plan": "w1.json"})[0], 200)
        self.assertFalse(os.path.exists(os.path.join(DATA, "queue", "w1.json")))

        config.write_json(os.path.join(DATA, "queue", "w2.json"), example_plan())
        run = os.path.join(DATA, "runs", "w2-run")
        os.makedirs(run, exist_ok=True)
        meta = {"run_id": "w2-run", "plan": "w2.json", "key": "map-todos", "status": "running",
                "cwd": PROJECT}
        config.write_json(os.path.join(run, "meta.json"), meta)
        self.assertEqual(self.req("/api/withdraw", {"plan": "w2.json"})[0], 409)
        self.assertEqual(self.req("/api/close", {"plan": "w2.json"})[0], 409)
        meta["status"] = "done"
        config.write_json(os.path.join(run, "meta.json"), meta)
        self.assertEqual(self.req("/api/close", {"plan": "w2.json"})[0], 200)
        shutil.rmtree(run)
        for n in ("w2.json", "w2.closed.json"):
            os.unlink(os.path.join(DATA, "queue", n))

    def test_images_inline_svg_downloads(self):
        art = os.path.join(DATA, "runs", "img1", "artifacts")
        os.makedirs(art, exist_ok=True)
        for name in ("shot.png", "evil.svg"):
            with open(os.path.join(art, name), "wb") as f:
                f.write(b"x")
        auth = {"Authorization": "Basic " + base64.b64encode(f"promptwerk:{self.pw}".encode()).decode()}

        def head(name):
            r = urllib.request.Request(f"{self.base}/api/artifact/img1/{name}", headers=auth)
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.headers["Content-Type"], resp.headers.get("Content-Disposition")
        self.assertEqual(head("shot.png"), ("image/png", None))
        self.assertEqual(head("evil.svg"), ("application/octet-stream", "attachment"))

    def wait_for(self, path, tries=100):
        for _ in range(tries):
            if os.path.exists(path):
                return config.read_json(path)
            time.sleep(0.1)
        self.fail(f"{path} did not appear")

    def test_generate_without_project_and_as_follow_up(self):
        os.makedirs(os.path.join(DATA, "queue"), exist_ok=True)
        config.write_json(os.path.join(DATA, "queue", "o1.json"), example_plan())
        self.assertEqual(self.req("/api/generate", {"topic": "Follow up on that", "origin": "nope.json"})[0], 404)
        code, out = self.req("/api/generate", {"topic": "Add a filter for done todos", "origin": "o1.json"})
        self.assertEqual(code, 200, out)
        draft = self.wait_for(os.path.join(DATA, "drafts", out["id"] + ".json"))
        self.assertEqual(draft["_promptwerk"]["project"], "")
        self.assertEqual(draft["_promptwerk"]["from_plan"], "o1.json")
        self.assertEqual(draft["runs"][0]["cwd"], PROJECT)  # inferred from the knowledge block
        plan = next(p for p in self.req("/api/state")[1]["plans"] if p["name"] == "o1.json")
        self.assertEqual(plan["successors"], [{"name": out["id"] + ".json", "topic": draft["topic"], "where": "draft"}])
        os.unlink(os.path.join(DATA, "drafts", out["id"] + ".json"))
        os.unlink(os.path.join(DATA, "queue", "o1.json"))

    def test_state_keeps_newest_finished_plans_and_active_ones(self):
        q = os.path.join(DATA, "queue")
        os.makedirs(q, exist_ok=True)
        names = []
        for i in range(23):
            plan = example_plan()
            plan["_promptwerk"] = {"approved": 1000 + i}
            names.append(f"old{i:02}.json")
            config.write_json(os.path.join(q, names[-1]), plan)
            if i:
                config.write_json(os.path.join(q, f"old{i:02}.closed.json"), {"closed": 1})
            config.write_json(os.path.join(q, f"old{i:02}.summary.json"),
                              {"goal_met": {"state": "yes"}, "topic": "x", "promises": [1]})
        s = self.req("/api/state")[1]
        shown = {p["name"] for p in s["plans"]}
        self.assertIn("old00.json", shown)  # oldest, but not closed and nothing ran: active
        self.assertNotIn("old01.json", shown)
        self.assertLessEqual(len(shown & set(names)), 21)
        self.assertGreaterEqual(s["plans_total"], 23)
        card = next(p for p in s["plans"] if p["name"] == "old22.json")
        self.assertEqual(card["summary"], {"goal_met": {"state": "yes"}})
        for n in os.listdir(q):
            if n.startswith("old"):
                os.unlink(os.path.join(q, n))

    def test_answer_and_resume_refused_while_another_run_works_in_the_dir(self):
        config.write_json(os.path.join(DATA, "queue", "d1.json"), example_plan())
        made = []
        for rid, status, extra in (("d1-a", "waiting_for_answer", {}), ("d1-b", "running", {}),
                                   ("d1-c", "check_failed", {"check": {"code": 1},
                                    "check_correction": {"reason": "npm test does not exist"},
                                    "denials": ["git push", "rm -rf /"]})):
            os.makedirs(os.path.join(DATA, "runs", rid), exist_ok=True)
            config.write_json(os.path.join(DATA, "runs", rid, "meta.json"),
                              {"run_id": rid, "plan": "d1.json", "key": rid[-1], "status": status,
                               "cwd": PROJECT, "started": "2026-01-01T00:00:0" + rid[-1], **extra})
            made.append(rid)
        code, out = self.req("/api/answer", {"run_id": "d1-a", "text": "Day only"})
        self.assertEqual(code, 409)
        self.assertIn("d1-b", out["error"])
        self.assertEqual(self.req("/api/resume", {"run_id": "d1-c"})[0], 409)
        plan = example_plan()
        plan["runs"][0]["id"] = "c"
        config.write_json(os.path.join(DATA, "queue", "d1.json"), plan)
        run = next(p for p in self.req("/api/state")[1]["plans"] if p["name"] == "d1.json")["runs"][0]
        self.assertEqual((run["check_reason"], run["denials"], run["check_code"]), ("npm test does not exist", 2, 1))
        for rid in made:
            shutil.rmtree(os.path.join(DATA, "runs", rid))
        os.unlink(os.path.join(DATA, "queue", "d1.json"))
        shutil.rmtree(os.path.join(DATA, "runs", ".locks"), ignore_errors=True)

    def test_prompt_mode_writes_a_prompt_and_delete_removes_it(self):
        code, out = self.req("/api/generate", {"topic": "A spreadsheet for my utility bill", "mode": "prompt",
                                               "project": "/ignored/in/prompt/mode"})
        self.assertEqual(code, 200, out)
        p = self.wait_for(os.path.join(DATA, "prompts", out["id"] + ".json"))
        self.assertEqual(p["prompt"], "Create a spreadsheet with the columns Date, Item, Amount.")
        self.assertEqual(self.req("/api/state")[1]["prompts"][0]["id"], out["id"])
        self.assertEqual(self.req("/api/prompt-delete", {"id": out["id"]})[0], 200)
        self.assertEqual(self.req("/api/prompt-delete", {"id": out["id"]})[0], 404)

    def test_findings_become_a_chained_draft_by_severity(self):
        rid = "f1-run"
        art = os.path.join(DATA, "runs", rid, "artifacts")
        os.makedirs(art, exist_ok=True)
        meta = {"run_id": rid, "plan": "f1.json", "key": "audit", "title": "UI audit", "mode": "read",
                "status": "running", "cwd": PROJECT}
        config.write_json(os.path.join(DATA, "runs", rid, "meta.json"), meta)
        config.write_json(os.path.join(art, "ui-findings.json"), [
            {"id": "UI-1", "severity": "HIGH", "title": "Button unreadable", "file": "app.css", "line": 3,
             "description": "contrast 2:1", "remediation": "darker text"},
            {"id": "UI-2", "severity": "LOW", "title": "Typo", "remediation": "fix it"}])
        self.assertEqual(self.req("/api/findings-draft", {"run_id": rid})[0], 409)  # not done yet
        meta["status"] = "done"
        config.write_json(os.path.join(DATA, "runs", rid, "meta.json"), meta)
        code, out = self.req("/api/findings-draft", {"run_id": rid})
        self.assertEqual(code, 200, out)
        draft = config.read_json(os.path.join(DATA, "drafts", out["draft"]))
        self.assertEqual([r["id"] for r in draft["runs"]], ["fix-critical-high", "fix-low"])
        self.assertEqual(draft["runs"][1]["needs"], ["fix-critical-high"])
        self.assertEqual(draft["runs"][0]["mode"], "free")  # todo-app has no check command
        self.assertIn("UI-1", draft["runs"][0]["prompt"])
        self.assertEqual(draft["_promptwerk"]["from_plan"], "f1.json")
        self.assertEqual(check_plan.check(draft), [])
        os.unlink(os.path.join(DATA, "drafts", out["draft"]))
        shutil.rmtree(os.path.join(DATA, "runs", rid))

    def test_path_traversal(self):
        for path in ("/api/run/..%2F..%2Fetc", "/api/artifact/x/..", "/api/artifact/..%2F..%2F/x",
                     "/fonts/..%2F..%2Fbin%2Fconfig.py"):
            self.assertIn(self.req(path)[0], (400, 404), path)


if __name__ == "__main__":
    unittest.main()
