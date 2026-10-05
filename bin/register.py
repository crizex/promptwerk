#!/usr/bin/env python3
"""Register of open points across all plans: R-0001, R-0002, ...

Every summary adds what it left open ('missing', except 'not_requested') and what the runs
found on the way ('new_findings'). A later summary closes items its reports prove finished
('register_done'). The planner sees the open items of the selected project, so a new task
does not rediscover them.

    register.py list [PROJECT]       open items, all projects or one
    register.py done R-0007 [WHY]    close an item by hand
    register.py add <plan.summary.json>   register a summary written before this existed
"""
import fcntl
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

PATH = os.path.join(config.DATA, "register.json")


class _Locked:
    """flock around read-modify-write: worker and UI may write summaries at the same time."""
    def __enter__(self):
        os.makedirs(config.DATA, exist_ok=True)
        self.f = open(PATH + ".lock", "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.items = config.read_json(PATH, []) or []
        return self.items

    def __exit__(self, *exc):
        if exc[0] is None:
            config.write_json(PATH, self.items)
        self.f.close()


def _close(items, rid, by):
    for it in items:
        if it["id"] == rid and it["status"] == "open":
            it.update(status="done", done_by=by, done_at=time.strftime("%Y-%m-%d"))
            return True
    return False


def add(summary):
    """Take over the open points of one summary. Idempotent per (plan, text)."""
    plan = summary.get("plan") or ""
    project = (summary.get("cwd") or [""])[0]
    new = [("open", m.get("who") or "", m.get("what") or "") for m in summary.get("missing") or []
           if m.get("who") != "not_requested"]
    new += [("finding", "", str(f)) for f in summary.get("new_findings") or []]
    with _Locked() as items:
        for rid in summary.get("register_done") or []:
            _close(items, str(rid), plan)
        seen = {(it["plan"], it["text"]) for it in items}
        n = max((int(it["id"][2:]) for it in items), default=0)
        for kind, who, text in new:
            text = text.strip()
            if not text or (plan, text) in seen:
                continue
            n += 1
            seen.add((plan, text))
            items.append({"id": f"R-{n:04}", "kind": kind, "who": who, "text": text,
                          "project": project, "plan": plan, "date": time.strftime("%Y-%m-%d"),
                          "status": "open", "done_by": None, "done_at": None})


def open_items(project=None, most=None):
    items = [it for it in config.read_json(PATH, []) or [] if it["status"] == "open"
             and (project is None or os.path.normpath(it["project"]) == os.path.normpath(project))]
    return items[-most:] if most else items


def as_text(items):
    return "\n".join(f"- {it['id']} ({it['kind']}{', ' + it['who'] if it['who'] else ''}, "
                     f"{it['date']}): {it['text']}" for it in items)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["list"] and len(a) <= 2:
        print(as_text(open_items(a[1] if len(a) == 2 else None)) or "No open items.")
    elif a[:1] == ["done"] and len(a) >= 2:
        with _Locked() as items:
            ok = _close(items, a[1], " ".join(a[2:]) or "by hand")
        sys.exit(0 if ok else f"{a[1]} is not an open item.")
    elif a[:1] == ["add"] and len(a) == 2:
        add(config.read_json(a[1], {}) or {})
    else:
        sys.exit(__doc__)
