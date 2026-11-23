"""git_updater and cloner talk to the git CLI directly (no GitPython)."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import FunctionNode  # noqa: E402,F401  (models import must come first)
from app import cloner, git_updater  # noqa: E402


def git(repo, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_changed_files_since_lists_code_changes_and_both_sides_of_a_rename(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "keep.py").write_text("y = 1\n")
    (tmp_path / "notes.md").write_text("hi\n")
    git(tmp_path, "init", "-q"); git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "one")
    first = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "mv", "a.py", "b.py")
    (tmp_path / "keep.py").write_text("y = 2\n")
    (tmp_path / "notes.md").write_text("changed\n")
    git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "two")
    head = git(tmp_path, "rev-parse", "HEAD")

    files, got = git_updater.changed_files_since(str(tmp_path), first)
    assert got == head and files == ["a.py", "b.py", "keep.py"]            # rename gives both; .md is not code
    assert git_updater.changed_files_since(str(tmp_path), None) == ([], head)
    assert git_updater.changed_files_since(str(tmp_path), head) == ([], head)
    assert git_updater.changed_files_since(str(tmp_path), "0" * 40) == ([], head)   # unknown commit: no-op
    plain = tmp_path / "plain"; plain.mkdir()
    assert git_updater.changed_files_since(str(tmp_path.parent / "nowhere"), first) == ([], None)


def test_clone_failures_are_reported_not_raised_as_git_errors(monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(128, a[0], stderr="fatal: repository not found\n")
    monkeypatch.setattr(cloner.subprocess, "run", boom)
    with pytest.raises(cloner.InvalidRepoUrl, match="repository not found"):
        with cloner.clone_to_temp("https://github.com/o/r"):
            pass

    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(cloner.subprocess, "run", no_git)
    with pytest.raises(RuntimeError, match="git is not installed"):
        with cloner.clone_to_temp("https://github.com/o/r"):
            pass
