"""
agentltl_coding/memory.py – find the statements in an agent's memory that could be rules.

Coding agents keep instructions in markdown: ``CLAUDE.md`` and auto memory for Claude Code,
``.github/copilot-instructions.md`` and ``AGENTS.md`` for Copilot. The harness says which
files apply (:attr:`agentltl_coding.Harness.memory`). :func:`scan` splits them into
statements (a bullet, a paragraph, a table row, or one auto-memory file), gives each a stable
id, and adds what code can tell for sure: the modal words it uses, and the commands it quotes,
translated into the calls a rule would name. Deciding which statements are rules is left to
the agent (the plugin's ``import`` skill); this module only makes that repeatable:

- ``id``: a hash of the statement's text, so it survives edits elsewhere in the file;
- ``status``: ``covered`` (a rule in force has ``from: <id>``), ``declined`` (the user said
  no, see :func:`decline`) or ``new``;
- ``target``: the rule file a rule from it belongs in, the project's or the user's.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml

from . import store
from .harness import current as _harness

PROJECT, USER = "project", "user"
_SKIP_DIRS = {".git", "node_modules", "vendor", "third_party", ".venv", "venv", "dist", "build",
              "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", "target", "site-packages"}
_MAX_DEPTH = 6
_MAX_CONTEXT = 1500

# A statement is a *candidate* rule when it is phrased as an instruction (a modal word, and an
# imperative opening, capitals, or a "you"/"Claude") about an action a tool call performs.
_STRONG = re.compile(r"\b(never|always|must(?: not)?|do not|don't|avoid|prefer|ask (?:me|the user|"
                     r"first|before)|instead of|first|only|stop)\b", re.I)
_ACTION = re.compile(r"\b(commit|push|pull|merge|rebase|install|uninstall|delet\w*|remov\w*|rm|"
                     r"edit\w*|writ\w*|modif\w*|run\w*|execut\w*|deploy\w*|apply|read|kill|restart|"
                     r"launch|overwrit\w*|upload|publish|tag|reset|force|sudo|chmod|clone|call\w*|"
                     r"invoke|pip|npm|docker|git|kubectl|terraform|helm|curl)\b", re.I)
_IMPERATIVE = re.compile(r"^(never|always|do not|don't|avoid|prefer|use|run|edit|ask|keep|make|"
                         r"write|check|read|commit|push|stop|only|before|after|when|if|no)\b", re.I)
_ADDRESSED = re.compile(r"\b(you|claude|copilot|codex|we|i)\b", re.I)
_MODAL = re.compile(r"\b(never|always|only|must(?: not)?|do not|don't|avoid|prefer(?:s|red)?|"
                    r"ask(?: me)?(?: first| before)?|before|after|unless|except|instead of)\b", re.I)
_EMPHASIS = re.compile(r"\b(NEVER|ALWAYS|ONLY|MUST|DO NOT|DON'T|NOT)\b")
_SPAN = re.compile(r"`([^`\n]+)`")
_COMMAND_WORD = re.compile(r"^[a-z][a-z0-9_.+-]*$")
_BULLET = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")


@dataclass
class Source:
    path: str
    kind: str          # claude-md | rules | auto-memory | agents-md | instructions
    target: str        # PROJECT or USER


@dataclass
class Statement:
    id: str
    file: str
    line: int
    text: str
    section: str = ""
    context: str = ""  # an auto-memory file's body
    code: str = ""     # the code blocks that follow the statement
    modality: List[str] = field(default_factory=list)
    emphasis: bool = False
    commands: List[Dict[str, Any]] = field(default_factory=list)
    target: str = PROJECT
    candidate: bool = False
    status: str = "new"
    rules: List[str] = field(default_factory=list)


# ── where memory lives ────────────────────────────────────────────────────────

def sources(root: str, user_only: bool = False, home: Optional[str] = None) -> List[Source]:
    """The memory files that apply in project *root*, as the harness finds them, each once.
    With *user_only*, only the user's own."""
    find = _harness().memory
    if find is None:
        return []
    seen, unique = set(), []
    for s in find(root, user_only, home or os.path.expanduser("~")):
        real = os.path.realpath(s.path)
        if os.path.isfile(s.path) and real not in seen:
            seen.add(real)
            unique.append(s)
    return unique


def files(folder: str, pattern: str) -> List[str]:
    """Files under *folder* whose name matches *pattern*, in a stable order."""
    found = []
    for base, dirs, names in os.walk(folder):
        dirs.sort()
        found += [os.path.join(base, n) for n in sorted(names) if fnmatch.fnmatch(n, pattern)]
    return found


def nested(root: str, name: str) -> List[str]:
    """Files called *name* below *root* (not *root*'s own), outside dependencies and
    submodules."""
    submodules = set()
    gitmodules = os.path.join(root, ".gitmodules")
    if os.path.isfile(gitmodules):
        with open(gitmodules, encoding="utf-8", errors="replace") as fh:
            submodules = {os.path.normpath(os.path.join(root, m)) for m in
                          re.findall(r"^\s*path\s*=\s*(.+?)\s*$", fh.read(), re.M)}
    found = []
    for base, dirs, names in os.walk(root):
        depth = os.path.relpath(base, root).count(os.sep)
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")
                         and os.path.join(base, d) not in submodules and depth < _MAX_DEPTH)
        if base != root and name in names:
            found.append(os.path.join(base, name))
    return found


# ── splitting into statements ─────────────────────────────────────────────────

def statement_id(text: str) -> str:
    norm = re.sub(r"[\s*_`>#|]+", " ", text).strip().lower()
    return hashlib.sha1(norm.encode()).hexdigest()[:10]


def split(text: str, path: str = "", kind: str = "claude-md") -> List[Statement]:
    """The statements of one memory file, in order."""
    if kind == "auto-memory":
        one = _auto_memory_statement(text, path)
        if one:
            return [one]
    out: List[Statement] = []
    for line, body, section, code in _blocks(text):
        if len(re.findall(r"\w+", body)) < 3 and not _SPAN.search(body):
            continue
        out.append(Statement(statement_id(body), path, line, body, section, code=code))
    return out


def _auto_memory_statement(text: str, path: str) -> Optional[Statement]:
    """An auto-memory file is one statement: its ``description``, with the body as context."""
    m = re.match(r"---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    description = str(meta.get("description") or meta.get("name") or "").strip()
    if not description:
        return None
    body = text[m.end():].strip()
    kind = (meta.get("metadata") or {}).get("type") if isinstance(meta.get("metadata"), dict) \
        else meta.get("type")
    code = "\n".join(re.findall(r"^(?:`{3,}|~{3,})[^\n]*\n(.*?)^(?:`{3,}|~{3,})", body, re.S | re.M))
    return Statement(statement_id(description), path, 1, description,
                     section=str(kind or ""), context=body[:_MAX_CONTEXT], code=code)


def _blocks(text: str) -> Iterator[Tuple[int, str, str, str]]:
    """(line, text, heading path, code) for each bullet, paragraph or table row.

    A code block belongs to the statement just before it in the same section. A code block
    with no such statement is a statement of its own: its text is the code.
    """
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":           # frontmatter of a rules file
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            lines = [""] * (end + 1) + lines[end + 1:]
    headings: List[str] = []
    out: List[List[Any]] = []                          # [line, [text lines], section, [code]]
    open_ = False                                      # is out[-1] still being written?
    fence: Optional[str] = None
    fence_line, code = 0, []                           # type: int, List[str]

    for n, raw in enumerate(lines, 1):
        stripped = raw.strip()
        section = " > ".join(headings)
        if fence is not None:
            if not stripped.startswith(fence):
                code.append(raw)
                continue
            fence = None
            block = "\n".join(code)
            if out and out[-1][2] == section and block.strip():
                out[-1][3].append(block)
            elif block.strip():
                out.append([fence_line, [block], section, [block]])
            open_ = False
            continue
        m = re.match(r"(`{3,}|~{3,})", stripped)
        if m:
            fence, fence_line, code = m.group(1), n, []
            continue
        if not stripped or stripped.startswith("<!--"):
            open_ = False
            continue
        h = re.match(r"(#{1,6})\s+(.*)", stripped)
        if h:
            open_ = False
            headings = headings[:len(h.group(1)) - 1] + [h.group(2).strip("# ")]
            continue
        if stripped.startswith("|"):
            open_ = False
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue                                    # the separator row
            if n < len(lines) and re.match(r"\s*\|[\s:|-]+\|\s*$", lines[n]):
                continue                                    # the header row
            out.append([n, [" | ".join(c for c in cells if c)], section, []])
            continue
        b = _BULLET.match(raw)
        if b and not b.group(1):
            out.append([n, [raw[b.end():]], section, []])
            open_ = True
        elif open_:
            out[-1][1].append(raw[b.end():] if b else stripped)
        else:
            out.append([n, [stripped], section, []])
            open_ = True
    for line, parts, section, codes in out:
        text_ = parts[0] if codes and parts == codes[:1] else " ".join(x.strip() for x in parts)
        yield line, text_.strip(), section, "\n".join(codes)


# ── hints ─────────────────────────────────────────────────────────────────────

def commands_in(statement: Statement, translator: Any = None) -> List[Dict[str, Any]]:
    """The commands *statement* quotes (inline code, and lines of the code that follows it),
    each with the calls it translates to. ``needs_spec`` marks a call with no spec (only
    ``argv``): a rule can't name its options until a spec is added under ``tools:``."""
    candidates = [s.strip() for s in _SPAN.findall(statement.text + "\n" + statement.context)]
    for line in statement.code.splitlines()[:40]:
        line = re.sub(r"^\$\s+", "", line.strip())
        if line and not line.startswith("#"):
            candidates.append(line)
    out: List[Dict[str, Any]] = []
    seen, shapes = set(), set()
    for text in candidates:
        first = text.split()[0] if text.split() else ""
        if text in seen or not _COMMAND_WORD.match(first) or len(out) >= 8:
            continue
        if not (_known(first, translator) or ("." not in first and shutil.which(first))):
            continue                               # a name, a script or a word, not a command
        if " " not in text and not _known(first, translator):
            continue
        seen.add(text)
        entry: Dict[str, Any] = {"text": text}
        if translator is not None:
            try:
                calls = translator.translate(text)
            except Exception:                      # not shell, or not a whole command
                continue
            shape = tuple(c.name for c in calls)
            if shape in shapes and text not in statement.text:
                continue                           # the code block's 5th docker_compose_run
            shapes.add(shape)
            entry["calls"] = [{"name": c.name, "args": _shown(c.args)} for c in calls]
            entry["needs_spec"] = sorted({c.name for c in calls
                                          if _no_spec(c.args) and not _has_spec(c.name, translator)})
        out.append(entry)
    return out


def _known(word: str, translator: Any) -> bool:
    registry = getattr(translator, "registry", None)
    return bool(registry is not None and registry.get(word))


def _has_spec(name: str, translator: Any) -> bool:
    cache = getattr(translator, "_agentltl_tool_names", None)
    if cache is None:
        registry = getattr(translator, "registry", None)
        cache = {t["name"] for t in registry.tool_schemas()} if registry is not None else set()
        try:
            translator._agentltl_tool_names = cache
        except AttributeError:
            pass
    return name in cache


def _no_spec(args: Dict[str, Any]) -> bool:
    keys = {k for k in args if k not in ("redirect_to", "overwrite_to", "redirect_from",
                                         "unknown_paths")}
    return keys == {"argv"}


def _shown(args: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in args.items() if v not in (None, False, [], "", {})}


def shown_path(path: str, root: str, home: Optional[str] = None) -> str:
    """*path* relative to the project, or from ``~``: how ``from:`` names a file."""
    path, root = os.path.abspath(path), os.path.abspath(root)
    home = os.path.abspath(home or os.path.expanduser("~"))
    if path.startswith(root + os.sep) and root != home:
        return os.path.relpath(path, root)
    if path.startswith(home + os.sep):
        return "~/" + os.path.relpath(path, home)
    return path


def is_candidate(st: Statement) -> bool:
    """Whether *st* reads like an instruction about tool calls: worth the agent's judgment."""
    if st.file and "/memory/" in st.file and st.context:
        return True                         # an auto-memory file is one deliberate memory
    text = re.sub(r"[*_`]", "", st.text).strip()
    if not _STRONG.search(text) or not (st.commands or _ACTION.search(text)):
        return False
    return bool(_IMPERATIVE.search(text) or st.emphasis or _ADDRESSED.search(text))


# ── the scan ──────────────────────────────────────────────────────────────────

def scan(root: str, ruleset: Any = None, translator: Any = None, user_only: bool = False,
         files: Optional[List[str]] = None, home: Optional[str] = None) -> Dict[str, Any]:
    """Every statement of the memory that applies in *root*, with its hints and status."""
    if files:
        found = [Source(os.path.abspath(f), "auto-memory" if "/memory/" in f else "claude-md",
                        PROJECT) for f in files]
    else:
        found = sources(root, user_only, home)
    covered: Dict[str, List[str]] = {}
    for rule in getattr(ruleset, "rules", None) or []:
        for origin in getattr(rule, "origins", ()) or ():
            if origin.get("id"):
                covered.setdefault(origin["id"], []).append(rule.id)
    declined = set(declined_ids(root))
    statements: List[Statement] = []
    for src in found:
        try:
            with open(src.path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for st in split(text, shown_path(src.path, root, home), src.kind):
            st.target = src.target
            st.modality = sorted({m.lower() for m in _MODAL.findall(st.text)})
            st.emphasis = bool(_EMPHASIS.search(st.text))
            st.commands = commands_in(st, translator)
            st.candidate = is_candidate(st)
            if st.id in covered:
                st.status, st.rules = "covered", covered[st.id]
            elif st.id in declined:
                st.status = "declined"
            statements.append(st)
    counts = {k: sum(s.status == k for s in statements) for k in ("new", "covered", "declined")}
    counts["candidates"] = sum(s.candidate and s.status == "new" for s in statements)
    counts["statements"] = len(statements)
    return {"sources": [asdict(s) for s in found],
            "statements": [asdict(s) for s in statements],
            "counts": counts}


# ── remembering a "no" ────────────────────────────────────────────────────────

def declined_ids(root: str) -> List[str]:
    return list(store.read_project(root).get("memory_declined") or [])


def decline(root: str, ids: List[str]) -> List[str]:
    with store.locked_project(root) as state:
        have = list(state.get("memory_declined") or [])
        have += [i for i in ids if i not in have]
        state["memory_declined"] = have
    return have


def forget(root: str, ids: List[str]) -> List[str]:
    with store.locked_project(root) as state:
        have = [i for i in state.get("memory_declined") or [] if i not in ids]
        state["memory_declined"] = have
    return have


__all__ = ["Source", "Statement", "sources", "files", "nested", "split", "scan", "statement_id",
           "commands_in", "is_candidate", "shown_path", "decline", "forget", "declined_ids",
           "PROJECT", "USER"]
