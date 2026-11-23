"""Phase 6: incremental updates via git diff."""
from __future__ import annotations
import subprocess
from pathlib import Path
from typing import List, Set

from .ingestion import detect_language


def _git(repo_path: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True).stdout


def changed_files_since(repo_path: str, last_commit: str | None) -> tuple[List[str], str | None]:
    """Return (changed file paths relative to repo, current_head_sha).

    If last_commit is None, returns ([], head) so the caller treats the snapshot as fresh.
    Falls back to no-op if git is unavailable.
    """
    try:
        head = _git(repo_path, "rev-parse", "HEAD").strip()
    except (subprocess.CalledProcessError, OSError):  # not a repo, no commits yet, or no git
        return [], None
    if last_commit is None or last_commit == head:
        return [], head
    try:
        # --no-renames lists both the old and the new path of a rename; -z keeps odd names unquoted
        out = _git(repo_path, "diff", "--name-only", "--no-renames", "--relative", "-z", last_commit, head)
    except (subprocess.CalledProcessError, OSError):  # last_commit no longer exists, e.g. after a force-push
        return [], head
    files: Set[str] = {p for p in out.split("\0") if p and detect_language(Path(p))}
    return sorted(files), head
