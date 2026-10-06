"""
agentltl_coding/store.py – per-session state on disk.

Each hook call is a fresh process, so traces and enforcer counters live on disk: the
session memory in ``<state dir>/<session id>.json``, the project memory (every call made in
a project, across sessions) in ``<state dir>/../projects/<hash of the project dir>.json``. A harness may run tool calls in parallel, so every
read-modify-write holds an exclusive lock on ``<file>.lock``.

State dir: ``$AGENTLTL_STATE`` (or ``$AGENTLTL_CC_STATE``), else the harness's ``state_dir``,
else ``sessions/`` next to the virtualenv the guard runs in (``<data>/venv-<pins>`` →
``<data>/sessions``, so a hook and the ``agentltl`` CLI agree without sharing an
environment), else ``~/.cache/agentltl-<harness>/sessions``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional


def state_dir() -> str:
    from .harness import current
    for var in ("AGENTLTL_STATE", "AGENTLTL_CC_STATE"):
        if os.environ.get(var):
            return os.environ[var]
    if current().state_dir:
        return os.path.expanduser(current().state_dir)
    if re.fullmatch(r"venv(-\d+)?", os.path.basename(sys.prefix)):
        return os.path.join(os.path.dirname(sys.prefix), "sessions")
    return os.path.join(os.path.expanduser("~"), ".cache", f"agentltl-{current().name}", "sessions")


def session_path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "default")[:128]
    return os.path.join(state_dir(), f"{safe}.json")


def project_path(project_dir: str) -> str:
    key = hashlib.sha1(os.path.abspath(project_dir).encode()).hexdigest()[:16]
    return os.path.join(os.path.dirname(state_dir()), "projects", f"{key}.json")


def _read(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def read(session_id: str) -> Dict[str, Any]:
    return _read(session_path(session_id))


def read_project(project_dir: str) -> Dict[str, Any]:
    return _read(project_path(project_dir))


@contextmanager
def _locked(path: str) -> Iterator[Dict[str, Any]]:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _read(path)
        yield state
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, default=str)
        os.replace(tmp, path)


@contextmanager
def locked(session_id: str) -> Iterator[Dict[str, Any]]:
    """Yield the session state for update; whatever it holds on exit is written back."""
    with _locked(session_path(session_id)) as state:
        yield state


@contextmanager
def locked_project(project_dir: str) -> Iterator[Dict[str, Any]]:
    """Like :func:`locked`, for the project memory. Take it after the session lock."""
    with _locked(project_path(project_dir)) as state:
        state["project_dir"] = os.path.abspath(project_dir)
        yield state


def latest_session(cwd: Optional[str] = None) -> Optional[str]:
    """The most recently updated session, optionally only one whose project contains *cwd*."""
    folder = state_dir()
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".json")]
    except OSError:
        return None
    names.sort(key=lambda n: os.path.getmtime(os.path.join(folder, n)), reverse=True)
    for name in names:
        sid = name[:-5]
        project = read(sid).get("project_dir")
        if cwd is None or (project and (cwd == project or cwd.startswith(project + os.sep))):
            return sid
    return None


def reset(session_id: str) -> None:
    with locked(session_id) as state:
        keep = {k: state[k] for k in ("project_dir",) if k in state}
        state.clear()
        state.update(keep)


def reset_project(project_dir: str) -> None:
    with locked_project(project_dir) as state:
        state.clear()


__all__: List[str] = ["state_dir", "session_path", "project_path", "read", "read_project",
                       "locked", "locked_project", "latest_session", "reset", "reset_project"]
