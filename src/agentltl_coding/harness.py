"""
agentltl_coding/harness.py – what differs between coding agents.

The rule language, the library, the guard and its state are the same for every coding
agent. A harness (the Claude Code plugin, the GitHub Copilot CLI plugin, ...) describes the
rest once, at start-up::

    from agentltl_coding import Harness, configure

    configure(Harness(name="claude-code", agent="Claude", user_dir="~/.claude",
                      shell_tools={"Bash": "command"}, builtins={"memory_first": MEMORY_FIRST}))

Rules name tools by their canonical names, Claude Code's (``Bash``, ``Write``, ``Edit``,
``Read``, ...), so the library works in every harness. A harness whose agent calls them
something else maps its names with ``tool_aliases``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# native tool name -> (canonical tool name, {native argument: canonical argument})
Aliases = Dict[str, Tuple[str, Dict[str, str]]]


@dataclass
class Harness:
    """
    Attributes:
        name: Short name, used for the default state directory and the library's
            ``harnesses:`` lists.
        agent: Who makes the calls, as messages name it ("Claude", "the agent").
        user_dir: Where the user-level AGENTLTL.yaml lives (rules for every project).
        shell_tools: Tool name -> the argument holding a shell command line. Those calls
            are checked as the structured calls of the line (cli-to-tools). Names are
            canonical (after ``tool_aliases``).
        tool_aliases: The agent's own tool names mapped onto the canonical ones, with their
            argument names (Copilot's ``create {path, file_text}`` is ``Write {file_path,
            content}``). Arguments not listed keep their names.
        builtins: Rules the harness switches on unless a setting of the same name is
            false (``memory_first: false``) or the rule id is disabled or replaced.
        state_dir: Where session and project state live; None: the default for *name*.
        auto_modes: Permission modes in which nobody answers a prompt, so ``unparseable``
            uses its ``auto`` setting.
        project_env: An environment variable naming the project directory, if the agent
            sets one; otherwise the project is the git root of the working directory.
        skill: How the agent's user invokes one of the plugin's skills, ``{}`` standing for
            its name ("/agentltl:{}" in Claude Code).
        memory: Finds the memory files that apply (see :mod:`agentltl_coding.memory`):
            ``memory(root, user_only, home) -> [Source]``. None: the harness has no memory
            import.
        memory_note: What the session-start reminder says about the ``memory-first`` rule.
    """

    name: str = "agent"
    agent: str = "the agent"
    user_dir: str = "~/.agentltl"
    shell_tools: Dict[str, str] = field(default_factory=lambda: {"Bash": "command",
                                                                  "bash": "command"})
    tool_aliases: Aliases = field(default_factory=dict)
    builtins: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    state_dir: Optional[str] = None
    auto_modes: Tuple[str, ...] = ()
    project_env: Optional[str] = None
    skill: str = "/agentltl-{}"
    memory: Optional[Callable[[str, bool, Optional[str]], List[Any]]] = None
    memory_note: str = ""

    @property
    def Agent(self) -> str:
        """*agent* at the start of a sentence."""
        return self.agent[:1].upper() + self.agent[1:]

    def user_file(self) -> str:
        return os.path.join(os.path.expanduser(self.user_dir), "AGENTLTL.yaml")

    def skill_name(self, name: str) -> str:
        return self.skill.format(name)

    def canonical(self, tool: str, tool_input: Optional[Dict[str, Any]]
                  ) -> Tuple[str, Dict[str, Any]]:
        """*tool* and its input under the canonical names rules use."""
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        if tool not in self.tool_aliases:
            return tool, tool_input
        name, keys = self.tool_aliases[tool]
        return name, {keys.get(k, k): v for k, v in tool_input.items()}

    def matches(self, harnesses: Any) -> bool:
        """Whether a library entry's ``harnesses:`` list (None: every harness) includes this
        one."""
        return not harnesses or self.name in harnesses


_current = Harness()


def configure(harness: Harness) -> None:
    """Set the harness for this process (call once, before loading rules)."""
    global _current
    _current = harness


def current() -> Harness:
    return _current


__all__ = ["Aliases", "Harness", "configure", "current"]
