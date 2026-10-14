"""What only differs on Windows: backslash paths against `/` globs, and the session lock."""

import errno
import os
import time

import pytest

from agentltl_coding.pattern import Paths, parse_target

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows paths and msvcrt locking")


@windows_only
@pytest.mark.parametrize("glob,value,expected", [
    ("src/*.py", "a.py", True),                       # project-relative glob, relative argument
    ("src/*.py", "C:\\proj\\src\\a.py", True),        # ... and an absolute backslash argument
    ("src/*.py", "..\\a.py", False),
    ("*.lock", "C:\\proj\\sub\\poetry.lock", True),   # basename glob
    ("**/.env", "C:\\proj\\.env", True),
    ("**/.env", "C:\\proj\\.environment", False),
])
def test_globs_match_backslash_paths(glob, value, expected):
    paths = Paths("C:\\proj\\src", "C:\\proj")
    target = parse_target({"tool": "rm", "where": {"paths": glob}}, "t")
    assert target.matches("rm", {"paths": [value]}, paths) is expected


@windows_only
def test_the_session_lock_surfaces_an_error_that_cannot_recover(tmp_path, monkeypatch):
    import msvcrt

    from agentltl_coding import store

    def invalid(*args):
        raise OSError(errno.EINVAL, "invalid lock")

    monkeypatch.setattr(msvcrt, "locking", invalid)
    start = time.monotonic()
    with open(tmp_path / "x.lock", "w") as fh, pytest.raises(OSError):
        store._lock_exclusive(fh)
    assert time.monotonic() - start < 5          # not the 60 seconds of waiting for a holder


@windows_only
def test_the_session_lock_waits_while_another_process_holds_it(tmp_path, monkeypatch):
    import msvcrt

    from agentltl_coding import store

    calls = []

    def busy_twice(fd, mode, nbytes):
        calls.append(1)
        if len(calls) < 3:
            raise OSError(errno.EACCES, "locked")

    monkeypatch.setattr(msvcrt, "locking", busy_twice)
    with open(tmp_path / "x.lock", "w") as fh:
        store._lock_exclusive(fh)
    assert len(calls) == 3
