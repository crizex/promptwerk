"""Optional notifications: one JSON POST to a webhook. Off unless [notify] webhook_url is set.

Payload: {"text": "...", "event": "...", "data": {...}}. Most chat tools (Slack, Discord via
/slack, Mattermost, ntfy with a JSON template) accept a "text" field as-is; anything else can
sit behind a tiny relay.

Only status changes worth a look are sent: a question from a run, a failure, a rate limit,
the daily cap, and a finished plan.
"""
import json
import urllib.request

import config

WORTH = {"waiting_for_answer": "has a question", "failed": "failed",
         "check_failed": "check is red", "ratelimit": "paused by the usage limit",
         "incomplete": "finished incomplete"}


def send(text, event="info", data=None):
    url = config.C["notify"]["webhook_url"]
    if not url:
        return False
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        return False  # no plain-text webhooks over the network
    body = json.dumps({"text": text, "event": event, "data": data or {}}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except OSError:
        return False  # a dead webhook must never stop a run


def run_status(meta):
    """Send a note for a status change worth looking at."""
    what = WORTH.get(meta.get("status"))
    if what:
        send(f"Promptwerk: '{meta.get('title') or meta.get('key')}' {what}.",
             "run_" + meta["status"], {"run_id": meta.get("run_id"), "plan": meta.get("plan")})


def plan_done(summary):
    goal = (summary.get("goal_met") or {}).get("state", "?")
    send(f"Promptwerk: plan '{summary.get('topic')}' finished. Goal met: {goal}. "
         f"{summary.get('verdict') or ''}".strip(), "plan_done",
         {"plan": summary.get("plan"), "cost_usd": summary.get("cost_usd")})
