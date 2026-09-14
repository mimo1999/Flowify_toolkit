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


def estimate_source_size(repo_path: str) -> tuple[int, int]:
    """Return (total_bytes, file_count) across parseable source files, without
    parsing any of them. Measured at ~0.13s for a 4,785-file repo — cheap
    enough to run before build_function_graph spends real memory/CPU on a
    repo that's going to be rejected anyway."""
    total_bytes = 0
    file_count = 0
    for fp in iter_source_files(repo_path):
        try:
            total_bytes += fp.stat().st_size
        except OSError:
            continue
        file_count += 1
    return total_bytes, file_count
