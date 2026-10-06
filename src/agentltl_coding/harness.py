"""
agentltl_coding/harness.py – what differs between coding agents.

The rule language, the library, the guard and its state are the same for every coding
agent. A harness (the Claude Code plugin, one for Codex, ...) describes the rest once,
at start-up::

    from agentltl_coding import Harness, configure

    configure(Harness(name="claude-code", agent="Claude", user_dir="~/.claude",
                      shell_tools={"Bash": "command"}, builtins={"memory_first": MEMORY_FIRST}))
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Harness:
    """
    Attributes:
        name: Short name, used for the default state directory.
        agent: Who makes the calls, as messages name it ("Claude", "the agent").
        user_dir: Where the user-level AGENTLTL.yaml lives (rules for every project).
        shell_tools: Tool name -> the argument holding a shell command line. Those calls
            are checked as the structured calls of the line (cli-to-tools).
        builtins: Rules the harness switches on unless a setting of the same name is
            false (``memory_first: false``) or the rule id is disabled or replaced.
        state_dir: Where session and project state live; None: the default for *name*.
    """

    name: str = "agent"
    agent: str = "the agent"
    user_dir: str = "~/.agentltl"
    shell_tools: Dict[str, str] = field(default_factory=lambda: {"Bash": "command",
                                                                  "bash": "command"})
    builtins: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    state_dir: Optional[str] = None

    @property
    def Agent(self) -> str:
        """*agent* at the start of a sentence."""
        return self.agent[:1].upper() + self.agent[1:]

    def user_file(self) -> str:
        return os.path.join(os.path.expanduser(self.user_dir), "AGENTLTL.yaml")


_current = Harness()


def configure(harness: Harness) -> None:
    """Set the harness for this process (call once, before loading rules)."""
    global _current
    _current = harness


def current() -> Harness:
    return _current


__all__ = ["Harness", "configure", "current"]
