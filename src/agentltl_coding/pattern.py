"""
agentltl_coding/pattern.py – which calls a rule is talking about.

A target names one or more tools and optionally narrows them by argument:

    git_push                                  # any git push
    [Edit, Write]                             # either tool
    {tool: git_push, with: {force: true}}     # exact argument values
    {tool: [Edit, Write], where: {file_path: "**/.env"}}   # glob patterns

``with`` compares values for equality; when the call's value is a list, an expected scalar
must be one of its elements (``rm a b`` matches ``with: {paths: a}``). ``where`` matches
string values against glob patterns (``*`` also crosses ``/``); a path is tried as written,
resolved against the working directory, relative to the project root, and, for a pattern
without ``/``, by its basename. The key ``"*"`` stands for any argument:

    {tool: [Edit, Write, rm, mv, cat], where: {"*": "**/.env"}}

``exists: true`` (or ``false``) additionally requires a value that ``where`` matched to be a
path that exists (or does not) at the moment of the check, so "never modify an existing
migration" does not also catch creating a new one:

    {tool: Write, where: {file_path: "migrations/*"}, exists: true}

``succeeded: true`` (or ``false``) requires a call that ran to have succeeded (or failed):
a shell command's exit status, as the harness recorded it. A call that has not run yet may
or may not succeed, so for it the answer is "maybe":

    before: {first: {tool: pytest, succeeded: true}, then: git_push}

A ``with`` value written ``$name`` is a variable, not a literal: ``before`` uses it to tie
two calls together (``{tool: Read, with: {file_path: $f}}`` before
``{tool: Edit, with: {file_path: $f}}`` means "the same file").

Path arguments (``file_path``, ``paths``, ``redirect_to``, ...) are made absolute before
rules see them, so ``cat a.py`` and ``Read /proj/a.py`` name the same file.

A list of targets matches when any of them does:

    [{tool: Write, where: {file_path: "*.lock"}}, {tool: rm, where: {paths: "*.lock"}}]
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cli_to_tools import PATH_NAMES, UNKNOWN_PATHS, Paths, normalize_paths
from cli_to_tools._effects import REDIRECT_KEYS as _REDIRECT_KEYS
from cli_to_tools._effects import absolute as _absolute
from cli_to_tools._effects import strings as _strings
from cli_to_tools._effects import unknown_path_keys as _unknown_path_keys


class RuleError(ValueError):
    """A rule file entry that cannot be compiled."""


# Arguments holding file paths (cli-to-tools' default names, the harness tools' file_path,
# and the redirections). A call whose file arguments are only known at run time carries
# UNKNOWN_PATHS: True when any path argument may be (`xargs rm`), else the list of those that
# hold one (`rm $UNSET` gives ["paths"]). A condition on such an argument is answered "maybe".
PATH_KEYS = tuple(dict.fromkeys(PATH_NAMES + _REDIRECT_KEYS))


@dataclass
class Target:
    tools: Tuple[str, ...]
    with_: Dict[str, Any] = field(default_factory=dict)
    where: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    exists: Optional[bool] = None
    variables: Dict[str, str] = field(default_factory=dict)   # argument -> variable name
    succeeded: Optional[bool] = None

    def matches(self, name: str, args: Optional[Dict[str, Any]], paths: Paths,
                call: Any = None) -> bool:
        return self.match(name, args, paths, call) is True

    def match(self, name: str, args: Optional[Dict[str, Any]], paths: Paths,
              call: Any = None) -> Optional[bool]:
        """True, False, or None for "maybe": a path condition on a call whose file
        arguments are only known at run time (see UNKNOWN_PATHS), or a ``succeeded``
        condition on a call that has not run. *call* is the recorded call, if it ran."""
        if not tool_matches(name, self.tools):
            return False
        if self.succeeded is not None:
            ran = _status(call)
            if ran is None:
                return None if self.match_args(name, args, paths) is not False else False
            if (ran == 0) != self.succeeded:
                return False
        return self.match_args(name, args, paths)

    def match_args(self, name: str, args: Optional[Dict[str, Any]], paths: Paths) -> Optional[bool]:
        """The argument conditions alone (``with``, ``where``, ``exists``)."""
        args = args or {}
        unknown = args.get(UNKNOWN_PATHS) or []
        maybe = False
        for key, expected in self.with_.items():
            actual = args.get(key)
            if not _equal(expected, actual) and not (
                    key in PATH_KEYS and isinstance(expected, str)
                    and _equal(_absolute(expected, paths), actual)):
                if _unknown(unknown, key):
                    maybe = True
                    continue
                return False
        hits: List[str] = []
        for key, patterns in self.where.items():
            values = _strings(list(args.values()) if key == "*" else args.get(key))
            found = [v for v in values if any(_glob(v, p, paths) for p in patterns)]
            if not found:
                if _unknown(unknown, key):
                    maybe = True
                    continue
                return False
            hits += found
        if self.exists is not None:
            if not self.where:   # no glob narrowed it: judge the path arguments themselves
                hits = [v for key in PATH_KEYS for v in _strings(args.get(key))]
            if not any(os.path.exists(_absolute(v, paths)) == self.exists for v in hits):
                if not unknown:
                    return False
                maybe = True
        return None if maybe else True

    def describe_hit(self, name: str, args: Optional[Dict[str, Any]], paths: Paths) -> str:
        return self.describe()

    def describe(self) -> str:
        tools = " or ".join(self.tools)
        parts = [f"{k}={v!r}" for k, v in self.with_.items()]
        parts += [f"{k}=${v}" for k, v in self.variables.items()]
        parts += [f"{k} matching {' or '.join(v)}" for k, v in self.where.items()]
        if self.exists is not None:
            parts.append("existing path" if self.exists else "new path")
        if self.succeeded is not None:
            parts.append("succeeded" if self.succeeded else "failed")
        return f"{tools} ({', '.join(parts)})" if parts else tools


@dataclass
class AnyTarget:
    """Several targets; a call matches when it matches any of them."""

    targets: Tuple[Target, ...]

    @property
    def tools(self) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(t for target in self.targets for t in target.tools))

    def matches(self, name: str, args: Optional[Dict[str, Any]], paths: Paths,
                call: Any = None) -> bool:
        return self.match(name, args, paths, call) is True

    def match(self, name: str, args: Optional[Dict[str, Any]], paths: Paths,
              call: Any = None) -> Optional[bool]:
        results = [t.match(name, args, paths, call) for t in self.targets]
        if True in results:
            return True
        return None if None in results else False

    def describe(self) -> str:
        return " or ".join(t.describe() for t in self.targets)

    def describe_hit(self, name: str, args: Optional[Dict[str, Any]], paths: Paths) -> str:
        """The alternatives a call matches (or may match), not every one."""
        hits = [t for t in self.targets if t.match(name, args, paths) is not False]
        return " or ".join(t.describe() for t in hits) or self.describe()


def parse_target(spec: Any, where: str, *, with_: Any = None, where_: Any = None) -> Any:
    """Build a target from its YAML form; ``with_``/``where_`` are rule-level extras."""
    if isinstance(spec, list) and any(isinstance(s, dict) for s in spec):
        return AnyTarget(tuple(parse_target(s, f"{where}[{i}]", with_=with_, where_=where_)
                               for i, s in enumerate(spec)))
    if isinstance(spec, dict):
        unknown = set(spec) - {"tool", "with", "where", "exists", "succeeded"}
        if unknown:
            raise RuleError(f"{where}: unknown key(s) {sorted(unknown)} "
                            "(expected tool, with, where, exists, succeeded)")
        exists = spec.get("exists")
        if exists is not None and not isinstance(exists, bool):
            raise RuleError(f"{where}.exists: must be true or false")
        succeeded = spec.get("succeeded")
        if succeeded is not None and not isinstance(succeeded, bool):
            raise RuleError(f"{where}.succeeded: must be true or false")
        if "tool" not in spec:
            raise RuleError(f"{where}: a target needs 'tool'")
        tools = _names(spec["tool"], where)
        w = {**_mapping(spec.get("with"), f"{where}.with"), **_mapping(with_, "with")}
        g = {**_patterns(spec.get("where"), f"{where}.where"), **_patterns(where_, "where")}
        return _with_variables(Target(tools, w, g, exists, succeeded=succeeded))
    return _with_variables(Target(_names(spec, where), _mapping(with_, "with"),
                                  _patterns(where_, "where")))


def _status(call: Any) -> Optional[int]:
    """The exit status of a call that ran: 0 when recorded without one (calls were only
    recorded once they succeeded before statuses were); None for a call not run yet."""
    raw = getattr(call, "raw", None)
    if not isinstance(raw, dict) or ("result" not in raw and "status" not in raw):
        return None
    return int(raw.get("status") or 0)


def tool_matches(name: str, tools: Tuple[str, ...]) -> bool:
    """Whether *name* is one of *tools*; a name with ``*`` is a glob (``kubectl_*``)."""
    return name in tools or any("*" in t and fnmatch.fnmatchcase(name, t) for t in tools)


def _with_variables(target: Target) -> Target:
    """Move ``$name`` values out of ``with`` into ``variables``."""
    for key, value in list(target.with_.items()):
        if isinstance(value, str) and value.startswith("$") and value[1:].isidentifier():
            target.variables[key] = value[1:]
            del target.with_[key]
    return target


# Redirections are written out in the command line itself: `xargs` or `find -exec` can add
# arguments to a command, never a redirection to it (see _unknown).


def _unknown(unknown: Any, key: str) -> bool:
    """Whether argument *key* may hold a path only known at run time (see UNKNOWN_PATHS)."""
    if unknown is True:
        return key == "*" or (key in PATH_KEYS and key not in _REDIRECT_KEYS)
    return bool(unknown) and (key == "*" or key in unknown)


def _names(spec: Any, where: str) -> Tuple[str, ...]:
    names = [spec] if isinstance(spec, str) else spec
    if not isinstance(names, (list, tuple)) or not names or not all(
            isinstance(n, str) and n for n in names):
        raise RuleError(f"{where}: expected a tool name or a list of tool names, got {spec!r}")
    return tuple(names)


def _mapping(spec: Any, where: str) -> Dict[str, Any]:
    if spec is None:
        return {}
    if not isinstance(spec, dict):
        raise RuleError(f"{where}: expected a mapping of argument name to value")
    return dict(spec)


def _patterns(spec: Any, where: str) -> Dict[str, Tuple[str, ...]]:
    out: Dict[str, Tuple[str, ...]] = {}
    for key, value in _mapping(spec, where).items():
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise RuleError(f"{where}.{key}: expected a glob pattern or a list of them")
        out[key] = tuple(values)
    return out


def _equal(expected: Any, actual: Any) -> bool:
    if isinstance(actual, list) and not isinstance(expected, list):
        return any(_equal(expected, a) for a in actual)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(actual) == bool(expected) if actual is not None else expected is False
    if isinstance(expected, (int, float)) and isinstance(actual, str):
        return actual.strip() == str(expected)
    return expected == actual


def _glob(value: str, pattern: str, paths: Paths) -> bool:
    for candidate in _forms(value, paths, basename="/" not in pattern):
        if fnmatch.fnmatchcase(candidate, pattern):
            return True
    return False


def _forms(value: str, paths: Paths, basename: bool) -> Sequence[str]:
    forms = [value]
    if value and not value.startswith(("-", "http://", "https://")):
        absolute = _absolute(value, paths)
        forms.append(absolute)
        if paths.root and (absolute == paths.root or absolute.startswith(paths.root + os.sep)):
            forms.append(os.path.relpath(absolute, paths.root))
        if basename:
            forms.append(os.path.basename(absolute))
    return forms


def unknown_path_keys(args: Dict[str, Any]) -> List[str]:
    """The path arguments holding a value only known at run time."""
    return _unknown_path_keys(args, PATH_KEYS)
