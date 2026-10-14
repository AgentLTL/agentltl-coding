"""`from:` names a file with / on every platform: it is written into a shared rule file."""

from agentltl_coding.memory import shown_path


def test_a_project_file_is_shown_relative_with_slashes(tmp_path):
    root, home = tmp_path / "proj", tmp_path / "home"
    (root / "sub").mkdir(parents=True)
    f = root / "sub" / "CLAUDE.md"
    f.write_text("x")
    assert shown_path(str(f), str(root), str(home)) == "sub/CLAUDE.md"


def test_a_home_file_is_shown_from_the_tilde_with_slashes(tmp_path):
    root, home = tmp_path / "proj", tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    root.mkdir()
    f = home / ".claude" / "CLAUDE.md"
    f.write_text("x")
    assert shown_path(str(f), str(root), str(home)) == "~/.claude/CLAUDE.md"
