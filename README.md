# agentltl-coding

AgentLTL rules for coding agents, whatever the harness: the `AGENTLTL.yaml` rule language, a
library of tested rules, and a guard that decides each tool call before it runs.

- **Rules** in plain YAML, compiled to [AgentLTL](https://github.com/AgentLTL/AgentLTL)
  formulas over the calls made so far:

  ```yaml
  rules:
    - id: tests-before-push
      before: {first: pytest, then: git_push, since: [Edit, Write]}
      why: CI is slow; run the tests locally first.
      mode: warn
  use: [no-force-push, protect-secret-files]   # from the library
  ```

- **Shell commands** are checked as the structured calls of their command line
  ([cli-to-tools](https://github.com/AgentLTL/cli-to-tools)): `git add . && git push -f` is
  `git_add`, then `git_push {force: true}`, checked all or nothing, with redirections,
  paths after `cd`, and files only known at run time accounted for.
- **Modes** map one to one onto AgentLTL severities: `block`, `warn` (the agent may insist),
  `ask` (you decide), `retry` (then you decide), `stop`, `log`.
- **Memory:** each rule reads the calls of this session, or of the project across sessions.

The [Claude Code plugin](https://github.com/AgentLTL/agentltl-claude-code) is the first
harness built on it. The full rule reference is on
[agentltl.github.io](https://agentltl.github.io/rules/).

## A harness in a few lines

```python
from agentltl_coding import Guard, Harness, Paths, configure, load, rule_files

configure(Harness(name="my-agent", agent="the agent", user_dir="~/.my-agent",
                  shell_tools={"shell": "command"}))

ruleset = load(rule_files(cwd), Paths(cwd, project_root))
guard = Guard(ruleset, Paths(cwd, project_root))
guard.restore(session_state, project_state)

verdict = guard.decide("shell", {"command": "git push --force"})
# verdict.action: "none" | "deny" | "ask" | "stop"; verdict.reason: the message
...
guard.record("shell", {"command": "pytest"}, call_id, output, status=0)   # after it ran
session_state, project_state = guard.dump(), guard.dump_project()
```

`Harness` says what differs between agents: the agent's name in messages, where the user's
`AGENTLTL.yaml` lives, which tools take shell command lines, and built-in rules of its own
(the Claude Code plugin adds `memory-first`). `agentltl_coding.cli.main(argv, extend=...)`
is the `agentltl` command (validate, check, translate, tools, library, use, ...), to which a
harness can add commands.

## Develop

```bash
pip install -e ".[dev]" "agentltl @ git+https://github.com/AgentLTL/AgentLTL.git" \
    "cli-to-tools @ git+https://github.com/AgentLTL/cli-to-tools.git"
pytest -q
```

`tests/golden/verdicts.json` records what every packaged rule, example file and formula
rule decides, step by step; a change that alters a verdict regenerates it
(`AGENTLTL_GOLDEN_UPDATE=1 pytest tests/test_golden.py`) and shows up as a reviewed diff.
