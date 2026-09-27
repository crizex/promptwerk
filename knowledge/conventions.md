# Conventions for generated runs

## Artifacts

Analysis runs (`mode: read`) produce exactly two files in the project root:

1. **`<AREA>_AUDIT.md`**: prose for humans. The first section is always "Summary and
   blockers", the last is always "Prioritized actions" with three classes: now (under an hour),
   medium, large.
2. **`<area>-findings.json`**: machine readable, an array, one object per finding.

## Finding schema

Each object in a `*-findings.json`:

```json
{
  "id": "UI-001",
  "severity": "CRITICAL | HIGH | MEDIUM | LOW",
  "category": "ui | a11y | performance | legal | wording | security",
  "title": "Short and precise",
  "file": "src/app/page.tsx",
  "line": 142,
  "evidence": "Verbatim excerpt from the location",
  "description": "What is wrong",
  "impact": "What happens because of it",
  "remediation": "Concrete fix or finished replacement text"
}
```

The id prefix is unique per run (`UI-`, `SEC-`, `DOC-`), so several runs can be merged later
without collisions.

## Evidence

Every finding needs file and line plus a verbatim excerpt. Claims without evidence do not
belong in an artifact. For hosted surfaces without source code, the URL with evidence takes the
place of file and line.

## Questions during a run

A run that cannot continue without a decision ends its turn with exactly this block and waits:

````
```promptwerk:question
{ "question": "...", "options": ["...", "..."], "why_blocking": "..." }
```
````

This is the emergency exit, not the normal path. Questions are asked before the run, in the
plan's `clarifications`. Only for a real blocker, and only if the answer is not already under
"Operator constraints" in the prompt. Small decisions the run makes itself and records in the
artifact under "Decisions made without asking". Several open points are bundled, not asked one
by one.

## What a run does not do

No commit, no push, no deploy: the worker does that afterwards, serially, if enabled. A run that
commits on its own collides with parallel runs. Reversible operational steps such as migrations
the run does perform itself, and proves that they took effect.
