"""Escalation modes, unparseable commands, and state carried between hook processes."""

import json

import pytest

from agentltl_coding.guard import Guard
from agentltl_coding.pattern import Paths
from agentltl_coding.rules import RuleFileError, load, loads

from .conftest import guard_for, run

NEVER_RM = "rules: [{{id: r, never: rm, why: keep files, fix: move them to trash/, mode: {}}}]"


class TestModes:
    def test_block_denies_every_time(self):
        g = guard_for(NEVER_RM.format("block"))
        assert run(g, "rm a", "rm a", "rm a") == ["deny"] * 3

    def test_warn_lets_claude_insist_with_the_exact_call(self):
        g = guard_for(NEVER_RM.format("warn"))
        assert run(g, "rm a", "rm b", "rm b", "rm c") == ["deny", "deny", "none", "deny"]

    def test_warn_override_needs_the_very_next_call(self):
        g = guard_for(NEVER_RM.format("warn"))
        assert run(g, "rm a", "ls", "rm a") == ["deny", "none", "deny"]

    def test_retry_escalates_to_the_user_then_starts_over(self):
        g = guard_for("settings: {retries: 2}\n" + NEVER_RM.format("retry"))
        assert run(g, "rm a", "rm b", "rm c", "rm d") == ["deny", "ask", "deny", "ask"]

    def test_ask(self):
        assert run(guard_for(NEVER_RM.format("ask")), "rm a", "rm a") == ["ask", "ask"]

    def test_stop(self):
        assert run(guard_for(NEVER_RM.format("stop")), "rm a") == ["stop"]

    def test_log_allows_and_tells_claude(self):
        g = guard_for(NEVER_RM.format("log"))
        v = g.decide("Bash", {"command": "ls && rm a"})
        assert v.action == "none" and "breaks AGENTLTL rule 'r' (keep files)" in v.context

    def test_strongest_rule_decides(self):
        # a warn override must not slip past a block rule the same call breaks
        g = guard_for("""
rules:
  - {id: soft, never: rm, mode: warn}
  - {id: hard, never: rm, with: {recursive: true}, mode: block}
""")
        assert run(g, "rm -r a", "rm -r a") == ["deny", "deny"]
        assert g.decide("Bash", {"command": "rm -r a"}).rule == "hard"

    def test_message(self):
        v = guard_for(NEVER_RM.format("block")).decide(
            "Bash", {"command": "ls && rm -f a.txt | cat"})
        assert v.reason.splitlines() == [
            "[AGENTLTL] Rule 'r' blocked this call (block). Nothing was executed.",
            "Rule: keep files",
            "Problem: rm is not allowed: it matches rm.",
            "Blocked at: rm -f a.txt",
            "To comply: move them to trash/",
            "This rule cannot be overridden by you. Do something that satisfies it instead, "
            "or explain the situation to the user.",
        ]
        assert [c["tool_name"] for c in v.calls] == ["ls", "rm", "cat"]


class TestUnparseable:
    RULES = "rules: [{id: r, never: rm}, {id: e, never: Edit, where: {file_path: '*.lock'}}]"

    def test_interactive_asks_and_auto_notes(self):
        g = guard_for(self.RULES)
        cmd = {"command": "eval $CMD"}
        asked = g.decide("Bash", cmd)
        noted = g.decide("Bash", cmd, auto=True)
        assert asked.action == "ask" and "could not be checked" in asked.reason
        assert "Rules it could fall under: r." in asked.reason   # not the Edit-only rule
        assert noted.action == "none" and "could not be checked" in noted.context

    def test_settings(self):
        g = guard_for("settings: {unparseable: {interactive: deny, auto: allow}}\n" + self.RULES)
        assert g.decide("Bash", {"command": "eval x"}).action == "deny"
        assert g.decide("Bash", {"command": "eval x"}, auto=True).context == ""

    def test_recorded_under_the_claude_code_name_when_let_through(self):
        g = guard_for(self.RULES)
        g.record("Bash", {"command": "eval x"}, "t", "")
        assert g.trace[-1]["tool_name"] == "Bash"

    def test_no_rules_no_opinion(self):
        assert guard_for("rules: []").decide("Bash", {"command": "eval x"}).action == "none"


def test_state_round_trips_through_json():
    text = "rules: [{id: t, before: [pytest, git_push]}, {id: w, never: rm, mode: warn}]"
    rs = loads(text)

    def fresh(state):
        g = Guard(rs, Paths("/proj", "/proj"))
        g.restore(json.loads(json.dumps(state)))
        return g

    g = fresh({})
    assert run(g, "pytest", "rm x") == ["none", "deny"]
    g = fresh(g.dump())   # a new hook process
    assert run(g, "rm x", "git push") == ["none", "none"]
    assert [c["tool_name"] for c in g.trace] == ["pytest", "rm", "git_push"]


def test_redirections_are_visible():
    g = guard_for("rules: [{id: r, never: {tool: [echo, cat, Write], where: {'*': '**/.env'}}}]")
    assert run(g, "echo x > a.txt", "echo x >> .env", "echo x 2> .env", "echo x 2>&1",
               "cat <<EOF > .env\nA=1\nEOF") == ["none", "deny", "deny", "none", "deny"]


def test_large_inputs_are_trimmed_in_the_trace():
    g = guard_for("rules: [{id: r, never: rm}]")
    g.record("Write", {"file_path": "a", "content": "x" * 10000}, "t", "y" * 10000)
    assert len(g.trace[-1]["arguments"]["content"]) < 2100
    assert len(g.trace[-1]["result"]) == 2000


class TestMemoryScopes:
    RULES = """
rules:
  - {id: tests-ever, before: [pytest, git_push], scope: project}
  - {id: tests-now, before: [ruff, git_push]}
"""

    def test_project_memory_survives_a_new_session_and_session_memory_does_not(self):
        rs = loads(self.RULES)

        def session(project_state):
            g = Guard(rs, Paths("/proj", "/proj"))
            g.restore({}, json.loads(json.dumps(project_state)))
            return g

        first = session({})
        assert run(first, "pytest", "ruff check", "git push") == ["none", "none", "none"]
        second = session(first.dump_project())       # new session, same project
        v = second.decide("Bash", {"command": "git push"})
        assert (v.action, v.rule) == ("deny", "tests-now")  # pytest is remembered, ruff is not
        assert [c["tool_name"] for c in second.project_trace] == ["pytest", "ruff", "git_push"]
        assert second.trace == []

    def test_scope_is_validated_and_defaults_from_settings(self):
        rs = loads("settings: {scope: project}\nrules: [{id: a, never: rm}, "
                   "{id: b, never: rm, scope: session}]")
        assert [(r.id, r.scope) for r in rs.rules] == [("a", "project"), ("b", "session")]
        with pytest.raises(RuleFileError, match="scope must be one of"):
            loads("rules: [{id: a, never: rm, scope: forever}]")


class TestWriteTargets:
    """Commands whose targets used to be invisible to path rules."""

    MIGRATIONS = "rules: [{id: r, never: {tool: '*', where: {'*': 'migrations/*'}}}]"

    def test_unknown_targets_are_a_possible_match(self):
        g = guard_for(self.MIGRATIONS)
        for command in ("ls migrations | xargs rm", "find migrations -name x -exec rm {} +",
                        "rm $SOMETHING", "find . -exec sed -i s/a/b/ {} ';'"):
            v = g.decide("Bash", {"command": command})
            assert v.action == "deny", command
        v = g.decide("Bash", {"command": "ls | xargs rm"})
        assert "only known at run time" in v.reason
        assert g.decide("Bash", {"command": "rm build/x"}).action == "none"

    def test_an_unknown_value_only_makes_its_own_argument_uncertain(self):
        g = guard_for("rules: [{id: r, never: {tool: '*', where: {redirect_to: '*.md'}}}]")
        assert run(g, "cd $SOMEWHERE", "rm $F", "echo x > $F", "ls | xargs echo > a.txt"
                   ) == ["none", "none", "deny", "none"]   # the redirection is known: a.txt

    def test_xargs_cannot_add_a_redirection(self):
        g = guard_for("rules: [{id: r, never: {tool: '*', where: {redirect_to: '*.md'}}}]")
        assert run(g, "ls | xargs kill", "ls | xargs rm", "ls | xargs echo > notes.md") == [
            "none", "none", "deny"]

    def test_a_require_rule_cannot_be_satisfied_by_unknown_targets(self):
        g = guard_for("rules: [{id: r, require: {tool: rm, where: {paths: 'build/**'}}}]")
        assert run(g, "rm build/a", "ls | xargs rm", "F=build/b; rm $F") == ["none", "deny", "none"]

    def test_cd_moves_where_relative_paths_point(self):
        g = guard_for("rules: [{id: r, never: {tool: rm, where: {paths: '/proj/secrets/*'}}}]")
        assert run(g, "cd secrets && rm key", "cd /proj/secrets; rm key", "rm secrets/key",
                   "cd secrets; cd ..; rm key", "cd docs && rm key", "rm key") == [
            "deny", "deny", "deny", "none", "none", "none"]

    def test_a_cd_ends_with_its_subshell_and_does_nothing_in_a_pipeline(self):
        g = guard_for("rules: [{id: r, never: {tool: rm, where: {paths: '/proj/secrets/*'}}}]")
        assert run(g, "(cd secrets; rm key)", "(cd secrets; ls); rm key",
                   'bash -c "cd secrets && rm key"', 'bash -c "cd secrets"; rm key',
                   "cd secrets | rm key", "pushd secrets && rm key && popd") == [
            "deny", "none", "deny", "none", "none", "deny"]

    def test_after_a_cd_to_an_unknown_directory_relative_paths_are_unknown(self):
        g = guard_for("rules: [{id: r, never: {tool: rm, where: {paths: '/proj/secrets/*'}}}]")
        v = g.decide("Bash", {"command": "cd $D && rm key"})
        assert v.action == "deny" and "only known at run time" in v.reason
        assert run(g, "cd - && rm key", "popd; rm key", "cd $D && rm /tmp/key") == [
            "deny", "deny", "none"]

    def test_resolved_variables_and_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        g = guard_for("rules: [{id: r, never: {tool: rm, where: {paths: '**/secrets/*'}}}]")
        assert run(g, "D=secrets; rm $D/key", "rm $HOME/secrets/key", "rm ~/secrets/key",
                   "rm ~/notes") == ["deny", "deny", "deny", "none"]

    def test_patch_targets_come_from_the_diff(self, tmp_path):
        (tmp_path / "fix.diff").write_text(
            "diff --git a/migrations/001.sql b/migrations/001.sql\n"
            "--- a/migrations/001.sql\n+++ b/migrations/001.sql\n@@ -1 +1 @@\n-x\n+y\n")
        (tmp_path / "ok.diff").write_text("--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1 @@\n-a\n+b\n")
        g = guard_for(self.MIGRATIONS.replace("migrations/*", "**/migrations/*"), cwd=str(tmp_path))
        assert run(g, "patch -p1 < fix.diff", "git apply fix.diff", "patch -p1 -i ok.diff",
                   "git apply missing.diff") == ["deny", "deny", "none", "deny"]

    def test_curl_remote_name_writes_a_file(self):
        g = guard_for("rules: [{id: r, never: {tool: curl, where: {output: '**/bin/*'}}}]")
        assert run(g, "curl -O https://x.dev/tool", "curl --output-dir bin -O https://x.dev/tool"
                   ) == ["none", "deny"]

    def test_tool_name_globs(self):
        g = guard_for("rules: [{id: r, never: {tool: 'kubectl_*', with: {namespace: prod}}}]")
        assert run(g, "kubectl -n prod get pods", "kubectl -n dev delete pod x",
                   "kubectl -n prod delete pod x") == ["deny", "none", "deny"]


class TestMemoryFirst:
    """The built-in rule steers rules Claude would memorise into AGENTLTL.yaml."""

    def guard(self, tmp_path, text="rules: []"):
        (tmp_path / "AGENTLTL.yaml").write_text(text)
        paths = Paths(str(tmp_path), str(tmp_path))
        g = Guard(load([str(tmp_path / "AGENTLTL.yaml")], paths), paths)
        g.restore({})
        return g

    def test_memory_writes_are_refused_once(self, tmp_path):
        g = self.guard(tmp_path)
        write = ("Write", {"file_path": "CLAUDE.md", "content": "never force-push"})
        assert run(g, write, write) == ["deny", "none"]
        assert run(g, "echo '- run tests first' >> CLAUDE.md", "echo x | tee .claude/rules/a.md",
                   ("Edit", {"file_path": "/h/.claude/projects/p/memory/MEMORY.md"})
                   ) == ["deny", "deny", "deny"]
        assert "/agentltl:rules skill" in g.decide(*write).reason

    def test_reading_memory_and_other_files_is_fine(self, tmp_path):
        g = self.guard(tmp_path)
        assert run(g, "cat CLAUDE.md", ("Read", {"file_path": "CLAUDE.md"}),
                   ("Write", {"file_path": "README.md"}), "echo x > notes.md", "cd $X"
                   ) == ["none"] * 5

    def test_can_be_turned_off_or_replaced(self, tmp_path):
        write = ("Write", {"file_path": "CLAUDE.md"})
        g = self.guard(tmp_path, "settings: {memory_first: false}\nrules: []")
        assert run(g, write) == ["none"]
        g = self.guard(tmp_path, "rules: [{id: memory-first, never: Write, mode: log}]")
        assert g.decide(*write).action == "none"


class TestProjectSpecs:
    def test_a_project_spec_extends_the_bundled_one_and_reaches_bash(self):
        g = guard_for("rules: [{id: r, never: {tool: 'kubectl_*', with: {namespace: prod}}}]\n"
                      "tools:\n  kubectl:\n    subcommands:\n"
                      "      rollout: {positionals: [{name: action}, {name: resource}]}")
        assert run(g, "kubectl rollout restart deploy/web -n prod", "kubectl delete pod x -n prod",
                   "kubectl delete pod x -n dev") == ["deny", "deny", "none"]

    def test_extend_false_replaces_it(self):
        g = guard_for("rules: [{id: r, never: kubectl_delete}]\n"
                      "tools:\n  kubectl: {extend: false, subcommands: {rollout: {}}}")
        assert g.translator.translate("kubectl delete pod x")[0].args.get("argv")


class TestAskWording:
    RULE = ("rules: [{id: no-rm-rf, never: {tool: rm, with: {recursive: true}}, "
            "why: A recursive delete cannot be undone., mode: MODE}]")

    def test_the_prompt_is_written_for_the_user_and_claude_gets_a_note(self):
        v = guard_for(self.RULE.replace("MODE", "ask")).decide("Bash", {"command": "rm -rf build"})
        assert v.action == "ask"
        assert v.reason.splitlines()[0] == "AgentLTL: this call breaks the rule 'no-rm-rf'."
        assert "Why the rule exists: A recursive delete cannot be undone." in v.reason
        assert "Command: rm -rf build" in v.reason and v.reason.endswith("Allow this call anyway?")
        assert "If they decline, do not retry it" in v.context

    def test_retry_escalation_asks_the_same_way(self):
        g = guard_for(self.RULE.replace("MODE", "retry"))
        verdicts = [g.decide("Bash", {"command": "rm -rf build"}) for _ in range(3)]
        assert [v.action for v in verdicts] == ["deny", "deny", "ask"]   # retries: 3
        assert "Claude was refused 2 time(s)" in verdicts[-1].reason
