"""
agentltl_coding/session.py – what a harness's hooks do, whatever their I/O.

Each agent calls its hooks with its own payloads and expects its own JSON back; what they
do in between is the same everywhere. A harness turns its payload into one of these calls
and the result into its answer::

    s = Session(cwd, project, session_id)
    if not s.files: ...                       # no AGENTLTL.yaml: stay out of the way
    s.prompt()                                # the user wrote: lift a stop, new turn
    s.start()                                 # the reminder of the rules, or None
    s.pre("Bash", {"command": "git push"}, auto=False)    # a Verdict
    s.post("Bash", {...}, "id", output, status=0)         # (user notice, agent note) or None
    s.finish()                                # a Verdict: "block" while `finally` is unmet

Tool names may be the agent's own: the guard maps them (``Harness.tool_aliases``).
:class:`~agentltl_coding.rules.RuleFileError` from a broken rule file reaches the caller,
which tells the user (see :func:`broken`).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from . import store
from .guard import Guard, Verdict
from .harness import current as _harness
from .pattern import Paths
from .rules import RuleSet, load, rule_files

STILL_STOPPED = (
    "[AGENTLTL] Rule '{rule}' stopped this session: every tool call is refused until the user "
    "replies. Nothing was executed. Don't try another command or a workaround. Tell the user "
    "what you were trying to do, why, and what you need from them, then end your turn.")
_MAX_DECISIONS = 200


class Session:
    def __init__(self, cwd: str, project: str, session_id: Optional[str]) -> None:
        self.cwd, self.project, self.sid = cwd, project, session_id or "default"
        self.files: List[str] = rule_files(cwd, project)
        self._ruleset: Optional[RuleSet] = None
        logging.getLogger("agentltl").setLevel(logging.ERROR)
        logging.getLogger("cli_to_tools").setLevel(logging.ERROR)

    @property
    def ruleset(self) -> RuleSet:
        """The compiled rules. Raises RuleFileError for a broken rule file."""
        if self._ruleset is None:
            self._ruleset = load(self.files, Paths(self.cwd, self.project))
        return self._ruleset

    # ── events ────────────────────────────────────────────────────────────────

    def prompt(self) -> None:
        """The user wrote: lift a stop (even if the rule file has since become unreadable),
        and let ``finally`` rules send the agent back again in this new turn."""
        with store.locked(self.sid) as state:
            state.pop("stopped", None)
            state["termination_nudges"] = 0
        with store.locked_project(self.project) as project_state:
            project_state["termination_nudges"] = 0

    def start(self) -> Optional[str]:
        """What to tell the agent at the start of a session (None: settings.announce off)."""
        return reminder(self.ruleset)

    def pre(self, tool: str, tool_input: Dict[str, Any], *, auto: bool = False) -> Verdict:
        """Decide a call before it runs. After a ``stop``, every call is denied until
        :meth:`prompt`."""
        def decide(guard: Guard, state: Dict[str, Any]) -> Verdict:
            if state.get("stopped"):
                return Verdict("deny", STILL_STOPPED.format(**state["stopped"]),
                               rule=state["stopped"].get("rule"))
            verdict = guard.decide(tool, tool_input, auto=auto)
            if verdict.action == "stop":
                state["stopped"] = {"rule": verdict.rule or "?"}
            return verdict

        return self._with_guard(decide, _harness().canonical(tool, tool_input))

    def post(self, tool: str, tool_input: Dict[str, Any], tool_id: str, result: Any,
             status: Optional[int] = None) -> Optional[Tuple[str, str]]:
        """Record a call that ran (status 0) or failed (1). Returns what to say when its output
        holds a credential: (notice for the user, instruction for the agent)."""
        self._with_guard(lambda guard, state: guard.record(tool, tool_input, tool_id, result,
                                                           status=status))
        if not self.ruleset.settings.scan_output or result is None:
            return None
        from .scan import credential_kinds, report
        kinds = credential_kinds(result)
        return report(_harness().canonical(tool, {})[0], kinds) if kinds else None

    def finish(self) -> Verdict:
        """The agent is about to end its turn: "block" with what is missing while a
        ``finally`` rule is unmet."""
        return self._with_guard(lambda guard, state: guard.finish()) or Verdict()

    # ── state ─────────────────────────────────────────────────────────────────

    def _with_guard(self, fn: Any, call: Optional[Tuple[str, Dict[str, Any]]] = None) -> Any:
        """Run *fn(guard, session_state)* on the saved state and save it again. A verdict on
        *call* that intervenes is logged for ``agentltl trace``."""
        guard = Guard(self.ruleset, Paths(self.cwd, self.project))
        with store.locked(self.sid) as state, store.locked_project(self.project) as project_state:
            guard.restore(state, project_state)
            out = fn(guard, state)
            for old in ("trace", "engine"):          # saved before AgentLTL 0.2
                state.pop(old, None)
            state.update(guard.dump())
            state["project_dir"] = self.project
            project_state.update(guard.dump_project())
            if call and isinstance(out, Verdict) and out.action not in ("none", "block"):
                state.setdefault("decisions", []).append(
                    {"tool": call[0], "input": call[1], "action": out.action, "rule": out.rule})
                state["decisions"] = state["decisions"][-_MAX_DECISIONS:]
        return out


def reminder(ruleset: RuleSet) -> Optional[str]:
    """The session-start reminder: the rules in force, one line each."""
    if not ruleset.settings.announce:
        return None
    lines = [
        f"This project enforces {len(ruleset.rules)} AGENTLTL rule(s) on every tool call, shell "
        "commands included (each command line is checked as the sequence of commands it runs). "
        "A call that breaks a rule is refused with the reason; follow it rather than working "
        "around it. Rules:",
    ]
    note = _harness().memory_note
    for r in ruleset.rules:
        if r.id == "memory-first" and r.kind == "never" and note:
            lines.append(f"- memory-first [{r.mode}]: {note}")
            continue
        why = f" — {r.why}" if r.why else ""
        memory = ", whole project" if r.scope == "project" else ""
        lines.append(f"- {r.id} [{r.mode}{memory}]: {r.summary}{why}")
    return "\n".join(lines)


def broken(problems: List[str]) -> str:
    """What to tell the agent (and through it the user) about a broken rule file."""
    return ("AGENTLTL.yaml has errors, so NO AGENTLTL rules are being enforced until it is "
            "fixed:\n" + "\n".join(f"- {p}" for p in problems))


__all__ = ["Session", "STILL_STOPPED", "broken", "reminder"]
