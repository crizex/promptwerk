#!/usr/bin/env python3
"""Learning model choice for the optional cheap model ([models] cheap in config.toml).

Counts, per kind of run (its mode: read, build, free) and model family, how often finished
runs failed. A failure is a run that had to be escalated or did not end 'done'. Once the
cheap model fails at least FORCE_FROM of at least MIN_N runs of a kind, that kind goes
straight to the main model; below PROVEN_UP_TO it counts as proven.

    model_choice.py      prints the table the planner also gets
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

C = config.C
MIN_N, FORCE_FROM, PROVEN_UP_TO = 4, 0.4, 0.2


def family(model):
    m = str(model or "").lower()
    return next((f for f in ("haiku", "sonnet", "opus", "fable") if f in m), m or "?")


def stats(metas=None):
    """{(kind, family): [runs, failures]} over finished runs."""
    if metas is None:
        metas = [config.read_json(os.path.join(config.RUNS, d, "meta.json")) or {}
                 for d in (os.listdir(config.RUNS) if os.path.isdir(config.RUNS) else [])]
    out = {}
    for m in metas:
        if m.get("status") not in ("done", "incomplete"):
            continue
        model = (m.get("escalated") or {}).get("from") or m.get("model") or C["model"]
        s = out.setdefault((m.get("mode") or "?", family(model)), [0, 0])
        s[0] += 1
        s[1] += bool(m.get("escalated")) or m["status"] != "done"
    return out


def force_main(kind, metas=None):
    """Should a run of this kind skip the cheap model?"""
    n, bad = stats(metas).get((kind, family(C["models"]["cheap"])), [0, 0])
    return n >= MIN_N and bad / n >= FORCE_FROM


def table(metas=None):
    rows = ["| kind | family | runs | failed | rating |", "|---|---|---|---|---|"]
    for (kind, fam), (n, bad) in sorted(stats(metas).items()):
        rate = bad / n
        rating = ("too few runs" if n < MIN_N else "use the main model" if rate >= FORCE_FROM
                  else "proven" if rate <= PROVEN_UP_TO else "mixed")
        rows.append(f"| {kind} | {fam} | {n} | {bad} | {rating} |")
    return "\n".join(rows)


def planner_block():
    """Knowledge for the planner, or nothing when no cheap model is configured."""
    cheap = C["models"]["cheap"]
    if not cheap:
        return ""
    return ("# MODEL CHOICE\n\n"
            f"Runs use `{C['model']}` by default. For a simple, well-bounded run you may set "
            f"`\"model\": \"{cheap}\"`. Never for a kind rated 'use the main model' below. "
            "Such a run is escalated to the main model once if its check is red or artifacts "
            "are missing, which costs both. When unsure, leave `model` out.\n\n"
            + table())


if __name__ == "__main__":
    print(table())
