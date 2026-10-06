"""
agentltl_coding/cli.py – the ``agentltl`` command.

    agentltl validate [FILE]             compile the rules; list them or the problems
    agentltl check STEP...               replay steps through the rules in a fresh session
    agentltl translate COMMAND           the structured calls a shell command stands for
    agentltl tools [PATTERN]             tool names and arguments rules can refer to
    agentltl trace [--session ID]        what the guard recorded: this session, and the project
    agentltl reset [--session ID]        forget this session's trace
    agentltl reset --project             forget the project's trace
    agentltl library [NAME]              packaged rules you can switch on, or one in full
    agentltl use NAME... [--mode M]      switch packaged rules on (unuse: off)
    agentltl disable ID...               switch single rules off by id (enable: back on)

``use``/``unuse``/``disable``/``enable`` edit the project's AGENTLTL.yaml (created if
missing), or the user's (see :class:`agentltl_coding.Harness`) with ``--user``. A harness
adds its own commands with ``main(argv, extend=...)``.

A ``check`` step is a shell command (``"git push -f"``) or another tool as
``'Edit {"file_path": ".env"}'``. Prefix a step with what you expect (``deny: git push``,
``allow: pytest``) and ``check`` exits 1 when a step does not do that. Steps that are
allowed count as executed for the steps after them.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import store
from .harness import current as _harness
from .pattern import Paths
from .rules import (
    FILE_NAME,
    MODES,
    RuleFileError,
    RuleSet,
    find_rule_file,
    library,
    load,
    rule_files,
    user_rule_file,
)

_EXPECT = re.compile(r"^(allow|deny|ask|stop|note)\s*:\s*", re.I)
_TOOL_STEP = re.compile(r"^([A-Z][A-Za-z0-9_]*|mcp__[A-Za-z0-9_]+)\s+(\{.*\})\s*$", re.S)


def main(argv: Optional[List[str]] = None,
         extend: Optional[Callable[[Any, Dict[str, Callable]], None]] = None) -> int:
    """Run the ``agentltl`` command. *extend(subparsers, commands)* lets a harness add its
    own subcommands (``commands[name] = handler(args) -> int``)."""
    logging.getLogger("agentltl").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(prog="agentltl", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="compile the rule files and list the rules")
    p.add_argument("files", nargs="*", help="rule files (default: the ones that apply here)")

    p = sub.add_parser("check", help="replay steps through the rules")
    p.add_argument("steps", nargs="+", metavar="STEP")
    p.add_argument("--rules", action="append", default=[], metavar="FILE",
                   help="use only these rule files (repeatable)")
    p.add_argument("--add", action="append", default=[], metavar="FILE",
                   help="add these rule files (e.g. a draft) to the ones that apply here")
    p.add_argument("--auto", action="store_true", help="as if the agent ran in auto mode")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("translate", help="show the structured calls of a shell command")
    p.add_argument("command")

    p = sub.add_parser("tools", help="tool names and arguments that rules can refer to")
    p.add_argument("pattern", nargs="?", default="*",
                   help="glob on tool names, e.g. 'git_*' (default: all)")

    for name in ("trace", "reset"):
        p = sub.add_parser(name, help="show" if name == "trace" else "forget"
                           " what the guard recorded")
        p.add_argument("--session", help="session id (default: the latest one here)")
        if name == "reset":
            p.add_argument("--project", action="store_true",
                           help="forget the project memory instead of the session's")

    p = sub.add_parser("library", help="packaged rules you can switch on with `use`")
    p.add_argument("name", nargs="?", help="show this one in full")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    for name, what in (("use", "switch packaged rules on"), ("unuse", "switch packaged rules off"),
                       ("disable", "switch rules off by id"), ("enable", "undo `disable`")):
        p = sub.add_parser(name, help=what)
        p.add_argument("names", nargs="+", metavar="ID" if "able" in name else "NAME")
        p.add_argument("--user", action="store_true",
                       help=f"edit {_harness().user_file()} (every project) instead of this "
                            "project's")
        if name == "use":
            p.add_argument("--mode", choices=list(MODES), help="override the packs' modes")
            p.add_argument("--from", dest="origin", metavar="ID", action="append",
                           help="the memory statement this enforces")

    commands = dict(_COMMANDS)
    if extend is not None:
        extend(sub, commands)
    args = parser.parse_args(argv)
    try:
        return commands[args.cmd](args)
    except RuleFileError as exc:
        print("Rule file problems:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return 2


def _here() -> Tuple[str, str]:
    cwd = os.getcwd()
    return cwd, os.environ.get("CLAUDE_PROJECT_DIR") or _git_root(cwd) or cwd


def _git_root(path: str) -> Optional[str]:
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _ruleset(files: List[str], add: List[str] = ()) -> RuleSet:
    cwd, root = _here()
    files = list(files) or rule_files(cwd, root)
    files += [f for f in add if f not in files]
    return load(files, Paths(cwd, root))


# ── commands ──────────────────────────────────────────────────────────────────

def _validate(args: argparse.Namespace) -> int:
    ruleset = _ruleset(args.files)
    if not ruleset.files:
        print(f"No AGENTLTL.yaml here (nor {_harness().user_file()}). Nothing is enforced.")
        return 0
    print(f"OK: {len(ruleset.rules)} rule(s) from {', '.join(ruleset.files)}")
    for r in ruleset.rules:
        memory = ", project memory" if r.scope == "project" else ""
        came = "".join(f", from {o.get('file', o.get('id'))}" + (f":{o['line']}" if o.get("line") else "")
                       for o in r.origins)
        print(f"  {r.id} [{r.mode}{memory}] ({_origin(r.source)}{came}): {r.summary}")
        if r.why:
            print(f"      why: {r.why}")
    _warn(ruleset)
    s = ruleset.settings
    print(f"settings: default mode {s.mode}, default memory {s.scope}, retries {s.retries}, "
          "unparseable commands: "
          f"{s.unparseable_interactive} (interactive) / {s.unparseable_auto} (auto mode)")
    return 0


def _check(args: argparse.Namespace) -> int:
    from .guard import Guard

    cwd, root = _here()
    ruleset = _ruleset(args.rules, args.add)
    guard = Guard(ruleset, Paths(cwd, root))
    guard.restore({}, {})
    if not args.json:
        _warn(ruleset)
    rows, failed = [], False
    for i, raw in enumerate(args.steps, 1):
        expect = None
        m = _EXPECT.match(raw)
        if m:
            expect, raw = m.group(1).lower(), raw[m.end():]
        tool, tool_input = _step(raw)
        verdict = guard.decide(tool, tool_input, auto=args.auto)
        got = verdict.action
        if got == "none":
            got = "note" if verdict.context else "allow"
        if got in ("allow", "note"):
            guard.record(tool, tool_input, f"step{i}", "")
        ok = expect is None or expect == got or (expect == "allow" and got == "note")
        failed |= not ok
        rows.append({"step": i, "call": raw, "result": got, "expected": expect, "ok": ok,
                     "rule": verdict.rule, "calls": verdict.calls,
                     "reason": verdict.reason or verdict.context})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 1 if failed else 0
    for row in rows:
        mark = "" if row["expected"] is None else ("  ok" if row["ok"] else
                                                   f"  EXPECTED {row['expected'].upper()}")
        rule = f"  [{row['rule']}]" if row["rule"] else ""
        print(f"{row['step']:>2}. {row['result'].upper():<5} {row['call']}{rule}{mark}")
        names = [c["tool_name"] for c in row["calls"]]
        if names and names != [row["call"].split()[0]]:
            print(f"      calls: {', '.join(names)}")
        if row["result"] not in ("allow",) and row["reason"]:
            for line in row["reason"].splitlines():
                print(f"      | {line}")
    return 1 if failed else 0


def _origin(source: str) -> str:
    """Where a rule comes from, in the words `disable` / `unuse` / editing need."""
    if source.startswith("library:") or source == "built-in":
        return source
    if source == _harness().user_file():
        return f"user file {source}"
    return f"file {source}" if source else "?"


def _warn(ruleset: RuleSet) -> None:
    from .guard import lint
    for warning in lint(ruleset):
        print(f"WARNING {warning}")


def _step(raw: str) -> Tuple[str, Dict[str, Any]]:
    m = _TOOL_STEP.match(raw.strip())
    if m:
        try:
            return m.group(1), json.loads(m.group(2))
        except ValueError as exc:
            raise SystemExit(f"step {raw!r}: invalid JSON input: {exc}")
    return "Bash", {"command": raw}


def _translate(args: argparse.Namespace) -> int:
    from cli_to_tools import TranslationError

    from .guard import translator_for
    try:
        calls = translator_for(_ruleset([])).translate(args.command)
    except TranslationError as exc:
        print(f"Cannot translate: {exc}", file=sys.stderr)
        return 1
    for c in calls:
        flags = [k for k in ("conditional", "repeated") if c.meta.get(k)]
        extra = f"  ({', '.join(flags)})" if flags else ""
        print(f"{c.name} {json.dumps(c.args)}{extra}")
    return 0


def _tools(args: argparse.Namespace) -> int:
    from .guard import translator_for
    schemas = translator_for(_ruleset([])).registry.tool_schemas()
    shown = 0
    for s in schemas:
        if not fnmatch.fnmatchcase(s["name"], args.pattern):
            continue
        props = s.get("parameters", {}).get("properties", {})
        params = ", ".join(f"{k}: {v.get('type', 'string')}" for k, v in props.items())
        print(f"{s['name']}({params})")
        shown += 1
    if shown == 0:
        print(f"No tool matches {args.pattern!r}. Commands without a spec become a tool named "
              "after the executable with a single 'argv' list.")
    print("\nThe agent's own tools keep their names (Edit, Write, Read, WebFetch, mcp__...) "
          "with their input fields as arguments (Edit/Write: file_path). Shell output "
          "redirections appear as 'redirect_to'.")
    return 0


def _calls(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The recorded calls of a saved state (AgentLTL 0.2's, or the older ``trace``)."""
    return list(state.get("completed_tool_calls") or state.get("trace") or [])


def _session(args: argparse.Namespace) -> Optional[str]:
    sid = args.session or store.latest_session(_here()[1])
    if sid is None:
        print("No recorded session for this project.", file=sys.stderr)
    return sid


def _trace(args: argparse.Namespace) -> int:
    root = _here()[1]
    project = _calls(store.read_project(root))
    print(f"project {root}: {len(project)} recorded call(s), across sessions")
    sid = args.session or store.latest_session(root)
    if sid is None:
        print("No recorded session for this project.")
        return 0
    state = store.read(sid)
    trace = _calls(state)
    print(f"session {sid}: {len(trace)} recorded call(s)")
    for i, c in enumerate(trace[-50:], max(1, len(trace) - 49)):
        args_ = {k: v for k, v in (c.get("arguments") or {}).items()
                 if v not in (None, False, [], "")}
        print(f"  {i:>3}. {c.get('tool_name')} {json.dumps(args_)[:120]}")
    decisions = state.get("decisions") or []
    if decisions:
        print(f"\nlast {min(10, len(decisions))} intervention(s):")
        for d in decisions[-10:]:
            what = d["input"].get("command") or d["input"].get("file_path") or ""
            print(f"  {d['action'].upper():<5} [{d['rule'] or 'not checked'}] {d['tool']} {str(what)[:100]}")
    return 0


def _reset(args: argparse.Namespace) -> int:
    if args.project:
        root = _here()[1]
        store.reset_project(root)
        print(f"Project {root}: trace and counters cleared.")
        return 0
    sid = _session(args)
    if sid is None:
        return 1
    store.reset(sid)
    print(f"Session {sid}: trace and counters cleared.")
    return 0


def _library(args: argparse.Namespace) -> int:
    packs = library()
    try:
        used = set(_ruleset([]).used)
    except RuleFileError:
        used = set()
    if args.name:
        if args.name not in packs:
            print(f"No library rule {args.name!r}. `agentltl library` lists them.", file=sys.stderr)
            return 1
        with open(packs[args.name]["path"], encoding="utf-8") as fh:
            print(fh.read(), end="")
        return 0
    if args.json:
        print(json.dumps([{"name": n, "summary": p.get("summary", ""), "tags": p.get("tags", []),
                           "in_use": n in used,
                           "rules": [r.get("id") for r in p.get("rules") or []]}
                          for n, p in packs.items()], indent=2))
        return 0
    width = max(map(len, packs), default=0)
    for name, pack in packs.items():
        mark = "on " if name in used else "   "
        tags = ", ".join(pack.get("tags") or [])
        print(f"  {mark} {name:<{width}}  {pack.get('summary', '')}  [{tags}]")
    print("\nSwitch one on: agentltl use NAME   (--user for every project; --mode to override)")
    return 0


def _target_file(user: bool) -> str:
    if user:
        return user_rule_file() or _harness().user_file()
    cwd, root = _here()
    return find_rule_file(cwd, root) or os.path.join(root, FILE_NAME)


def _edit_list(args: argparse.Namespace) -> int:
    from .edit import read_list, write_list
    from .rules import _use_name

    path = _target_file(args.user)
    key = "use" if args.cmd in ("use", "unuse") else "disable"
    items = read_list(path, key)
    if args.cmd == "use":
        packs = library()
        unknown = [n for n in args.names if n not in packs]
        if unknown:
            import difflib
            for n in unknown:
                close = difflib.get_close_matches(n, packs, n=1)
                print(f"No library rule {n!r}" + (f" (did you mean {close[0]!r}?)" if close else ""),
                      file=sys.stderr)
            return 1
        items = [i for i in items if _use_name(i) not in args.names]
        extra = {k: v for k, v in (("mode", args.mode), ("from", _from(args.origin))) if v}
        items += [{n: dict(extra)} if extra else n for n in args.names]
    elif args.cmd == "unuse":
        items = [i for i in items if _use_name(i) not in args.names]
    elif args.cmd == "disable":
        items += [n for n in args.names if n not in items]
    else:
        missing = [n for n in args.names if n not in items]
        if missing:
            print(f"Not disabled in {path}: {', '.join(missing)}", file=sys.stderr)
        items = [i for i in items if i not in args.names]
    write_list(path, key, items)
    try:
        ruleset = load([path], Paths(*_here()))
    except RuleFileError as exc:
        print(f"{path} has problems now:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return 2
    shown = ", ".join(_use_name(i) for i in items) or "(none)"
    n = sum(r.source != "built-in" for r in ruleset.rules)
    print(f"{path}: {key} = {shown}; {n} rule(s) in this file now.")
    return 0


def _from(ids: Optional[List[str]]) -> Any:
    if not ids:
        return None
    return ids[0] if len(ids) == 1 else list(ids)


_COMMANDS = {"validate": _validate, "check": _check, "translate": _translate, "tools": _tools,
             "trace": _trace, "reset": _reset, "library": _library, "use": _edit_list,
             "unuse": _edit_list, "disable": _edit_list, "enable": _edit_list}

__all__ = ["main"]

if __name__ == "__main__":
    sys.exit(main())
