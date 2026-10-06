"""Golden verdicts: what every packaged rule, example file and formula rule decides.

A refactor must keep these verdicts. An intended change is made by regenerating the file
(AGENTLTL_GOLDEN_UPDATE=1 pytest tests/test_golden.py) and reviewing its diff.
"""

import json
import os
from pathlib import Path

import pytest

from agentltl_coding.rules import RuleFileError, library

from .conftest import guard_for
from .test_library import _BEHAVIOUR, guard_using

GOLDEN = Path(__file__).parent / "golden" / "verdicts.json"
EXAMPLES = Path(__file__).parent.parent / "examples"

R, E, W = "Read", "Edit", "Write"

# Calls that exercise every example file: git, tests, infra, files, network, subagents.
CORPUS = [
    "git status", "git push origin main", "git push", "pytest", "git push",
    (E, {"file_path": "src/app.py"}), "git push", "git push -f", "git push -f",
    "cat .env", "cat README.md", (W, {"file_path": "poetry.lock"}), (R, {"file_path": "src/app.py"}),
    "alembic revision -m a", "alembic revision -m b", "alembic revision -m b",
    "terraform apply", "terraform plan", "terraform apply", "gh pr create", "gh pr create",
    "git reset --hard", "git reset --hard", (W, {"file_path": "docs/x.md"}),
    "psql prod", "curl https://example.com", "ruff check", "git commit -m 'fix: x'",
    "sudo ls", ("WebFetch", {"url": "https://x"}), "curl https://example.com",
    (E, {"file_path": "results/table.csv"}), "python run_experiment.py",
    ("Agent", {"prompt": "x"}), ("Agent", {"prompt": "x"}), ("Agent", {"prompt": "x"}),
    ("Agent", {"prompt": "x"}), "rm -rf build", "kubectl drain n1", "reboot", "ls",
]

FORMULAS = {
    "once-deploy": ('G(now("deploy") -> X(G(!now("deploy"))))',
                    ["deploy", "deploy", "deploy", "ls", "cat x", "deploy"]),
    "respond-after": ('G(now("deploy") -> F(now("notify")))', ["deploy", "ls", "notify"]),
    "not-before": ('!before("a", "b")', ["a", "b", "ls"]),
    "before": ('before("a", "b")', ["b", "a", "b"]),
    "never-after-web": ('G(now("WebFetch") -> G(!now("curl")))',
                        ["curl x", ("WebFetch", {"url": "u"}), "curl x", "curl x", "ls"]),
    "eventually": ('F(now("done"))', ["ls", "done"]),
    "next": ('G(now("rm") -> X(now("log")))', ["rm x", "ls", "ls", "rm x", "log", "ls"]),
}
MODES = ["block", "warn", "log", "retry"]


def _run(guard, steps):
    out = []
    for step in steps:
        tool, tool_input = ("Bash", {"command": step}) if isinstance(step, str) else step
        v = guard.decide(tool, tool_input)
        out.append(f"{v.action}:{v.rule or ''}" if v.action != "none" else "none")
        if v.action == "none":
            guard.record(tool, tool_input, "id", "")
    return out


def _formula_rules(formula, mode):
    return f"rules:\n  - id: r\n    ltl: '{formula}'\n    mode: {mode}\n    why: test\n"


def verdicts():
    got = {}
    for name, steps, _ in _BEHAVIOUR:
        got[f"pack/{name}"] = _run(guard_using(name), steps)
    for name in sorted(library()):
        got[f"corpus/{name}"] = _run(guard_using(name), CORPUS)
    for path in sorted(EXAMPLES.glob("*.yaml")):
        got[f"example/{path.name}"] = _run(guard_for(path.read_text()), CORPUS)
    for name, (formula, steps) in FORMULAS.items():
        for mode in MODES:
            try:
                guard = guard_for(_formula_rules(formula, mode))
            except RuleFileError:
                got[f"ltl/{name}/{mode}"] = "rejected"
                continue
            got[f"ltl/{name}/{mode}"] = _run(guard, steps)
    return got


def test_golden_verdicts():
    got = verdicts()
    if os.environ.get("AGENTLTL_GOLDEN_UPDATE"):
        GOLDEN.parent.mkdir(exist_ok=True)
        lines = (f"{json.dumps(k)}: {json.dumps(got[k])}" for k in sorted(got))
        GOLDEN.write_text("{\n" + ",\n".join(lines) + "\n}\n")
        pytest.skip("golden verdicts rewritten")
    want = json.loads(GOLDEN.read_text())
    changed = {k: (want.get(k), got.get(k)) for k in set(want) | set(got) if want.get(k) != got.get(k)}
    assert not changed, json.dumps(changed, indent=1)
