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

Three harnesses are built on it: the
[Claude Code plugin](https://github.com/AgentLTL/agentltl-claude-code), the
[GitHub Copilot CLI plugin](https://github.com/AgentLTL/agentltl-copilot-cli) and the
[Mistral Vibe plugin](https://github.com/AgentLTL/agentltl-mistral-vibe). The full rule
reference is on [agentltl.github.io](https://agentltl.github.io/rules/).

## A harness in a few lines

```python
from agentltl_coding import Harness, Session, configure

configure(Harness(name="my-agent", agent="the agent", user_dir="~/.my-agent",
                  shell_tools={"Bash": "command"},
                  tool_aliases={"shell": ("Bash", {"cmd": "command"}),
                                "write_file": ("Write", {"path": "file_path"})}))

s = Session(cwd, project_root, session_id)       # one per hook call; state is on disk
if s.files:                                      # an AGENTLTL.yaml applies here
    verdict = s.pre("shell", {"cmd": "git push --force"})      # before the call
    # verdict.action: "none" | "deny" | "ask" | "stop"; verdict.reason: the message
    ...
    s.post("shell", {"cmd": "pytest"}, call_id, output, status=0)   # after it ran
    s.finish()          # the agent wants to stop: "block" while a `finally` rule is unmet
    s.prompt()          # the user wrote: lift a `stop`
```

`Harness` says what differs between agents:

- the agent's name in messages, and where the user's `AGENTLTL.yaml` lives;
- `tool_aliases`: its tool names mapped onto the canonical ones rules use (Claude Code's
  `Bash`, `Write`, `Edit`, `Read`...), so the library works unchanged (Copilot's
  `create {path, file_text}` is `Write {file_path, content}`);
- which tools take shell command lines, and the permission modes in which nobody answers a
  prompt;
- built-in rules of its own (`memory-first`), and where its memory lives, for
  `agentltl memory scan` (`agentltl_coding.memory`).

`Session` is what the hooks do: it keeps the session and project traces on disk, holds a
`stop` until the user replies, logs interventions, and scans call output for credentials.
`Guard` underneath decides one call. `agentltl_coding.cli.main(argv, extend=...)` is the
`agentltl` command (validate, check, translate, tools, library, use, memory...), to which a
harness can add commands. A library entry with `harnesses: [claude-code]` is for that agent
only; elsewhere `use:` of it switches nothing on, so one project file serves every agent.

## Testing a harness end to end

`e2e/` lets a plugin test itself in the real agent, without a model or an account:

- `e2e/scripted_model.py SCRIPT.json` serves the OpenAI chat-completions and Anthropic
  messages formats (Mistral Vibe, Copilot CLI with a custom provider, Claude Code with
  `ANTHROPIC_BASE_URL`) and answers each request with the next step of a script: tool calls,
  then a final text.
- `e2e/check.py TRACE EXPECTED` checks that `agentltl trace` shows the expected refusals, in
  order.
- `.github/workflows/agent-e2e.yml` is a reusable workflow: it runs the plugin's
  `docker/e2e.sh` and, when that fails on a pull request (such as Dependabot's update of the
  agent's version), says so on the pull request and mentions who to notify.

Each plugin pins the agent version it tests in `docker/` (a `package.json` or a
`requirements.txt`), so Dependabot proposes every new release as a pull request.

## Develop

```bash
pip install -e ".[dev]" "agentltl @ git+https://github.com/AgentLTL/AgentLTL.git" \
    "cli-to-tools @ git+https://github.com/AgentLTL/cli-to-tools.git"
pytest -q
```

`tests/golden/verdicts.json` records what every packaged rule, example file and formula
rule decides, step by step; a change that alters a verdict regenerates it
(`AGENTLTL_GOLDEN_UPDATE=1 pytest tests/test_golden.py`) and shows up as a reviewed diff.
