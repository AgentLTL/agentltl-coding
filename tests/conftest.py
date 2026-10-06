import pytest

from agentltl_coding.harness import Harness, configure

# The Claude Code plugin's harness, for these tests: they were written against it.
MEMORY_FILES = ["CLAUDE.md", "CLAUDE.local.md", "*/.claude/rules/*", "*/.claude/projects/*/memory/*"]
MEMORY_FIRST = {
    "id": "memory-first",
    "never": [{"tool": ["Write", "Edit", "MultiEdit"], "where": {"file_path": MEMORY_FILES}},
              {"tool": "*", "where": {"redirect_to": MEMORY_FILES}},
              {"tool": ["tee", "sponge"], "where": {"*": MEMORY_FILES}}],
    "mode": "warn",
    "why": "AGENTLTL rules are enforced on every call; memory can be forgotten. If what you are "
           "saving says which tool calls or commands to make, avoid, or make first (never X, "
           "always Y before Z, at most N times, only with these arguments), add it to "
           "AGENTLTL.yaml instead, using the /agentltl:rules skill, and leave it out of memory.",
    "fix": "Write the rule with the /agentltl:rules skill. Keep in memory only what no rule can "
           "check (facts, preferences, style). If nothing here can be a rule, repeat this exact "
           "call to save it.",
}
CLAUDE = Harness(name="claude-code", agent="Claude", user_dir="~/.claude",
                 shell_tools={"Bash": "command"}, builtins={"memory_first": MEMORY_FIRST},
                 auto_modes=("auto", "bypassPermissions", "dontAsk"),
                 project_env="CLAUDE_PROJECT_DIR", skill="/agentltl:{}")
# GitHub Copilot CLI's tool names, mapped onto the canonical (Claude Code) ones.
COPILOT = Harness(name="copilot-cli", agent="Copilot", user_dir="~/.copilot",
                  shell_tools={"Bash": "command", "powershell": "command"},
                  tool_aliases={"bash": ("Bash", {}),
                                "create": ("Write", {"path": "file_path", "file_text": "content"}),
                                "edit": ("Edit", {"path": "file_path", "old_str": "old_string",
                                                  "new_str": "new_string"}),
                                "view": ("Read", {"path": "file_path"})})
configure(CLAUDE)

from agentltl_coding.guard import Guard
from agentltl_coding.pattern import Paths
from agentltl_coding.rules import loads


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No user-level rule file, and session state under tmp."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTLTL_CC_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


def guard_for(text, cwd="/proj"):
    paths = Paths(cwd, "/proj")
    g = Guard(loads(text, paths), paths)
    g.restore({})
    return g


def run(guard, *steps, auto=False):
    """Decide each step; allowed steps are recorded as executed. Returns the actions."""
    out = []
    for step in steps:
        tool, tool_input = ("Bash", {"command": step}) if isinstance(step, str) else step
        v = guard.decide(tool, tool_input, auto=auto)
        out.append(v.action)
        if v.action == "none":
            guard.record(tool, tool_input, "id", "")
    return out
