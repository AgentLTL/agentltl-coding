"""What hooks do, whatever the harness: state across calls, stops, the log, finishing."""

import os

from agentltl_coding import Harness, Session, configure, store
from agentltl_coding.memory import PROJECT, Source, scan

from .conftest import CLAUDE, COPILOT


def _project(tmp_path, rules):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTLTL.yaml").write_text(rules)
    return str(tmp_path)


def _session(root, sid="s1"):
    return Session(root, root, sid)


def test_no_rule_file_means_no_files(tmp_path):
    assert _session(str(tmp_path)).files == []


def test_state_carries_from_one_hook_call_to_the_next(tmp_path):
    root = _project(tmp_path, "rules: [{id: t, before: [pytest, git_push]}]")
    assert _session(root).pre("Bash", {"command": "git push"}).action == "deny"
    _session(root).post("Bash", {"command": "pytest"}, "1", "ok", status=0)
    assert _session(root).pre("Bash", {"command": "git push"}).action == "none"
    log = store.read("s1")["decisions"]
    assert log == [{"tool": "Bash", "input": {"command": "git push"}, "action": "deny",
                    "rule": "t"}]


def test_a_stop_holds_until_the_user_writes(tmp_path):
    root = _project(tmp_path, "rules: [{id: env, never: {tool: cat, where: {'*': '*.env'}}, "
                              "mode: stop}]")
    assert _session(root).pre("Bash", {"command": "cat .env"}).action == "stop"
    held = _session(root).pre("Bash", {"command": "ls"})
    assert held.action == "deny" and "stopped this session" in held.reason
    _session(root).prompt()
    assert _session(root).pre("Bash", {"command": "ls"}).action == "none"


REVIEW_ASKED = ("rules: [{id: review-loaded, finally: {call: {tool: Skill, with: {skill: review}}, "
              "since: {tool: UserPrompt, where: {prompt: '*/review*'}}}}]")


def _prompts(state):
    return [c["arguments"]["prompt"] for c in state.get("completed_tool_calls", [])
            if c["tool_name"] == "UserPrompt"]


def test_a_rule_can_react_to_what_the_user_asked(tmp_path):
    root = _project(tmp_path, REVIEW_ASKED)
    _session(root).prompt("hello")
    assert _session(root).finish().action == "none"       # nothing asked for yet
    _session(root).prompt("please run /review on the docs")
    blocked = _session(root).finish()
    assert blocked.action == "block" and "review-loaded" in blocked.reason
    _session(root).post("Skill", {"skill": "review"}, "1", "", status=0)
    assert _session(root).finish().action == "none"
    _session(root).prompt("and /review again, please")      # a new request needs a new load
    assert _session(root).finish().action == "block"


def test_prompts_are_recorded_whole_in_both_traces(tmp_path):
    root = _project(tmp_path, REVIEW_ASKED)
    text = "x" * 5000 + " /review"
    _session(root).prompt(text)
    assert _prompts(store.read("s1")) == [text]
    assert _prompts(store.read_project(root)) == [text]
    assert "decisions" not in store.read("s1")
    assert _session(root).finish().action == "block"      # the request past 2000 characters


def test_only_prompts_a_rule_is_about_are_recorded(tmp_path):
    root = _project(tmp_path, "rules: [{id: n, at_most: {call: '*', times: 1}}, "
                              "{id: f, finally: {call: pytest, since: '*'}}]")
    _session(root).prompt("my secret plans")              # no rule names UserPrompt
    assert _prompts(store.read("s1")) == [] and _prompts(store.read_project(root)) == []
    assert _session(root).finish().action == "none"
    assert _session(root).pre("Bash", {"command": "ls"}).action == "none"
    (tmp_path / "AGENTLTL.yaml").write_text(REVIEW_ASKED)
    _session(root).prompt("my secret plans")              # the rule is not about this one
    _session(root).prompt("then /review them")
    assert _prompts(store.read("s1")) == ["then /review them"]


def test_prompt_without_text_or_with_a_broken_rule_file_still_lifts_a_stop(tmp_path):
    root = _project(tmp_path, "rules: [{id: env, never: {tool: cat, where: {'*': '*.env'}}, "
                              "mode: stop}, {id: p, never: UserPrompt}]")
    for text in (None, "", 42, "go on"):
        assert _session(root).pre("Bash", {"command": "cat .env"}).action == "stop"
        _session(root).prompt(text)
        assert _session(root).pre("Bash", {"command": "ls"}).action == "none"
    assert _prompts(store.read("s1")) == ["go on"]        # recorded, never refused
    _session(root).pre("Bash", {"command": "cat .env"})
    (tmp_path / "AGENTLTL.yaml").write_text("rules: [{id: broken")
    _session(root).prompt("go on")
    assert "stopped" not in store.read("s1")


def test_finally_rules_send_the_agent_back(tmp_path):
    root = _project(tmp_path, "rules: [{id: tested, finally: pytest}]")
    assert _session(root).finish().action == "block"
    _session(root).post("Bash", {"command": "pytest"}, "1", "", status=0)
    assert _session(root).finish().action == "none"


def test_credentials_in_output_are_reported_without_the_value(tmp_path):
    root = _project(tmp_path, "rules: [{id: r, never: sudo}]")
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    user, agent = _session(root).post("Bash", {"command": "env"}, "1", f"X={key}", status=0)
    assert "AWS access key ID" in user and key not in user + agent


def test_reminder_lists_the_rules_and_the_harness_memory_note(tmp_path):
    root = _project(tmp_path, "rules: [{id: r, never: sudo, why: no root}]")
    text = _session(root).start()
    assert "- r [block]" in text and "no root" in text and "memory-first [warn]" in text
    configure(Harness(name="x", builtins=CLAUDE.builtins, memory_note="NOTE"))
    try:
        assert "- memory-first [warn]: NOTE" in _session(root).start()
    finally:
        configure(CLAUDE)


def test_copilot_calls_are_logged_under_canonical_names(tmp_path):
    root = _project(tmp_path, "rules: [{id: no-env, never: {tool: Write, where: "
                              "{file_path: '*.env'}}}]")
    configure(COPILOT)
    try:
        v = _session(root).pre("create", {"path": ".env", "file_text": "A=1"})
    finally:
        configure(CLAUDE)
    assert v.action == "deny"
    entry = store.read("s1")["decisions"][0]
    assert entry["tool"] == "Write" and entry["input"]["file_path"] == ".env"


def test_memory_files_come_from_the_harness(tmp_path):
    (tmp_path / "NOTES.md").write_text("- Never run `git push --force` on main.\n")

    def find(root, user_only, home):
        return [Source(os.path.join(root, "NOTES.md"), "instructions", PROJECT)]

    configure(Harness(name="x", memory=find))
    try:
        result = scan(str(tmp_path))
    finally:
        configure(CLAUDE)
    [st] = result["statements"]
    assert st["candidate"] and st["file"] == "NOTES.md"
    assert scan(str(tmp_path))["sources"] == []        # the Claude test harness has no finder
