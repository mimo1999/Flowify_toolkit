"""Phase 1: Repository ingestion — walk the repo, collect Python files."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Iterator, List

SUPPORTED_EXTENSIONS_BY_LANGUAGE = {
    "python": {".py"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx"},
    "java": {".java"},
    "c": {".c", ".h"},
    "cpp": {".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"},
}
SUPPORTED_EXTENSIONS = {
    ext
    for extensions in SUPPORTED_EXTENSIONS_BY_LANGUAGE.values()
    for ext in extensions
}
IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", "dist", "build", ".mypy_cache"}


def detect_language(file_path: Path) -> str | None:
    suffix = file_path.suffix.lower()
    for language, extensions in SUPPORTED_EXTENSIONS_BY_LANGUAGE.items():
        if suffix in extensions:
            return language
    return None


def iter_repo_files(repo_path: str, extensions: set[str]) -> Iterator[Path]:
    """Walk *repo_path*, skipping IGNORED_DIRS and dotdirs, yielding files whose
    suffix (case-insensitive) is in *extensions*. Shared by any caller that
    needs a filtered repo walk (source files, documentation files, ...)."""
    root = Path(repo_path).resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in extensions:
                yield p


def iter_source_files(repo_path: str) -> Iterator[Path]:
    return iter_repo_files(repo_path, SUPPORTED_EXTENSIONS)


def list_source_files(repo_path: str) -> List[Path]:
    return list(iter_source_files(repo_path))
