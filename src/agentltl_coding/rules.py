"""
agentltl_coding/rules.py – AGENTLTL.yaml → AgentLTL constraints.

    settings:
      mode: block               # default escalation for every rule
      retries: 3                # attempts allowed by mode: retry before asking you
      retry_counting: cumulative  # or consecutive (reset by an allowed call), hybrid
      report: 3                 # violations of the same mode one refusal may list
      unparseable: {interactive: ask, auto: note}
      announce: true            # list the rules to the agent at the start of a session
      scope: session            # default memory for rules: session | project
      scan_output: true         # warn when a call's output contains a credential
    rules:
      - id: tests-before-push
        before: {first: pytest, then: git_push, since: [Edit, Write]}
        why: CI is slow; run the tests locally first.
        fix: Run pytest, then push.
        mode: warn
        from: {file: CLAUDE.md, line: 12, id: 3f2a9c01d4}   # optional: the memory it came from
    use: [no-force-push, {tests-before-push: {mode: warn}}]   # packaged rules, see library/
    disable: [memory-first]     # switch off rules by id (from use:, the user file, or built in)
    tools:                      # cli-to-tools specs for your own commands
      deploy: {options: [{flags: [--prod], type: bool}]}

Rule kinds (exactly one per rule; targets are described in :mod:`agentltl_coding.pattern`),
each compiled to an AgentLTL formula over the calls up to the one being checked:

    never: T                  T is never called                  G ¬matches(T)
    before: [A, B]            B only once A has been called;     G(matches(B) → Y O matches(A))
                              dict form adds ``since: S``        ... Y(¬matches(S) S matches(A))
                              (an A must come after the last S)
    require: T                when one of T's tools is called,   G(T's tools → matches(T))
                              its arguments match T
    at_most: {call: T, times: n}                                 G(matches(T) → < n earlier)
    ltl: '...'                raw AgentLTL formula (``agentltl.parse`` syntax)
    formula: {type, args}     structured AgentLTL formula

Every kind but ``ltl``/``formula`` is past-only under ``G``, so AgentLTL judges it on the
call being checked alone: a rule broken earlier (by an override) never blocks unrelated
calls. A call whose files are only known at run time may match: that counts against it for
``never``/``before``'s ``then``/``at_most``, and does not satisfy ``require`` or ``before``'s
``first``.

``never``, ``require`` and ``at_most`` also take ``with:`` / ``where:`` at rule level.

``scope`` says which memory a rule reads: ``session`` (the calls made in this session;
starts empty with each new session, kept across compaction and resume) or ``project``
(every call made in this project, across sessions; never reset automatically).

Rules from the user's AGENTLTL.yaml (see :class:`agentltl_coding.Harness`) apply everywhere;
the project file adds to them and replaces a user rule with the same id. A harness may add
built-in rules, each switched off by a setting of its own (``memory_first: false``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from .harness import current as _harness
from .pattern import Paths, RuleError, parse_target, tool_matches

_MAYBE = (" Its file arguments are only known at run time (xargs, find -exec, or a $VARIABLE),"
          " so the guard cannot rule it out; name the files explicitly.")

FILE_NAME = "AGENTLTL.yaml"

# escalation mode -> AgentLTL ConstraintSeverity name, one to one
MODES: Dict[str, str] = {
    "block": "PERSISTENT_BLOCK",  # denied every time; the agent cannot override
    "warn": "BLOCK_AND_WARN",     # denied once; the agent may insist by repeating the exact call
    "retry": "SOFT_BLOCK",        # denied up to `retries` times, then you are asked
    "ask": "ASK",                 # you are asked every time
    "stop": "HARD_STOP",          # denied, and the agent stops until you reply
    "log": "TOLERATE",            # allowed; the agent is told it broke the rule
}
_MODE_HELP: Dict[str, str] = {
    "block": "denied every time; only you can let it through (edit the rule, or run it yourself)",
    "warn": "denied once; {agent} may override by repeating the exact same call",
    "retry": "denied, and after the allowed retries you are asked to approve",
    "ask": "you are asked to approve every time",
    "stop": "denied and {agent} stops working until you reply",
    "log": "allowed, but {agent} is told the rule was broken",
}


def mode_help(mode: str) -> str:
    return _MODE_HELP[mode].format(agent=_harness().agent)


# Strongest first. When one call breaks several rules, the strongest mode decides (AgentLTL's
# SEVERITY_STRENGTH); that also keeps a `warn` override (repeat the exact call) from slipping
# past a stronger rule the same call breaks.
STRENGTH = ("stop", "block", "ask", "retry", "warn", "log")
UNPARSEABLE = ("ask", "note", "allow", "deny")
SCOPES = ("session", "project")
KINDS = ("never", "before", "require", "at_most", "ltl", "formula")
_RULE_KEYS = {"id", "why", "fix", "mode", "scope", "with", "where", "from", *KINDS}
_FROM_KEYS = {"file", "line", "id", "text"}


@dataclass
class Settings:
    mode: str = "block"
    retries: int = 3
    unparseable_interactive: str = "ask"
    unparseable_auto: str = "note"
    announce: bool = True
    scope: str = "session"
    scan_output: bool = True
    retry_counting: str = "cumulative"   # cumulative | consecutive | hybrid
    report: int = 3                      # violations one refusal may list (nudge_max)
    builtins: Dict[str, bool] = field(default_factory=dict)   # harness built-in rule switches

    def builtin(self, name: str) -> bool:
        return self.builtins.get(name, True)

    @property
    def memory_first(self) -> bool:      # the Claude Code built-in's switch, by its old name
        return self.builtin("memory_first")


@dataclass
class Rule:
    id: str
    kind: str
    why: str
    fix: str
    mode: str
    formula: Any
    tools: Tuple[str, ...] = ()
    source: str = ""
    targets: Tuple[Any, ...] = ()
    scope: str = "session"
    origins: Tuple[Dict[str, Any], ...] = ()   # `from:` the memory statements it enforces
    label: str = ""
    # Why a call breaks the rule, in plain words, given the calls up to and including it
    # (None for ltl/formula rules, whose explanation is AgentLTL's own).
    explain: Optional[Callable[[List[Any]], Optional[str]]] = None

    @property
    def summary(self) -> str:
        """One line saying what the rule checks."""
        return self.label or str(self.formula)

    def constraint(self) -> Any:
        from agentltl import Constraint
        return Constraint(self.id, self.formula, description=self.why, repair=self.fix)


@dataclass
class RuleSet:
    rules: List[Rule] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    tool_specs: Dict[str, Any] = field(default_factory=dict)
    files: List[str] = field(default_factory=list)
    explicit_settings: bool = False
    disabled: List[str] = field(default_factory=list)   # ids switched off by `disable:`
    used: List[str] = field(default_factory=list)       # library packs named by `use:`

    def get(self, rule_id: str) -> Optional[Rule]:
        return next((r for r in self.rules if r.id == rule_id), None)

    def scoped(self, scope: Optional[str]) -> List[Rule]:
        return [r for r in self.rules if scope is None or r.scope == scope]

    def constraints(self, scope: Optional[str] = None) -> List[Any]:
        """Constraints of one scope (all when None), strongest mode first (see STRENGTH)."""
        ordered = sorted(self.scoped(scope), key=lambda r: STRENGTH.index(r.mode))
        return [r.constraint() for r in ordered]

    def severities(self, scope: Optional[str] = None) -> Dict[str, Any]:
        from agentltl import ConstraintSeverity
        return {r.id: ConstraintSeverity[MODES[r.mode]] for r in self.scoped(scope)}


class RuleFileError(Exception):
    """One or more problems in a rule file; ``problems`` lists them with locations."""

    def __init__(self, problems: List[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


# ── locating and loading ──────────────────────────────────────────────────────

def find_rule_file(start: str, stop: Optional[str] = None) -> Optional[str]:
    """The nearest AGENTLTL.yaml from *start* upwards, not above *stop* or the git root."""
    here = os.path.abspath(start or os.getcwd())
    stop = os.path.abspath(stop) if stop else None
    while True:
        candidate = os.path.join(here, FILE_NAME)
        if os.path.isfile(candidate):
            return candidate
        if here == stop or os.path.exists(os.path.join(here, ".git")):
            return None
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def user_rule_file() -> Optional[str]:
    path = _harness().user_file()
    return path if os.path.isfile(path) else None


def rule_files(cwd: str, project_dir: Optional[str] = None) -> List[str]:
    """User-level then project-level rule files that apply in *cwd*."""
    files = [f for f in (user_rule_file(), find_rule_file(cwd, project_dir)) if f]
    return list(dict.fromkeys(files))


def load(files: List[str], paths: Optional[Paths] = None) -> RuleSet:
    """Compile the rule files, later files overriding earlier ones.

    Raises:
        RuleFileError: With every problem found, each prefixed by ``file:line``.
    """
    merged = RuleSet(files=list(files))
    problems: List[str] = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            part = loads(text, paths, source=path)
        except RuleFileError as exc:
            problems += exc.problems
            continue
        except OSError as exc:
            problems.append(f"{path}: {exc}")
            continue
        by_id = {r.id: i for i, r in enumerate(merged.rules)}
        for rule in part.rules:
            if rule.id in by_id:
                merged.rules[by_id[rule.id]] = rule
            else:
                merged.rules.append(rule)
        merged.tool_specs.update(part.tool_specs)
        merged.rules = [r for r in merged.rules if r.id not in part.disabled]
        merged.disabled += part.disabled
        merged.used += part.used
        if part.explicit_settings:
            merged.settings = part.settings
    if problems:
        raise RuleFileError(problems)
    for setting, raw in _harness().builtins.items():
        if (merged.settings.builtin(setting) and not merged.get(raw["id"])
                and raw["id"] not in merged.disabled):
            builtin = compile_rule(raw, merged.settings, paths or Paths(), "built-in")
            builtin.source = "built-in"
            merged.rules.append(builtin)
    return merged


def loads(text: str, paths: Optional[Paths] = None, source: str = "<rules>") -> RuleSet:
    """Compile one rule file's text."""
    try:
        data = yaml.safe_load(text) or {}
        lines = _rule_lines(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{source}:{mark.line + 1}" if mark else source
        raise RuleFileError([f"{where}: invalid YAML: {getattr(exc, 'problem', exc)}"]) from None
    if not isinstance(data, dict):
        raise RuleFileError([f"{source}: expected a mapping with 'rules' (and optionally "
                             "'settings', 'tools')"])
    problems: List[str] = []
    unknown = set(data) - {"version", "settings", "rules", "tools", "use", "disable"}
    if unknown:
        problems.append(f"{source}: unknown top-level key(s) {sorted(unknown)}")
    settings = Settings()
    try:
        settings = _settings(data.get("settings"))
    except RuleError as exc:
        problems.append(f"{source}: settings: {exc}")

    tools = data.get("tools") or {}
    if not isinstance(tools, dict):
        problems.append(f"{source}: 'tools' must map command names to cli-to-tools specs")
        tools = {}
    else:
        problems += _check_tools(tools, source)

    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        raise RuleFileError(problems + [f"{source}: 'rules' must be a list"])
    rules: List[Rule] = []
    seen: Dict[str, str] = {}
    paths = paths or Paths()
    try:
        packed, packed_tools = _use(data.get("use"), settings, paths)
    except RuleError as exc:
        problems.append(f"{source}: use: {exc}")
        packed, packed_tools = [], {}
    tools = {**packed_tools, **tools}
    disabled = data.get("disable") or []
    if not isinstance(disabled, list) or not all(isinstance(d, str) for d in disabled):
        problems.append(f"{source}: disable: expected a list of rule ids")
        disabled = []
    for i, raw in enumerate(raw_rules):
        where = f"{source}:{lines[i]}" if i < len(lines) else f"{source}: rules[{i}]"
        try:
            rule = compile_rule(raw, settings, paths, where)
        except RuleError as exc:
            problems.append(f"{where}: {exc}")
            continue
        if rule.id in seen:
            problems.append(f"{where}: duplicate id '{rule.id}' (first at {seen[rule.id]})")
            continue
        seen[rule.id] = where
        rule.source = source
        rules.append(rule)
    if problems:
        raise RuleFileError(problems)
    own = {r.id for r in rules}       # a rule of the file replaces a library rule by id
    rules = [r for r in packed if r.id not in own] + rules
    rules = [r for r in rules if r.id not in disabled]
    return RuleSet(rules, settings, tools, [source], "settings" in data, list(disabled),
                   [_use_name(u) for u in data.get("use") or []])


def _use_name(item: Any) -> str:
    return item if isinstance(item, str) else next(iter(item))


# ── the rule library ──────────────────────────────────────────────────────────

LIBRARY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "library")


def library() -> Dict[str, Dict[str, Any]]:
    """The packaged rules, by name: each has ``summary``, ``tags``, ``rules``, ``tools``, and
    for a bundle, ``include`` (other entries it switches on)."""
    out: Dict[str, Dict[str, Any]] = {}
    if os.path.isdir(LIBRARY_DIR):
        for name in sorted(os.listdir(LIBRARY_DIR)):
            if name.endswith(".yaml"):
                with open(os.path.join(LIBRARY_DIR, name), encoding="utf-8") as fh:
                    pack = yaml.safe_load(fh) or {}
                pack["path"] = os.path.join(LIBRARY_DIR, name)
                out[name[:-5]] = pack
    return out


def _use(raw: Any, settings: Settings, paths: Paths) -> Tuple[List[Rule], Dict[str, Any]]:
    """Compile ``use: [name, {name: {mode: warn}}]`` into the library's rules."""
    if raw is None:
        return [], {}
    if not isinstance(raw, list):
        raise RuleError("expected a list of library rule names (see `agentltl library`)")
    packs = library()
    rules: List[Rule] = []
    tools: Dict[str, Any] = {}
    for item in raw:
        name, extra = (item, {}) if isinstance(item, str) else (
            next(iter(item.items())) if isinstance(item, dict) and len(item) == 1 else (None, None))
        if name is None or not isinstance(extra, dict) or set(extra) - {"mode", "scope", "from"}:
            raise RuleError(f"{item!r}: expected a name, or "
                            "{name: {mode: ..., scope: ..., from: ...}}")
        if name not in packs:
            import difflib
            close = difflib.get_close_matches(name, packs, n=1)
            hint = f" (did you mean '{close[0]}'?)" if close else ""
            raise RuleError(f"no library rule '{name}'{hint}; `agentltl library` lists them")
        for entry in _expand(name, packs):
            pack = packs[entry]
            for i, rule in enumerate(pack.get("rules") or []):
                if any(r.id == rule.get("id") for r in rules):
                    continue                  # already switched on by another entry or bundle
                compiled = compile_rule({**rule, **extra}, settings, paths,
                                        f"library:{entry}[{i}]")
                compiled.source = f"library:{entry}"
                rules.append(compiled)
            tools.update(pack.get("tools") or {})
    return rules, tools


def _expand(name: str, packs: Dict[str, Dict[str, Any]], seen: Tuple[str, ...] = ()) -> List[str]:
    """*name* and, for a bundle, the entries its ``include:`` lists (recursively)."""
    if name in seen:
        raise RuleError(f"library entry '{name}' includes itself")
    if name not in packs:
        raise RuleError(f"library entry '{seen[-1]}' includes unknown '{name}'")
    out = [name]
    for sub in packs[name].get("include") or []:
        out += [n for n in _expand(sub, packs, seen + (name,)) if n not in out]
    return out


def _rule_lines(text: str) -> List[int]:
    node = yaml.compose(text)
    if not isinstance(node, yaml.MappingNode):
        return []
    for key, value in node.value:
        if key.value == "rules" and isinstance(value, yaml.SequenceNode):
            return [item.start_mark.line + 1 for item in value.value]
    return []


def _settings(raw: Any) -> Settings:
    if raw is None:
        return Settings()
    if not isinstance(raw, dict):
        raise RuleError("expected a mapping")
    switches = set(_harness().builtins)
    unknown = set(raw) - {"mode", "retries", "unparseable", "announce", "scope", "scan_output",
                          "retry_counting", "report"} - switches
    if unknown:
        raise RuleError(f"unknown key(s) {sorted(unknown)}")
    s = Settings()
    for key in switches & set(raw):
        if not isinstance(raw[key], bool):
            raise RuleError(f"{key} must be true or false")
        s.builtins[key] = raw[key]
    if "retry_counting" in raw:
        if raw["retry_counting"] not in ("cumulative", "consecutive", "hybrid"):
            raise RuleError("retry_counting must be cumulative, consecutive or hybrid")
        s.retry_counting = raw["retry_counting"]
    if "report" in raw:
        if not isinstance(raw["report"], int) or raw["report"] < 1:
            raise RuleError("report must be a positive integer")
        s.report = raw["report"]
    if "mode" in raw:
        s.mode = _mode(raw["mode"])
    if "retries" in raw:
        if not isinstance(raw["retries"], int) or raw["retries"] < 1:
            raise RuleError("retries must be a positive integer")
        s.retries = raw["retries"]
    if "scope" in raw:
        s.scope = _scope(raw["scope"])
    if "announce" in raw:
        if not isinstance(raw["announce"], bool):
            raise RuleError("announce must be true or false")
        s.announce = raw["announce"]
    if "scan_output" in raw:
        if not isinstance(raw["scan_output"], bool):
            raise RuleError("scan_output must be true or false")
        s.scan_output = raw["scan_output"]
    unp = raw.get("unparseable")
    if isinstance(unp, str):
        unp = {"interactive": unp, "auto": unp}
    if unp is not None:
        if not isinstance(unp, dict) or set(unp) - {"interactive", "auto"}:
            raise RuleError("unparseable must be one of ask/note/allow/deny, or a mapping with "
                            "'interactive' and/or 'auto'")
        for key, value in unp.items():
            if value not in UNPARSEABLE:
                raise RuleError(f"unparseable.{key} must be one of {', '.join(UNPARSEABLE)}")
        s.unparseable_interactive = unp.get("interactive", s.unparseable_interactive)
        s.unparseable_auto = unp.get("auto", s.unparseable_auto)
    return s


def _scope(value: Any) -> str:
    if value not in SCOPES:
        raise RuleError(f"scope must be one of {', '.join(SCOPES)}, got {value!r}")
    return value


def _mode(value: Any) -> str:
    if value not in MODES:
        raise RuleError(f"mode must be one of {', '.join(MODES)}, got {value!r}")
    return value


def _check_tools(tools: Dict[str, Any], source: str) -> List[str]:
    try:
        from cli_to_tools import SpecRegistry
    except ImportError:
        return []
    try:
        SpecRegistry(packs=[]).load_dict(tools)
    except Exception as exc:  # the spec loader raises whatever argparse or YAML shapes give
        return [f"{source}: tools: {exc}"]
    return []


# ── compiling one rule ────────────────────────────────────────────────────────

def compile_rule(raw: Any, settings: Settings, paths: Paths, where: str = "rule") -> Rule:
    if not isinstance(raw, dict):
        raise RuleError("a rule must be a mapping with an id and one rule kind")
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise RuleError(f"unknown key(s) {sorted(unknown)}; rule kinds are {', '.join(KINDS)}")
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise RuleError("missing 'id'")
    kinds = [k for k in KINDS if k in raw]
    if len(kinds) != 1:
        raise RuleError(f"rule '{rule_id}' needs exactly one of {', '.join(KINDS)}"
                        + (f", has {', '.join(kinds)}" if kinds else ""))
    kind = kinds[0]
    if ("with" in raw or "where" in raw) and kind not in ("never", "require", "at_most"):
        raise RuleError(f"rule '{rule_id}': 'with'/'where' at rule level only apply to "
                        "never, require and at_most; put them on a target instead")
    mode = _mode(raw.get("mode", settings.mode))
    scope = _scope(raw.get("scope", settings.scope))
    why = str(raw.get("why") or "").strip()
    fix = str(raw.get("fix") or "").strip()
    origins = _origins(raw.get("from"), rule_id)
    formula, label, explain, tools, *targets = _BUILDERS[kind](raw, paths, f"{rule_id}.{kind}")
    if kind in ("ltl", "formula"):
        _require_runtime_safe(formula, rule_id)
    return Rule(rule_id, kind, why, fix, mode, formula, tools, where, tuple(targets), scope,
                origins, label, explain)


def _origins(raw: Any, rule_id: str) -> Tuple[Dict[str, Any], ...]:
    """``from:``: the memory statement(s) a rule was written from (see ``agentltl memory``).
    A statement id alone, a mapping with file/line/id/text, or a list of these."""
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for item in items:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict) or not item or set(item) - _FROM_KEYS:
            raise RuleError(f"rule '{rule_id}': 'from' takes a statement id, or a mapping with "
                            f"{', '.join(sorted(_FROM_KEYS))}")
        out.append(item)
    return tuple(out)


class _Pattern:
    """A rule target as an AgentLTL call pattern: ``match`` answers True, False, or None
    for "maybe" (files only known at run time), with paths resolved from *paths*."""

    def __init__(self, target: Any, paths: Paths) -> None:
        self.target, self.paths = target, paths
        self.tools = target.tools

    def match(self, name: str, args: Dict[str, Any]) -> Optional[bool]:
        return self.target.match(name, args, self.paths)

    def bind(self, bindings: Dict[str, Any]) -> "_Pattern":
        return _Pattern(_bound(self.target, bindings), self.paths)

    def describe(self) -> str:
        return self.target.describe()


class _AnyTool:
    """A call to one of *tools* (``*`` globs), whatever its arguments."""

    def __init__(self, tools: Tuple[str, ...]) -> None:
        self.tools = tools

    def match(self, name: str, args: Dict[str, Any]) -> bool:
        return tool_matches(name, self.tools)

    def describe(self) -> str:
        return " or ".join(self.tools)


def _bound(target: Any, bindings: Dict[str, Any]) -> Any:
    """*target* with its ``$variables`` replaced by values from *bindings*."""
    import dataclasses
    if hasattr(target, "targets"):
        return type(target)(tuple(_bound(t, bindings) for t in target.targets))
    with_ = dict(target.with_)
    variables = {}
    for arg, name in target.variables.items():
        if name in bindings:
            with_[arg] = bindings[name]
        else:
            variables[arg] = name
    return dataclasses.replace(target, with_=with_, variables=variables)


def _matches(target: Any, paths: Paths, maybe: bool) -> Any:
    from agentltl import Matches
    return Matches(_Pattern(target, paths), maybe=maybe)


def _always(body: Any) -> Any:
    """``G(body)``. With a past-only body each call is judged on its own: a rule broken
    earlier (by an override) never blocks unrelated calls."""
    from agentltl import Globally
    return Globally(body)


def _never(raw: Dict[str, Any], paths: Paths, where: str) -> Tuple[Any, ...]:
    """``G ¬matches(T)``; a possible match counts."""
    from agentltl import Not
    target = parse_target(raw["never"], where, with_=raw.get("with"), where_=raw.get("where"))

    def explain(calls: List[Any]) -> Optional[str]:
        c = calls[-1]
        hit = target.match(c.name, c.args, paths)
        if hit is True:
            return f"{c.name} is not allowed: it matches {target.describe_hit(c.name, c.args, paths)}."
        if hit is None:
            return f"{c.name} may match {target.describe_hit(c.name, c.args, paths)}." + _MAYBE
        return None

    formula = _always(Not(_matches(target, paths, maybe=True)))
    return formula, f"never {target.describe()}", explain, target.tools, target


def _before(raw: Dict[str, Any], paths: Paths, where: str) -> Tuple[Any, ...]:
    """``G(matches(B) → Y(¬matches(S) S matches(A)))``: every B has an A after the last S
    (``since``), or at all (``Y O matches(A)``). A possible B or S counts; a possible A does
    not."""
    from agentltl import Implies, Not, Once, Previous, Since
    spec = raw["before"]
    since = None
    if isinstance(spec, list) and len(spec) == 2:
        first, then = spec
    elif isinstance(spec, dict) and {"first", "then"} <= set(spec) and not (
            set(spec) - {"first", "then", "since"}):
        first, then = spec["first"], spec["then"]
        if spec.get("since") is not None:
            since = parse_target(spec["since"], f"{where}.since")
    else:
        raise RuleError(f"{where}: expected [first, then] or {{first, then, since}}")
    a = parse_target(first, f"{where}.first")
    b = parse_target(then, f"{where}.then")
    if _parts_with_variables(a) or _parts_with_variables(b):
        return _before_same_value(a, b, since, paths, where)

    def explain(calls: List[Any]) -> Optional[str]:
        c = calls[-1]
        if b.match(c.name, c.args, paths) is False:
            return None
        start = -1
        if since is not None:   # a call that may have been a `since` one resets, to be safe
            start = max((i for i, p in enumerate(calls[:-1])
                         if since.match(p.name, p.args, paths) is not False), default=-1)
        if any(a.matches(p.name, p.args, paths) for p in calls[start + 1:-1]):
            return None
        tail = ""
        if since is not None and start >= 0:
            tail = f" since the last {since.describe()} (call #{start + 1})"
        return f"{c.name} needs {a.describe()} to have run first{tail}."

    first_ok = _matches(a, paths, maybe=False)
    if since is None:
        earlier = Previous(Once(first_ok))
    else:
        earlier = Previous(Since(Not(_matches(since, paths, maybe=True)), first_ok))
    formula = _always(Implies(_matches(b, paths, maybe=True), earlier))
    label = f"{a.describe()} before {b.describe()}" + (f" since {since.describe()}" if since else "")
    return (formula, label, explain, a.tools + b.tools, a, b) + ((since,) if since else ())


def _parts(target: Any) -> List[Any]:
    return list(getattr(target, "targets", (target,)))


def _parts_with_variables(target: Any) -> List[Any]:
    return [t for t in _parts(target) if t.variables]


def _before_same_value(a: Any, b: Any, since: Any, paths: Paths, where: str) -> Tuple[Any, ...]:
    """``before`` whose two sides share ``$variables``:
    ``G(∀x ∈ values of the call here. Y O matches(first with x))``.

    A variable is implicitly universal: for every value x the call being judged has (as
    ``then``), an earlier ``first`` call had it too. Several variables nest one ForAll each:
    with single values (a chart and its version) the earlier call must have had that exact
    combination; with lists, every combination.
    """
    from functools import reduce

    from agentltl import ForAll, Once, Or, Previous

    if since is not None:
        raise RuleError(f"{where}: 'since' cannot be combined with $variables yet")
    names = sorted({v for t in _parts(a) + _parts(b) for v in t.variables.values()})
    if not names:
        raise RuleError(f"{where}: use a $variable on both 'first' and 'then'")
    for t in _parts(a) + _parts(b):
        missing = [n for n in names if n not in t.variables.values()]
        if missing:
            raise RuleError(f"{where}: every target must bind "
                            f"{', '.join('$' + n for n in missing)} ({t.describe()} does not)")
    for t in _parts(a):
        if t.where or t.exists is not None:
            raise RuleError(f"{where}.first: with a $variable, 'first' takes 'with' values only "
                            "(they are compared for equality)")

    def values_here(var: str, call: Any) -> List[Any]:
        values: List[Any] = []
        for t in _parts(b):
            if t.matches(call.name, call.args, paths):
                for arg, name in t.variables.items():
                    if name == var:
                        value = call.args.get(arg)
                        values += value if isinstance(value, list) else [value]
        return list(dict.fromkeys(x for x in values if x is not None))

    def domain_of(var: str) -> Callable[..., List[Any]]:
        def domain(trace: Any, metrics: Any, position: int) -> List[Any]:
            call = trace.at(position)
            return values_here(var, call) if call is not None else []
        return domain

    def explain(calls: List[Any]) -> Optional[str]:
        c = calls[-1]
        missing = []
        for combo in _combinations({v: values_here(v, c) for v in names}):
            wanted = [_bound(t, combo) for t in _parts(a)]
            if not any(w.matches(p.name, p.args, paths) for p in calls[:-1] for w in wanted):
                missing.append(combo)
        if not missing:
            return None
        combo = missing[0]
        bound = ", ".join(f"{k}={v!r}" for k, v in combo.items())
        wanted = []
        for t in _parts(a):
            values = {**t.with_, **{arg: combo.get(name) for arg, name in t.variables.items()}}
            shown = ", ".join(f"{k}={v!r}" for k, v in values.items())
            for tool in t.tools:
                if not any(tool_matches(p.name, (tool,)) for p in calls):
                    wanted.append(f"{tool} was never called")
                else:
                    wanted.append(f"no earlier {tool} call had {shown}")
        return f"for {bound}: {' or '.join(wanted)}."

    body = reduce(Or, [Previous(Once(_matches(t, paths, maybe=False))) for t in _parts(a)])
    for var in reversed(names):
        body = ForAll(var, domain_of(var), body, description=f"values of ${var} in {b.describe()}")
    label = f"{a.describe()} before {b.describe()}"
    return _always(body), label, explain, a.tools + b.tools, a, b


def _combinations(values: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    import itertools
    names = list(values)
    return [dict(zip(names, combo)) for combo in itertools.product(*(values[n] for n in names))]


def _require(raw: Dict[str, Any], paths: Paths, where: str) -> Tuple[Any, ...]:
    """``G(called one of T's tools → matches(T))``; a possible match does not satisfy it."""
    from agentltl import Implies, Matches
    target = parse_target(raw["require"], where, with_=raw.get("with"), where_=raw.get("where"))
    for part in getattr(target, "targets", (target,)):
        if not part.with_ and not part.where:
            raise RuleError(f"{where}: require needs 'with' or 'where' on every target "
                            f"(what the arguments of {part.describe()} must be)")

    def explain(calls: List[Any]) -> Optional[str]:
        c = calls[-1]
        if not tool_matches(c.name, target.tools):
            return None
        hit = target.match(c.name, c.args, paths)
        if hit is None:
            return f"{c.name} must be called as {target.describe()}, which cannot be checked." + _MAYBE
        if hit is False:
            return f"{c.name} must be called as {target.describe()}."
        return None

    formula = _always(Implies(Matches(_AnyTool(target.tools)), _matches(target, paths, maybe=False)))
    return formula, f"require {target.describe()}", explain, target.tools, target


def _at_most(raw: Dict[str, Any], paths: Paths, where: str) -> Tuple[Any, ...]:
    """``G(matches(T) → fewer than n earlier calls matched T)``."""
    from agentltl import CountBefore, Implies
    spec = raw["at_most"]
    if not isinstance(spec, dict) or set(spec) != {"call", "times"}:
        raise RuleError(f"{where}: expected {{call: <target>, times: <n>}}")
    times = spec["times"]
    if not isinstance(times, int) or times < 0:
        raise RuleError(f"{where}.times: expected a non-negative integer")
    target = parse_target(spec["call"], f"{where}.call", with_=raw.get("with"),
                          where_=raw.get("where"))

    def explain(calls: List[Any]) -> Optional[str]:
        c = calls[-1]
        if target.match(c.name, c.args, paths) is False:
            return None
        n = sum(1 for p in calls[:-1] if target.matches(p.name, p.args, paths))
        if n >= times:
            return f"{target.describe()} may run at most {times} time(s); it already ran {n}."
        return None

    formula = _always(Implies(_matches(target, paths, maybe=True),
                              CountBefore(_matches(target, paths, maybe=False), times, "<")))
    return formula, f"at most {times} x {target.describe()}", explain, target.tools, target


def _ltl(raw: Dict[str, Any], paths: Paths, where: str) -> Tuple[Any, ...]:
    from agentltl import parse
    text = raw["ltl"]
    if not isinstance(text, str):
        raise RuleError(f"{where}: expected a formula string")
    try:
        formula = parse(text)
    except Exception as exc:
        raise RuleError(f"{where}: cannot parse formula: {exc}") from None
    return formula, str(formula), None, tuple(sorted(_tools_in(formula)))


def _formula(raw: Dict[str, Any], paths: Paths, where: str) -> Tuple[Any, ...]:
    formula = build_formula(raw["formula"], where)
    return formula, str(formula), None, tuple(sorted(_tools_in(formula)))


_BUILDERS = {"never": _never, "before": _before, "require": _require, "at_most": _at_most,
             "ltl": _ltl, "formula": _formula}


def build_formula(spec: Any, where: str = "formula") -> Any:
    """``{type: Before, args: {a: x, b: y}}`` → ``Before("x", "y")``, recursively."""
    import dataclasses

    import agentltl

    if isinstance(spec, list):
        return [build_formula(s, where) for s in spec]
    if not (isinstance(spec, dict) and "type" in spec):
        return spec
    cls = getattr(agentltl, str(spec["type"]), None)
    if not (isinstance(cls, type) and issubclass(cls, agentltl.Formula)) or cls is agentltl.Predicate:
        raise RuleError(f"{where}: unknown formula type {spec['type']!r}")
    args = spec.get("args") or {}
    if not isinstance(args, dict):
        raise RuleError(f"{where}: 'args' must be a mapping")
    fields = {f.name for f in dataclasses.fields(cls)}
    unknown = set(args) - fields
    if unknown:
        raise RuleError(f"{where}: {cls.__name__} has no argument(s) {sorted(unknown)}; "
                        f"expected {sorted(fields)}")
    try:
        return cls(**{k: build_formula(v, f"{where}.{k}") for k, v in args.items()})
    except TypeError as exc:
        raise RuleError(f"{where}: {exc}") from None


def _tools_in(formula: Any) -> set:
    import dataclasses
    out: set = set()
    if isinstance(formula, (list, tuple)):
        for f in formula:
            out |= _tools_in(f)
        return out
    if not dataclasses.is_dataclass(formula):
        return out
    for f in dataclasses.fields(formula):
        value = getattr(formula, f.name)
        if f.name in ("tool", "a", "b", "tool_a", "tool_b", "target", "tools") and value:
            out |= {value} if isinstance(value, str) else {v for v in value if isinstance(v, str)}
        else:
            out |= _tools_in(value)
    return out


def _require_runtime_safe(formula: Any, rule_id: str) -> None:
    """Reject formulas a check made call by call can't judge, as AgentLTL classifies them:
    one that fails until some call happens (UNSAFE) would refuse every call before it, and
    one that can't fail before the session ends (INERT) would never refuse anything."""
    from agentltl.runtime_safety import RuntimeSafety, classify_runtime_safety
    safety = classify_runtime_safety(formula).safety
    if safety == RuntimeSafety.UNSAFE:
        raise RuleError(
            f"rule '{rule_id}': this formula fails until some call happens (called(\"x\") is "
            "false until x runs), so every call before that would be refused. Say when it "
            "applies, e.g. `G(now(\"deploy\") -> called(\"pytest\"))`, or use a rule kind such "
            "as `before: {first: pytest, then: git_push}`.")
    if safety == RuntimeSafety.INERT:
        raise RuleError(
            f"rule '{rule_id}': this is a liveness property: it can't fail before the session "
            "ends (F, in_order: what it asks for may still happen), so it would never refuse a "
            "call. Bound it, e.g. `within_steps(\"deploy\", \"notify\", 3)`, or say what must "
            "not happen first, e.g. `before(\"pytest\", \"git_push\")`.")
