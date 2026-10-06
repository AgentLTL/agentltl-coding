"""finally rules, checked when the agent finishes, and rules on whether a call succeeded."""

import pytest

from agentltl_coding import RuleFileError, loads

from .conftest import guard_for, run

EDIT = ("Edit", {"file_path": "/proj/a.py"})
FINALLY = """
settings: {finish_retries: 1}
rules:
  - id: tests-after-edits
    finally: {call: pytest, since: [Edit, Write]}
    why: Untested work.
    fix: Run pytest.
"""


def test_finally_sends_the_agent_back_once_per_turn():
    g = guard_for(FINALLY)
    assert g.finish().action == "none"                 # nothing edited, nothing owed
    assert run(g, EDIT) == ["none"]                    # never refuses a call mid-run
    v = g.finish()
    assert v.action == "block" and "pytest has not run after the last Edit or Write" in v.reason
    assert "To comply: Run pytest." in v.reason
    assert g.finish().action == "none"                 # finish_retries: 1 per turn
    g.new_turn()
    assert g.finish().action == "block"
    run(g, "pytest -q")
    assert g.finish().action == "none"


def test_finally_without_since_needs_the_call_once():
    g = guard_for("rules: [{id: r, finally: {tool: git_push}}]")
    assert g.finish().action == "block"
    run(g, "git push")
    assert g.finish().action == "none"


def test_the_finish_counter_survives_a_new_process():
    g = guard_for(FINALLY)
    run(g, EDIT)
    assert g.finish().action == "block"
    again = guard_for(FINALLY)
    again.restore(g.dump())
    assert again.finish().action == "none"


SUCCEEDED = """
rules:
  - id: green-before-push
    before: {first: {tool: pytest, succeeded: true}, then: git_push}
"""


def test_a_failed_run_does_not_count():
    g = guard_for(SUCCEEDED)
    g.record("Bash", {"command": "pytest"}, "t1", "1 failed", status=1)
    v = g.decide("Bash", {"command": "git push"})
    assert v.action == "deny" and "pytest (succeeded)" in v.reason
    g.record("Bash", {"command": "pytest"}, "t2", "3 passed", status=0)
    assert g.decide("Bash", {"command": "git push"}).action == "none"


def test_a_call_recorded_without_a_status_succeeded():
    g = guard_for(SUCCEEDED)
    g.record("Bash", {"command": "pytest"}, "t1", "3 passed")
    assert g.decide("Bash", {"command": "git push"}).action == "none"


def test_succeeded_must_be_a_boolean():
    with pytest.raises(RuleFileError, match="succeeded: must be true or false"):
        loads("rules: [{id: r, never: {tool: rm, succeeded: maybe}}]")
