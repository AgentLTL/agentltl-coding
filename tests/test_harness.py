"""The same rules under another harness: its agent's name, user file, tools and built-ins."""

from agentltl_coding import Guard, Harness, Paths, configure, current, lint, loads

from .conftest import CLAUDE


def with_harness(harness, fn):
    configure(harness)
    try:
        return fn()
    finally:
        configure(CLAUDE)


def test_messages_name_the_harness_agent_and_shell_tool():
    codex = Harness(name="codex", agent="Codex", user_dir="~/.codex",
                    shell_tools={"shell": "cmd"})

    def run():
        g = Guard(loads("rules: [{id: no-push, never: git_push, mode: ask}]"), Paths("/p", "/p"))
        g.restore({})
        return g.decide("shell", {"cmd": "git push"})

    v = with_harness(codex, run)
    assert v.action == "ask" and "Codex cannot override this rule" in v.reason


def test_builtins_come_from_the_harness_and_settings_switch_them_off():
    rule = {"id": "no-sudo", "never": "sudo", "why": "x"}
    h = Harness(builtins={"no_sudo": rule})

    assert with_harness(h, lambda: [r.id for r in _load("")]) == ["no-sudo"]
    assert with_harness(h, lambda: [r.id for r in _load("settings: {no_sudo: false}")]) == []
    assert current() is CLAUDE


def _load(text):
    import os
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "AGENTLTL.yaml")
    with open(path, "w") as fh:
        fh.write(text or "rules: []")
    from agentltl_coding import load
    return load([path]).rules


def test_lint_catches_a_misspelt_tool_in_a_formula():
    rs = loads("rules: [{id: r, ltl: 'G(now(\"git_psuh\") -> X(G(!now(\"git_psuh\"))))'}]")
    assert any("git_psuh" in w and "git_push" in w for w in lint(rs))
    rs = loads("rules: [{id: r, ltl: 'G(now(\"git_push\") -> X(G(!now(\"git_push\"))))'}]")
    assert lint(rs) == []
