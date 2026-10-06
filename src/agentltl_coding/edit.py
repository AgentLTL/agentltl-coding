"""
agentltl_coding/edit.py – change the ``use:`` and ``disable:`` lists of a rule file in place.

Only the block of the key being changed is rewritten; the rules, comments and layout of the
rest of the file are kept. A change that would leave the file invalid is undone.
"""

from __future__ import annotations

import os
import re
from typing import Any, List

import yaml

NEW_FILE = ("# AGENTLTL.yaml – rules the AgentLTL plugin enforces on every Claude Code\n"
            "# tool call. `agentltl library` lists packaged rules; add your own under `rules:`.\n"
            "\nrules: []\n")


def read_list(path: str, key: str) -> List[Any]:
    """The current value of top-level *key* (a list), or [] when absent."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    value = data.get(key) if isinstance(data, dict) else None
    return list(value) if isinstance(value, list) else []


def write_list(path: str, key: str, items: List[Any]) -> None:
    """Set top-level *key* to *items* (removing the key when empty), keeping the rest."""
    text = NEW_FILE
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    block = _render(key, items)
    start = next((i for i, line in enumerate(lines) if re.match(rf"{key}\s*:", line)), None)
    if start is not None:
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t-#"):
            end += 1
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
            end -= 1                      # leave blank lines and comments of the next section
        lines[start:end] = block
    else:
        at = next((i for i, line in enumerate(lines) if re.match(r"rules\s*:", line)), len(lines))
        lines[at:at] = block + (["\n"] if block and at < len(lines) else [])
    new = "".join(lines)
    yaml.safe_load(new)                   # raises before anything is written
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)


def _render(key: str, items: List[Any]) -> List[str]:
    if not items:
        return []
    out = [f"{key}:\n"]
    for item in items:
        flow = yaml.safe_dump([item], default_flow_style=True, sort_keys=False).strip()
        out.append(f"  - {flow[1:-1]}\n")
    return out
