# Changelog

All notable changes to Promptwerk. Versions follow [Semantic Versioning](https://semver.org/).
Downloads are on the [releases page](https://github.com/crizex/promptwerk/releases).

## 1.3.0

A larger update for everyday use.

**Writing a task**
- Attach files by pasting (screenshots straight from the clipboard) or by dropping them on the
  form. Attached files show as a list with size and a remove button.
- More attachment types: source code, `.tsv`, `.rtf`, `.toml`, `.ini`, diffs and patches. Old
  Office formats (`.doc`, `.xls`, `.ppt`) are accepted with a note that asks for the modern format.
- Two files with the same name (every pasted screenshot is `image.png`) no longer overwrite each
  other: the second becomes `image-2.png`. If storing fails, no half-written folder is left.
- Picking a project without a deploy script shows a short hint, when deploys are configured.

**Reviewing a draft**
- Every run shows its full prompt and working directory before you approve.
- Clarifying questions have a multi-line answer field, and the planner's suggestions are buttons
  that fill it in.
- Your attachments appear in the draft as a small gallery.

**Plans and runs**
- Artifacts open inside the UI: Markdown is rendered, a JSON list of findings becomes a table
  sorted by severity, other text is shown as is, with size and line count. Links in Markdown
  only open for `http`/`https`.
- An approved plan that has not started yet can be withdrawn from the queue.
- A plan can no longer be closed while one of its runs is still working.
- Typing an answer or a budget no longer gets wiped by a live update.

**Overview**
- The top bar shows the last 7 days: plans approved, goal met out of those judged, cost.
- An open tab notices when the UI was updated and offers a reload button.
- The webhook also reports when a draft is ready for review or planning failed.

## 1.2.0

- The project picker remembers your last choice in this browser.

## 1.1.0

- Image artifacts and attachments (PNG, JPEG, GIF, WebP) open in the browser instead of
  downloading, and the run detail shows a preview under each image artifact. SVG stays a
  download, since it can contain script.

## 1.0.3

- Two projects with the same folder name (for example `/work/a/backend` and `/work/b/backend`) no
  longer share a lock, a deploy script or a project profile. Their names now include the parent
  folder (`a-backend.sh`, `b-backend.sh`). Projects with unique folder names keep their names.

## 1.0.2

- README: the diagram is readable on GitHub again, labels no longer get cut off inside their boxes.

## 1.0.1

- README: short demo animation from one rough sentence to a finished plan.

## 1.0.0

First public release.

- **Planner**: one sentence (plus optional attachments) becomes a plan of headless Claude Code
  runs. Two calls, a draft and a critic, with personas, conventions, your house rules and an
  optional profile per project.
- **Draft review**: clarifying questions with the planner's assumption, constraints you add
  before approval, and plan checks for cycles, missing dependencies, unknown projects and scope.
  Nothing runs before you approve.
- **Worker**: runs plans in dependency order, in parallel but never two in the same directory,
  with per-run budgets, automatic raises up to a hard cap, a daily cap and a usage-window limit.
- **Runs**: read runs may only write their declared artifacts, build runs cannot commit, push or
  do irreversible deletes. Runs can pause and ask you a question. Rate limits pause and resume
  by themselves.
- **Result checks**: a shell check per run, artifact presence, and a plan summary that says
  whether the goal was met, what to try, what is missing and what to do next.
- **Web UI**: a single page with three lanes (needs you, running, done), live updates, run logs
  and artifacts, answers, budget raises, resume and cancel. Works on a phone.
- **Security defaults**: local bind, always-on password (generated if unset), request header
  check, strict security headers, projects allowlist, opt-in commit, push, deploy and webhook.
- **Operations**: systemd unit examples, JSON files instead of a database, standard library
  Python only.
- **Tests**: 17 unittest cases and a fake `claude` binary for a model-free demo.
