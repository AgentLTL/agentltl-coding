"""
agentltl_coding – AgentLTL rules for coding agents, whatever the harness.

The rule language of AGENTLTL.yaml (never / before / require / at_most / ltl / formula),
the library of packaged rules, the guard that decides each tool call (shell command lines
checked as their structured calls), its per-session and per-project state, and the
``agentltl`` command, and what hooks do (:class:`Session`). A harness (the Claude Code
plugin, the GitHub Copilot CLI plugin, ...) supplies its hook I/O and a :class:`Harness`
description.
"""

from .guard import Guard, Verdict, lint, translator_for
from .harness import Harness, configure, current
from .pattern import Paths
from .rules import RuleFileError, RuleSet, library, load, loads, rule_files
from .session import Session

__version__ = "0.2.3"

__all__ = ["Guard", "Harness", "Paths", "RuleFileError", "RuleSet", "Session", "Verdict", "configure",
           "current", "library", "lint", "load", "loads", "rule_files", "translator_for"]
