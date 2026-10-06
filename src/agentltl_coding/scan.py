"""
agentltl_coding/scan.py – spot credentials in what a tool call returned.

A rule can only stop a call before it runs; when a program prints a credential anyway
(`python app.py` logging its config, a test failure dumping the environment), the value is
already in the conversation. The PostToolUse hook scans the output for well-known credential
formats, the documented prefixes that secret scanners also use, and reports what kind was
seen, never the value, so the user can rotate it and Claude does not repeat it.

Only formats with a distinctive prefix or structure are matched, to keep false alarms rare.
"""

from __future__ import annotations

import json
import re
from typing import Any, List

_MAX_SCAN = 1_000_000

# (kind, pattern): documented prefixes; lengths keep ordinary words from matching.
_FORMATS = [
    ("AWS access key ID", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    ("GitLab token", r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ("Stripe live key", r"\b[rs]k_live_[A-Za-z0-9]{20,}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("Anthropic API key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI API key", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}"),
    ("npm token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("private key", r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
]
_COMPILED = [(kind, re.compile(pattern)) for kind, pattern in _FORMATS]


def credential_kinds(value: Any) -> List[str]:
    """The kinds of credential that appear in *value* (a tool response), in a stable order."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text[:_MAX_SCAN]
    return [kind for kind, pattern in _COMPILED if pattern.search(text)]


def report(tool: str, kinds: List[str]) -> dict:
    """The PostToolUse answer: a notice for the user, an instruction for Claude."""
    what = ", ".join(kinds)
    return {
        "systemMessage": (f"AgentLTL: the output of {tool} contains what looks like a "
                          f"credential ({what}). If it is real, consider rotating it."),
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"The output of this {tool} call contains what looks like a credential ({what}). "
                "Do not repeat, quote, copy or write that value anywhere: not in replies, files, "
                "commands or commit messages. Tell the user it appeared, so they can rotate it, "
                "and carry on without it."),
        },
    }
