# Changelog

All notable changes to Promptwerk. Versions follow [Semantic Versioning](https://semver.org/).
Downloads are on the [releases page](https://github.com/crizex/promptwerk/releases).

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
