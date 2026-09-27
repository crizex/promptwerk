"""Insert operator constraints and the verbatim goal into run prompts.

Used on approval: answers to clarifications and extra constraints go into every run, the
user's original sentence goes in as the goal. Repeated calls append instead of duplicating.
"""
import re

HEAD = """
# Operator constraints
These are decided and rank above any remediation advice from an audit. If an item
contradicts a constraint, the constraint wins; the contradiction goes into the artifact,
not into a solution of your own.
"""

GOAL_HEAD = """
# Operator goal
The operator typed this verbatim. It is the yardstick the result is measured against,
not the acceptance list alone. If this run is finished and the goal is still not met,
the run was not finished. If an instruction below contradicts the goal, the goal wins.
"""


def insert(text, lines, head=HEAD, bullets=True):
    """Insert a block before the first heading, or append to an existing block."""
    if not lines:
        return text
    title = head.strip().splitlines()[0]
    body = "".join(f"- {z}\n" for z in lines) if bullets else "\n".join(lines) + "\n"
    if title in text:
        before, rest = text.split(title, 1)
        m = re.search(r"\n(?=# )", rest)
        cut = m.start() + 1 if m else len(rest)
        return before + title + rest[:cut].rstrip("\n") + "\n" + body + rest[cut:]
    # \A matters: most prompts start with a heading on line 1, and a pattern that only
    # looks for "\n# " would put the constraints after the whole work order.
    block = head + body
    m = re.search(r"(?:\A|\n)(?=# )", text)
    if m:
        return text[: m.end()] + block + text[m.end():]
    return text.rstrip("\n") + "\n" + block


def split_lines(text):
    """One constraint per line, bullets and blank lines removed."""
    return [z.strip(" -\t") for z in (text or "").splitlines() if z.strip(" -\t")]
