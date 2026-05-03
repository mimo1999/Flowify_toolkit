"""Phase 6: incremental updates via git diff."""
from __future__ import annotations
from pathlib import Path
from typing import List, Set

from .ingestion import detect_language

try:
    from git import Repo, InvalidGitRepositoryError
except ImportError:  # pragma: no cover
    Repo = None
    InvalidGitRepositoryError = Exception


def changed_files_since(repo_path: str, last_commit: str | None) -> tuple[List[str], str | None]:
    """Return (changed file paths relative to repo, current_head_sha).

    If last_commit is None, returns ([], head) so the caller treats the snapshot as fresh.
    Falls back to no-op if git is unavailable.
    """
    if Repo is None:
        return [], None
    try:
        repo = Repo(repo_path)
    except InvalidGitRepositoryError:
        return [], None
    head = repo.head.commit.hexsha
    if last_commit is None or last_commit == head:
        return [], head
    try:
        diff = repo.commit(last_commit).diff(repo.commit(head))
    except Exception:
        return [], head
    files: Set[str] = set()
    for d in diff:
        for p in (d.a_path, d.b_path):
            if p and detect_language(Path(p)):
                files.add(p)
    return sorted(files), head
