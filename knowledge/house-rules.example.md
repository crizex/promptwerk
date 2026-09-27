# House rules (example)

Copy this file to `house-rules.md` in the same folder and make it yours. Promptwerk appends it
to the system prompt of every run and gives it to the planner. `house-rules.md` is ignored by
git, so personal rules stay local.

- Before reading code at random, use the repository's search or code index tools.
- Never run `git add -A`. The worker commits file by file after the plan, if enabled.
- Do not commit, push or deploy. Describe operator steps in the report instead.
- Done means done: what you notice within the task and can fix in the same run, fix, with a test.
- Every claim in a report needs file and line.
- Keep new dependencies out unless the task explicitly asks for one.
- Write plainly. No marketing tone, no emoji.
