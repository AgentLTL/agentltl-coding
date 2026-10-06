"""
agentltl_coding/guard.py – decide one coding-agent tool call against the rules.

    guard = Guard(ruleset, Paths(cwd, root))
    guard.restore(session_state, project_state)   # traces and enforcer state
    verdict = guard.decide("Bash", {"command": "git push"}, auto=False)
    verdict.action                       # "none" | "deny" | "ask" | "stop"
    ...after the call ran...
    guard.record("Bash", {"command": "pytest"}, "toolu_1", "3 passed", status=0)
    session_state, project_state = guard.dump(), guard.dump_project()

Each rule reads one memory (its ``scope``): the session trace (calls made in this session)
or the project trace (every call made in this project, across sessions). Both record every
call that runs; each scope has its own AgentLTL enforcer, and the stricter verdict wins.

Shell tools (see :class:`agentltl_coding.Harness`) are translated by cli-to-tools into the
structured calls of the command line (``git commit -m x && git push`` → ``git_commit``,
``git_push``) and checked all or nothing. Every other tool is checked under its own name
with its input as arguments.

The guard never approves anything: "none" means it has no objection and the harness's own
permission flow still decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, NamedTuple, Optional

from cli_to_tools import SpecRegistry, ToolCall, TranslationError, Translator
from cli_to_tools.agentltl import ShellEnforcer

from .harness import current as _harness
from .pattern import PATH_KEYS, Paths, normalize_paths
from .rules import MODES, SCOPES, Rule, RuleSet

AUTO_MODES = ("auto", "bypassPermissions", "dontAsk")
_MAX_TRACE = 5000
_MAX_LOG = 200
_MAX_STRING = 2000
_RANK = {"none": 0, "ask": 1, "deny": 2, "stop": 3}

_TAILS = {
    "block": "This rule cannot be overridden by you. Do something that satisfies it instead, "
             "or explain the situation to the user.",
    "warn": "This is a warning. If you are sure the call is right, you may override the rule by "
            "repeating exactly the same call as your next action; otherwise comply.",
    "retry": "Change your approach to satisfy the rule. If you keep getting blocked, the user "
             "will be asked to decide.",
    "stop": "This rule stops the session: every tool call is refused until the user replies. "
            "Don't try another command. Tell the user what you were trying to do and why, then "
            "end your turn.",
}
# A `stop` rule that only *may* match (a file known at run time) refuses the call instead.
_MAYBE_STOP = ("This rule stops the session when it is broken. This call was refused because its "
               "files can't be known in advance: run the command that finds them first, then "
               "name them explicitly. If one of them is a file this rule protects, don't touch "
               "it: tell the user.")
# Told to the agent alongside a prompt the user answers (mode: ask, or retry escalating).
_ASKED = ("AGENTLTL rule '{name}' asked the user to approve this call. If they decline, do not "
          "retry it or work around the rule; ask the user what they want instead.")


@dataclass
class Verdict:
    """What the hook should tell Claude Code.

    action: "none" (no objection), "deny", "ask" (the user decides) or "stop" (deny and halt).
    reason: Shown with deny/ask/stop. context: extra text for Claude, whatever the action.
    """

    action: str = "none"
    reason: str = ""
    context: str = ""
    rule: Optional[str] = None
    calls: List[Dict[str, Any]] = field(default_factory=list)


class _Call(NamedTuple):
    name: str
    args: Dict[str, Any]


def translator_for(ruleset: RuleSet, paths: Optional[Paths] = None) -> Translator:
    """cli-to-tools with every bundled pack, the rule files' own ``tools:`` specs (which
    extend the bundled spec of the same command unless they say ``extend: false``), and the
    shell effects file rules need (redirections, paths after ``cd``, run-time-only files)."""
    registry = SpecRegistry()
    if ruleset.tool_specs:
        registry.load_dict(ruleset.tool_specs, extend=True)
    return Translator(registry, shell_effects=True, paths=paths)


# runners whose subcommands cli-to-tools does not split into tools (`make test` -> make, argv)
_RUNNERS = ("make", "npm", "yarn", "pnpm", "npx", "cargo", "go", "uv", "poetry", "just", "bun")
_ANY_TOOL_ARGS = ("*", "redirect_to", "overwrite_to", "redirect_from", "extra_args")


def lint(ruleset: RuleSet, registry: Optional[SpecRegistry] = None) -> List[str]:
    """Warnings for targets that can never match: unknown tools or argument names.

    The rule file is valid without them; these catch the rule that silently never fires
    (``with: {repository: origin}`` on ``git_push``, whose argument is ``remote``) or, for
    ``require``, fires on every call.
    """
    registry = registry or translator_for(ruleset).registry
    props = {s["name"]: s.get("parameters", {}).get("properties", {})
             for s in registry.tool_schemas()}
    schemas = {name: set(p) for name, p in props.items()}
    out: List[str] = []
    for rule in ruleset.rules:
        if rule.kind in ("ltl", "formula"):
            for tool in rule.tools:
                if tool[:1].isupper() or tool.startswith("mcp__") or "*" in tool:
                    continue
                if tool in schemas or registry.get(tool) is not None:
                    continue
                import difflib
                close = difflib.get_close_matches(tool, list(schemas), n=1)
                hint = f" Did you mean '{close[0]}'?" if close else ""
                if "_" in tool and registry.get(tool.split("_", 1)[0]) is not None:
                    out.append(f"{rule.id}: '{tool}' is not a declared subcommand, so only "
                               f"`{tool.replace('_', ' ', 1)}` would produce it.{hint}")
                else:
                    out.append(f"{rule.id}: no command translates to '{tool}', so the formula "
                               f"never sees it.{hint}")
            continue
        if rule.kind == "before" and rule.targets:
            for target in _flatten(rule.targets[:1]):
                for tool in target.tools:
                    lists = sorted(arg for arg in target.variables
                                   if props.get(tool, {}).get(arg, {}).get("type") == "array")
                    if lists:
                        out.append(f"{rule.id}: {tool}.{lists[0]} is a list, and a $variable "
                                   "on the 'first' side is compared with the whole list, so "
                                   f"`{tool.replace('_', ' ')} a b` never matches one file. "
                                   "Use a single-valued argument (e.g. Read's file_path).")
        for target in _flatten(rule.targets):
            keys = set(target.with_) | set(target.where) | set(target.variables)
            for tool in target.tools:
                if tool[:1].isupper() or tool.startswith("mcp__") or "*" in tool:
                    continue
                if tool in schemas:
                    unknown = sorted(k for k in keys - set(_ANY_TOOL_ARGS) if k not in schemas[tool])
                    if unknown:
                        out.append(f"{rule.id}: {tool} has no argument(s) {unknown}; it has "
                                   f"{sorted(schemas[tool])}")
                    continue
                prefix = tool.split("_", 1)[0]
                if "_" in tool and registry.get(prefix) is not None:
                    # a subcommand the spec does not declare (`git notes` -> git_notes) keeps
                    # its words in `argv`
                    unknown = sorted(keys - set(_ANY_TOOL_ARGS) - {"argv"})
                    if unknown:
                        out.append(f"{rule.id}: {tool} is not a declared subcommand, so its "
                                   f"only argument is 'argv'; {unknown} never match.")
                elif "_" in tool and prefix in _RUNNERS:
                    out.append(f"{rule.id}: no command translates to '{tool}'. Check with "
                               f"`agentltl translate \"{tool.replace('_', ' ', 1)}\"`; "
                               f"a command without a spec is '{prefix}' with an 'argv' list, "
                               f"e.g. {{tool: {prefix}, with: {{argv: ...}}}}")
                elif registry.get(tool) is None:
                    unknown = sorted(keys - set(_ANY_TOOL_ARGS) - {"argv"})
                    if unknown:
                        out.append(f"{rule.id}: '{tool}' has no spec, so its only argument is "
                                   f"'argv'; {unknown} never match. Add a spec under 'tools:'.")
    return out


def _flatten(targets: Any) -> List[Any]:
    out: List[Any] = []
    for t in targets:
        out += _flatten(t.targets) if hasattr(t, "targets") else [t]
    return out


def is_auto(permission_mode: Optional[str]) -> bool:
    return permission_mode in AUTO_MODES


class Guard:
    def __init__(self, ruleset: RuleSet, paths: Optional[Paths] = None) -> None:
        from agentltl import ConstraintSeverity

        import os
        self.ruleset = ruleset
        self.paths = paths or Paths(os.getcwd())
        self.translator = translator_for(ruleset, self.paths)
        s = ruleset.settings
        self.enforcers = {
            scope: ShellEnforcer(
                ruleset.constraints(scope), ruleset.severities(scope),
                default_severity=ConstraintSeverity.PERSISTENT_BLOCK,
                max_soft_attempts=s.retries, soft_block_mode=s.retry_counting,
                escalate_to=ConstraintSeverity.ASK, nudge_max=s.report,
                translator=self.translator, shell_tools=_harness().shell_tools,
            )
            for scope in SCOPES
        }

    @property
    def shell_tools(self) -> Dict[str, str]:
        return _harness().shell_tools

    # ── state ─────────────────────────────────────────────────────────────────

    def restore(self, session: Optional[Dict[str, Any]],
                project: Optional[Dict[str, Any]] = None) -> None:
        for scope, state in (("session", session), ("project", project)):
            self.enforcers[scope].from_state(_upgrade(state or {}))

    def dump(self, scope: str = "session") -> Dict[str, Any]:
        return self.enforcers[scope].to_state(max_trace=_MAX_TRACE, max_log=_MAX_LOG)

    def dump_project(self) -> Dict[str, Any]:
        return self.dump("project")

    @property
    def trace(self) -> List[Dict[str, Any]]:
        return self.enforcers["session"].trace

    @property
    def project_trace(self) -> List[Dict[str, Any]]:
        return self.enforcers["project"].trace

    # ── deciding ──────────────────────────────────────────────────────────────

    def translate(self, tool_name: str, tool_input: Dict[str, Any]) -> List[ToolCall]:
        """The structured calls a tool call stands for.

        Raises:
            TranslationError: For a shell command line cli-to-tools cannot analyse.
        """
        if tool_name in self.shell_tools:
            return self.translator.translate((tool_input or {}).get(self.shell_tools[tool_name]) or "")
        return [ToolCall(tool_name, self._input(tool_name, tool_input), "", {})]

    def _input(self, tool_name: str, tool_input: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The arguments rules see: path arguments of non-shell tools made absolute."""
        tool_input = dict(tool_input or {})
        return tool_input if tool_name in self.shell_tools else normalize_paths(
            tool_input, self.paths, PATH_KEYS)

    def decide(self, tool_name: str, tool_input: Dict[str, Any], *, auto: bool = False) -> Verdict:
        if not self.ruleset.rules:
            return Verdict()
        tool_input = tool_input or {}
        try:
            calls = self.translate(tool_name, tool_input)
        except TranslationError as exc:
            return self._unparseable(tool_input.get("command", ""), exc, auto)
        shown = [{"tool_name": c.name, "arguments": c.args} for c in calls]
        verdicts = [self._decide_in(scope, tool_name, tool_input, calls, shown)
                    for scope in SCOPES if self.ruleset.scoped(scope)]
        verdict = max(verdicts, key=lambda v: _RANK[v.action])
        verdict.context = "\n".join(v.context for v in verdicts if v.context)
        return verdict

    def _decide_in(self, scope: str, tool_name: str, tool_input: Dict[str, Any],
                   calls: List[ToolCall], shown: List[Dict[str, Any]]) -> Verdict:
        enf = self.enforcers[scope]
        enf.begin_generation()          # each call the harness reports is its own turn
        d = enf.check(tool_name, self._input(tool_name, tool_input))
        notes = self._tolerated(d.notes, enf, calls)
        if d.allowed:
            return Verdict("none", context=notes, calls=shown)
        v = d.violation
        name = v.constraint_name if v else ""
        rule = self.ruleset.get(name)
        index = d.index if d.index is not None else len(calls) - 1
        detail = self._explain(rule, enf, calls, index) or _plain(v.detail if v else "")
        segment = (calls[index].meta or {}).get("source", "") if calls else ""
        agent = _harness().Agent
        if d.action == "stop":
            if v is not None and v.uncertain:
                # Only a possible match (a file known at run time): refuse the call, but don't
                # stop the session over what naming the files would settle.
                return Verdict("deny", self._message(rule, name, detail, segment, mode="block",
                                                     tail=_MAYBE_STOP), rule=name, calls=shown)
            return Verdict("stop", self._message(rule, name, detail, segment, mode="stop"),
                           rule=name, calls=shown)
        if d.action == "ask":
            intro = (f"{agent} was refused {d.attempts - 1} time(s) by this rule and is trying "
                     "again." if d.escalated else "")
            reason = self._ask_message(rule, name, detail, segment, intro=intro)
            context = "\n".join(n for n in (_ASKED.format(name=name), notes) if n)
            return Verdict("ask", reason, context=context, rule=name, calls=shown)
        tail = None
        if d.action == "retry":
            tail = f"Attempt {d.attempts} of {self.ruleset.settings.retries}. " + _TAILS["retry"]
        mode = rule.mode if rule else "block"
        reason = self._message(rule, name, detail, segment, mode=mode, tail=tail)
        also = self._also(d.violations[1:], enf, calls, index)
        if also:
            reason = reason + "\n" + also
        return Verdict("deny", reason, context=notes, rule=name, calls=shown)

    def _history(self, enf: ShellEnforcer, calls: List[ToolCall], index: int) -> List[_Call]:
        """The calls up to the one at *index* of this command line, for explanations."""
        past = [_Call(e.get("tool_name", ""), e.get("arguments") or {}) for e in enf.trace]
        return past + [_Call(c.name, c.args) for c in calls[:index + 1]]

    def _explain(self, rule: Optional[Rule], enf: ShellEnforcer, calls: List[ToolCall],
                 index: int) -> Optional[str]:
        if rule is None or rule.explain is None or not calls:
            return None
        try:
            return rule.explain(self._history(enf, calls, index))
        except Exception:   # an explanation must never turn a refusal into a crash
            return None

    def _also(self, others: List[Any], enf: ShellEnforcer, calls: List[ToolCall],
              index: int) -> str:
        """Further rules the same call breaks, reported with the first (settings: report)."""
        lines = []
        for v in others:
            rule = self.ruleset.get(v.constraint_name)
            detail = self._explain(rule, enf, calls, index) or _plain(v.detail)
            lines.append(f"This call also breaks rule '{v.constraint_name}': {detail}"
                         + (f" To comply: {rule.fix}" if rule and rule.fix else ""))
        return "\n".join(lines)

    def record(self, tool_name: str, tool_input: Dict[str, Any], tool_id: str,
               result: Any, status: Optional[int] = None) -> None:
        """Add a call that has run to the session and project traces."""
        tool_input = _trim(self._input(tool_name, tool_input))
        text = result if isinstance(result, str) else ("" if result is None else str(result))
        text = text[:_MAX_STRING]
        for enf in self.enforcers.values():
            try:
                enf.record_completed(tool_name, tool_input, tool_id, text, status=status)
            except TranslationError:
                # an unparseable command the user let through: kept under the tool's own name
                entry = {"tool_name": tool_name, "arguments": tool_input, "id": tool_id,
                         "result": text}
                if status is not None:
                    entry["status"] = status
                enf.record_entry(entry)

    # ── messages ──────────────────────────────────────────────────────────────

    def _unparseable(self, command: str, exc: Exception, auto: bool) -> Verdict:
        s = self.ruleset.settings
        how = s.unparseable_auto if auto else s.unparseable_interactive
        if how == "allow":
            return Verdict()
        related = [r.id for r in self.ruleset.rules if _shell_side(r)]
        text = (
            "This command could not be checked against the project's AGENTLTL rules: "
            f"{exc}. "
            + (f"Rules it could fall under: {', '.join(related)}. " if related else "")
            + "To have it checked, write it as plain commands joined with ;, &&, || or | "
              "(no eval, background jobs, function definitions or commands named by variables)."
        )
        if how == "ask":
            return Verdict("ask", "[AGENTLTL: not checked] " + text)
        if how == "deny":
            return Verdict("deny", "[AGENTLTL: not checked, denied] " + text)
        return Verdict("none", context="[AGENTLTL: not checked] " + text)

    def _tolerated(self, notes: List[Any], enf: ShellEnforcer, calls: List[ToolCall]) -> str:
        lines = []
        for v in notes:
            rule = self.ruleset.get(v.constraint_name)
            why = f" ({rule.why})" if rule and rule.why else ""
            detail = self._explain(rule, enf, calls, len(calls) - 1) or _plain(v.detail)
            lines.append(f"Note: this call breaks AGENTLTL rule '{v.constraint_name}'{why}: {detail}")
        return "\n".join(dict.fromkeys(lines))

    @staticmethod
    def _ask_message(rule: Optional[Rule], name: str, detail: str, segment: str,
                     intro: str = "") -> str:
        """The permission prompt the user reads: what the rule protects, and the question."""
        lines = [f"AgentLTL: this call breaks the rule '{name}'.", intro]
        if rule and rule.why:
            lines.append(f"Why the rule exists: {rule.why}")
        if detail:
            lines.append(f"What breaks it: {detail}")
        if segment:
            lines.append(f"Command: {segment}")
        lines.append(f"{_harness().Agent} cannot override this rule. Allow this call anyway?")
        return "\n".join(line for line in lines if line)

    def _message(self, rule: Optional[Rule], name: str, detail: str, segment: str, *,
                 mode: Optional[str] = None, tail: Optional[str] = None) -> str:
        mode = mode or (rule.mode if rule else "block")
        lines = [f"[AGENTLTL] Rule '{name}' blocked this call ({mode}). Nothing was executed."]
        if rule and rule.why:
            lines.append(f"Rule: {rule.why}")
        if detail:
            lines.append(f"Problem: {detail}")
        if segment:
            lines.append(f"Blocked at: {segment}")
        if rule and rule.fix:
            lines.append(f"To comply: {rule.fix}")
        lines.append(tail or _TAILS.get(mode, ""))
        return "\n".join(line for line in lines if line)


def _plain(detail: str) -> str:
    """AgentLTL's explanation, tidied for a person to read."""
    return str(detail or "").strip()


def _upgrade(state: Dict[str, Any]) -> Dict[str, Any]:
    """State saved before AgentLTL 0.2 (``trace`` plus private ``engine`` fields), in
    :meth:`agentltl.Enforcer.to_state` form."""
    if "engine" not in state and "trace" not in state:
        return state
    out = {name.lstrip("_"): value for name, value in (state.get("engine") or {}).items()
           if name != "_blocked_command"}
    out["completed_tool_calls"] = list(state.get("trace") or [])
    return out


def _shell_side(rule: Rule) -> bool:
    """Whether a rule mentions any tool a shell command can turn into."""
    return any(not (t[:1].isupper() or t.startswith("mcp__")) for t in rule.tools) or not rule.tools


def _trim(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "…"
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim(v) for v in value]
    return value


__all__ = ["Guard", "Verdict", "is_auto", "lint", "translator_for", "MODES"]
