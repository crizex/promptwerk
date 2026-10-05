"""Configuration: one TOML file, a few environment overrides, nothing else.

Lookup order for the file: $PROMPTWERK_CONFIG, then <repo>/config.toml.
Missing file means built-in defaults (which are the same as config.example.toml).

Environment overrides (handy for tests and systemd):
  PROMPTWERK_CONFIG, PROMPTWERK_DATA_DIR, PROMPTWERK_CLAUDE_BIN, PROMPTWERK_PASSWORD
"""
import os
import re
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "data_dir": "~/.local/share/promptwerk",
    "knowledge_dir": "",          # empty: <repo>/knowledge
    "projects": [],               # absolute project dirs a run may use as cwd
    "model": "claude-opus-5-5",
    # Optional cheaper model the planner may give to simple runs. Empty = off.
    "models": {"cheap": ""},
    "claude_bin": "claude",
    "budgets": {"planner_usd": 8.0, "summary_usd": 1.5, "run_cap_usd": 25.0,
                "raise_factor": 1.5, "raise_multiple": 3.0, "daily_cap_usd": 150.0},
    "worker": {"parallel": 2, "usage_limit": 0.90, "poll_seconds": 20},
    "web": {"host": "127.0.0.1", "port": 8770, "user": "promptwerk", "password": ""},
    "notify": {"webhook_url": ""},
    "finish": {"auto_commit": False, "auto_push": False, "deploy_dir": ""},
}


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load():
    path = os.environ.get("PROMPTWERK_CONFIG") or os.path.join(ROOT, "config.toml")
    cfg = DEFAULTS
    if os.path.isfile(path):
        with open(path, "rb") as f:
            cfg = _merge(DEFAULTS, tomllib.load(f))
    cfg = _merge(cfg, {})  # private copy, callers may mutate
    if os.environ.get("PROMPTWERK_DATA_DIR"):
        cfg["data_dir"] = os.environ["PROMPTWERK_DATA_DIR"]
    if os.environ.get("PROMPTWERK_CLAUDE_BIN"):
        cfg["claude_bin"] = os.environ["PROMPTWERK_CLAUDE_BIN"]
    if os.environ.get("PROMPTWERK_PASSWORD"):
        cfg["web"]["password"] = os.environ["PROMPTWERK_PASSWORD"]
    cfg["data_dir"] = os.path.abspath(os.path.expanduser(cfg["data_dir"]))
    cfg["knowledge_dir"] = os.path.abspath(os.path.expanduser(
        cfg["knowledge_dir"] or os.path.join(ROOT, "knowledge")))
    cfg["projects"] = [os.path.abspath(os.path.expanduser(p)) for p in cfg["projects"]]
    return cfg


C = load()
DATA = C["data_dir"]
DRAFTS, QUEUE, RUNS, LOGS, ATTACH, PROMPTS = (
    os.path.join(DATA, d) for d in ("drafts", "queue", "runs", "logs", "attachments", "prompts"))
KNOW = C["knowledge_dir"]


def ensure_dirs():
    """Data tree private to the owner, no matter whether web or worker starts first."""
    for d in (DATA, DRAFTS, QUEUE, RUNS, LOGS, ATTACH, PROMPTS):
        os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(DATA, 0o700)


def slug(path):
    """Stable short name for a project dir: lock names, deploy scripts, profiles.

    The folder name alone, unless another configured project ends the same way
    (/work/a/backend and /work/b/backend): then parent folders are added until the
    name is unique, so the two never share a lock, a deploy script or a profile.
    """
    def tail(p, n):
        parts = os.path.normpath(p).strip(os.sep).split(os.sep)
        return re.sub(r"-+$", "", re.sub(r"[^a-z0-9]+", "-", "-".join(parts[-n:]).lower())).strip("-")
    me = os.path.normpath(os.path.abspath(path))
    others = [p for p in C["projects"] if os.path.normpath(p) != me]
    depth = len(me.strip(os.sep).split(os.sep))
    n = 1
    while n < depth and any(tail(o, n) == tail(me, n) for o in others):
        n += 1
    return tail(me, n)


def house_rules():
    """The user's house rules. house-rules.md wins; the shipped example is the fallback."""
    for name in ("house-rules.md", "house-rules.example.md"):
        p = os.path.join(KNOW, name)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
    return ""


def write_json(path, data):
    """Atomic write: the UI polls these files and must never see half a file."""
    import json
    with open(path + ".new", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(path + ".new", path)


def read_json(path, default=None):
    import json
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
