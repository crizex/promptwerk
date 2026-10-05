#!/usr/bin/env python3
"""Mechanical facts about a project, read from the file system at plan time. No model.

    project_facts.py <project dir>

The planner gets these next to the profile or the automatic snapshot, so it does not have to
guess the stack or invent a check command. The derived check command can be overridden per
project in knowledge/check-commands.json: {"<project slug>": "make test"}.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

NPM_CHECKS = ("typecheck", "lint", "test", "build")
STACK = {"next": "Next.js", "react": "React", "vue": "Vue", "svelte": "Svelte", "astro": "Astro",
         "express": "Express", "fastify": "Fastify", "electron": "Electron",
         "typescript": "TypeScript", "tailwindcss": "Tailwind", "prisma": "Prisma",
         "drizzle-orm": "Drizzle", "vitest": "Vitest", "jest": "Jest", "playwright": "Playwright"}


def _package(project):
    return config.read_json(os.path.join(project, "package.json"), {}) or {}


def check_command(project):
    """The project's own check command: override, npm scripts, else a Python test run."""
    override = config.read_json(os.path.join(config.KNOW, "check-commands.json"), {}) or {}
    if override.get(config.slug(project)):
        return override[config.slug(project)]
    scripts = _package(project).get("scripts") or {}
    found = [s for s in NPM_CHECKS if s in scripts]
    if found:
        return " && ".join(f"npm run {s}" for s in found)
    if os.path.isdir(os.path.join(project, "tests")) and any(
            n.endswith(".py") for n in os.listdir(os.path.join(project, "tests"))):
        return "python3 -m unittest discover tests"
    return None


def git_setup(project):
    try:
        top = subprocess.run(["git", "-C", project, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown (git not available)"
    if not top:
        return "not under git"
    if os.path.realpath(top) == os.path.realpath(project):
        return "own repository"
    return f"part of the repository at {top}: stage files by path, never everything"


def facts(project):
    pkg = _package(project)
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    stack = [v for k, v in STACK.items() if k in deps]
    for f, name in (("pyproject.toml", "Python"), ("requirements.txt", "Python"),
                    ("go.mod", "Go"), ("Cargo.toml", "Rust"), ("Gemfile", "Ruby"),
                    ("composer.json", "PHP")):
        if os.path.isfile(os.path.join(project, f)) and name not in stack:
            stack.append(name)
    check = check_command(project)
    lines = [f"- Git: {git_setup(project)}",
             f"- Stack: {', '.join(stack) or 'not detected'}",
             f"- Check command: `{check}`" if check else
             "- Check command: none found. A build run must name one that exists, or use mode free.",
             f"- Dockerfile: {'yes, a change needs a rebuild to go live' if os.path.isfile(os.path.join(project, 'Dockerfile')) else 'no'}"]
    if pkg.get("scripts"):
        lines.append("- npm scripts: " + ", ".join(sorted(pkg["scripts"])))
    for name in ("CLAUDE.md", "AGENTS.md"):
        if os.path.isfile(os.path.join(project, name)):
            lines.append(f"- {name} present: runs read it on their own")
    return "Facts (read from the file system just now):\n" + "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(facts(os.path.abspath(sys.argv[1])))
    print(json.dumps({"check": check_command(os.path.abspath(sys.argv[1]))}))
