"""The rule library: packaged rules switched on with `use:`, and `disable:`."""

import pytest

from agentltl_coding import cli
from agentltl_coding.guard import Guard, lint
from agentltl_coding.pattern import Paths
from agentltl_coding.rules import RuleFileError, library, load, loads

from .conftest import run

PACKS = library()


def guard_using(pack, cwd="/proj"):
    paths = Paths(cwd, cwd)
    g = Guard(loads(f"use: [{pack}]", paths), paths)
    g.restore({})
    return g


@pytest.mark.parametrize("name", sorted(PACKS))
def test_every_pack_compiles_without_warnings(name):
    pack = PACKS[name]
    assert pack.get("summary") and pack.get("tags") and (pack.get("rules") or pack.get("include"))
    ruleset = loads(f"use: [{name}]")
    assert ruleset.rules and all(r.why for r in ruleset.rules)
    assert lint(ruleset) == []


def test_rule_ids_are_unique_across_the_library():
    ids = [r["id"] for p in PACKS.values() for r in p.get("rules") or []]
    assert len(ids) == len(set(ids))


def test_a_bundle_switches_on_its_entries_once():
    rs = loads("use: [devops-secrets, no-env-dumps]")
    ids = [r.id for r in rs.rules]
    assert ids == list(dict.fromkeys(ids)) and len(ids) == len(PACKS["devops-secrets"]["include"])
    assert {r.source for r in rs.rules} == {f"library:{n}" for n in PACKS["devops-secrets"]["include"]}


AGENT = "Agent"
_BEHAVIOUR = [
    ("subagents-on-sonnet", [(AGENT, {"prompt": "x"}), (AGENT, {"prompt": "x", "model": "opus"}),
                             (AGENT, {"prompt": "x", "model": "sonnet"}),
                             (AGENT, {"prompt": "x", "model": "sonnet", "subagent_type": "fork"})],
     ["deny", "deny", "none", "deny"]),
    ("no-claude-coauthor", ['git commit -m x -m "Co-Authored-By: Claude <noreply@anthropic.com>"',
                            'git commit -m "fix bug"'], ["deny", "none"]),
    ("no-force-push", ["git push -f", "git push --force-with-lease", "git push"],
     ["deny", "none", "none"]),
    ("no-push-to-main", ["git push origin main", "git push origin HEAD:master",
                         "git push origin feature"], ["deny", "deny", "none"]),
    ("ask-before-discarding-work", ["git reset --hard", "git clean -fd", "git branch -D x",
                                    "git stash drop", "git checkout -- .", "git reset HEAD~1",
                                    "git checkout main"], ["ask"] * 5 + ["none"] * 2),
    ("protect-env-files", ["cat .env", "cat .env.example"], ["stop", "none"]),
    ("ask-before-installing", ["pip install x", "npm i x", "uv add x", "uv pip install x",
                               "python -m pip install x", "brew install x", "npm test",
                               "uv run pytest"], ["ask"] * 6 + ["none"] * 2),
    ("ask-before-recursive-delete", ["rm -rf build", "rm a.txt"], ["ask", "none"]),
    ("ask-before-infra-changes", ["terraform apply", "kubectl delete pod x", "terraform plan",
                                  "kubectl get pods"], ["ask", "ask", "none", "none"]),
    ("tests-before-push", ["git push", "uv run pytest", "git push",
                           ("Edit", {"file_path": "a.py"}), "git push", "npm test", "git push"],
     ["deny", "none", "none", "none", "deny", "none", "none"]),
    ("protect-secret-files", ["cat .env", ("Read", {"file_path": "/home/u/.aws/credentials"}),
                              "cp ~/.ssh/id_ed25519 /tmp/k", "base64 deploy.pem", "cat .env.example",
                              "cat README.md"], ["stop"] * 4 + ["none"] * 2),
    ("no-env-dumps", ["env", "printenv", "export -p", "echo $GITHUB_TOKEN",
                      "kubectl exec web -- env", "docker inspect web", "env FOO=1 ls", "set -e",
                      "echo $HOME", "export FOO=bar"], ["deny"] * 6 + ["none"] * 4),
    ("ask-before-reading-secret-stores", ["kubectl get secret db -o yaml", "kubectl config view --raw",
                                          "vault kv get secret/db", "sops -d s.enc.yaml",
                                          "aws ssm get-parameter --name x --with-decryption",
                                          "kubectl get pods", "kubectl config view",
                                          "aws ssm get-parameter --name x", "sops -e s.yaml"],
     ["ask"] * 5 + ["none"] * 4),
    ("ask-before-creating-credentials", ["aws iam create-access-key --user-name ci",
                                         "vault token create", "aws iam list-access-keys"],
     ["ask", "ask", "none"]),
    ("no-secrets-in-commands", ['curl -H "Authorization: Bearer abc" https://api.x',
                                "git remote add origin https://me:tok@github.com/o/r.git",
                                "mysql -uroot -psecret", "docker login -u me -p pw",
                                "kubectl create secret generic s --from-literal=pw=x",
                                "curl -H 'Accept: json' https://api.x", "mysql -uroot -p",
                                "docker login -u me --password-stdin",
                                "git remote add origin https://github.com/o/r.git"],
     ["deny"] * 5 + ["none"] * 4),
    ("no-leaky-debug", ['curl -v -H "Authorization: Bearer abc" https://api.x', "kubectl get pods -v=8",
                        "aws s3 ls --debug", "curl -v https://api.x", "kubectl get pods -v=4"],
     ["deny"] * 3 + ["none"] * 2),
    ("no-skipping-secret-scans", ["git commit --no-verify -m x", "git add -f .env.backup",
                                  "git commit -m x", "git add src"], ["ask", "ask", "none", "none"]),
]


@pytest.mark.parametrize("name, steps, expected", _BEHAVIOUR, ids=[b[0] for b in _BEHAVIOUR])
def test_pack_behaviour(name, steps, expected):
    assert run(guard_using(name), *steps) == expected


def test_read_before_overwrite(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    g = guard_using("read-before-overwrite", str(tmp_path))
    write = ("Write", {"file_path": "a.txt"})
    assert run(g, write, ("Read", {"file_path": "a.txt"}), write,
               ("Write", {"file_path": "new.txt"})) == ["deny", "none", "none", "none"]


def test_discarding_output_is_not_an_overwrite(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    g = guard_using("read-before-overwrite", str(tmp_path))
    assert run(g, "ls missing 2>/dev/null", "make >/dev/null 2>&1", "echo hi > /dev/stderr",
               "echo x > notes.txt") == ["none", "none", "none", "deny"]


class TestUse:
    def test_mode_override_and_replacement_by_id(self):
        rs = loads("use: [{no-force-push: {mode: warn}}, ask-before-recursive-delete]\n"
                   "rules: [{id: ask-before-recursive-delete, never: rm, mode: log}]")
        assert {r.id: (r.mode, r.source) for r in rs.rules} == {
            "no-force-push": ("warn", "library:no-force-push"),
            "ask-before-recursive-delete": ("log", "<rules>")}
        assert rs.used == ["no-force-push", "ask-before-recursive-delete"]

    def test_unknown_name_suggests_the_closest(self):
        with pytest.raises(RuleFileError, match="did you mean 'no-force-push'"):
            loads("use: [no-forcepush]")


class TestDisable:
    def test_a_project_can_switch_off_a_user_rule_and_the_built_in_one(self, tmp_path):
        user, project = tmp_path / "u.yaml", tmp_path / "p.yaml"
        user.write_text("rules: [{id: a, never: x}, {id: b, never: y}]")
        project.write_text("disable: [a, memory-first]\nuse: [no-force-push]\n"
                           "rules: [{id: c, never: z}]")
        assert [r.id for r in load([str(user), str(project)]).rules] == ["b", "no-force-push", "c"]
        assert "memory-first" in [r.id for r in load([str(user)]).rules]

    def test_one_rule_of_a_pack(self):
        rs = loads("use: [subagents-on-sonnet]\ndisable: [subagents-on-sonnet-no-fork]")
        assert [r.id for r in rs.rules] == ["subagents-on-sonnet"]


class TestCommands:
    @pytest.fixture
    def here(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        return tmp_path / "AGENTLTL.yaml"

    def test_use_unuse_disable_enable_keep_the_rest_of_the_file(self, here):
        here.write_text("# my rules\nrules:\n  - id: mine   # keep me\n    never: rm\n")
        assert cli.main(["use", "no-force-push", "tests-before-push"]) == 0
        assert cli.main(["use", "tests-before-push", "--mode", "warn"]) == 0
        assert cli.main(["disable", "memory-first"]) == 0
        assert cli.main(["unuse", "no-force-push"]) == 0
        text = here.read_text()
        assert "# my rules" in text and "- id: mine   # keep me" in text
        rs = loads(text)
        assert rs.used == ["tests-before-push"] and rs.disabled == ["memory-first"]
        assert rs.get("tests-before-push").mode == "warn"
        assert cli.main(["enable", "memory-first"]) == 0
        assert "disable" not in here.read_text()

    def test_use_creates_the_file_and_rejects_unknown_names(self, here, capsys):
        assert cli.main(["use", "no-forcepush"]) == 1
        assert "did you mean 'no-force-push'" in capsys.readouterr().err
        assert not here.exists()
        assert cli.main(["use", "no-force-push"]) == 0
        assert [r.id for r in loads(here.read_text()).rules] == ["no-force-push"]

    def test_library_lists_what_is_on(self, here, capsys):
        here.write_text("use: [no-force-push]\n")
        assert cli.main(["library"]) == 0
        line = next(ln for ln in capsys.readouterr().out.splitlines() if "no-force-push" in ln)
        assert line.split()[0] == "on"
