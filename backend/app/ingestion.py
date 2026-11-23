"""Phase 1: Repository ingestion — walk the repo, collect Python files."""
from __future__ import annotations
import os
import re
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

# Directories that dominate node counts without describing the product: on a
# real repo tests alone are ~40% of all functions. Skipped by default on a
# hosted instance; FLOWIFY_EXCLUDE_DIRS (comma-separated names, empty = none)
# overrides in any mode. "tests" also skips test_*.py / *_test.py / conftest.py.
DEFAULT_EXCLUDE_DIRS = {
    "tests", "test", "__tests__", "spec",
    "benchmarks", "benchmark", "examples", "example", "docs", "doc", "scripts",
}
_TEST_FILE = re.compile(r"^(test_.*|.*_test)\.\w+$|^conftest\.py$")


def excluded_dirs() -> set[str]:
    raw = os.environ.get("FLOWIFY_EXCLUDE_DIRS")
    if raw is None:
        server = os.environ.get("FLOWIFY_MODE", "local").lower().strip() == "server"
        return set(DEFAULT_EXCLUDE_DIRS) if server else set()
    return {d.strip() for d in raw.split(",") if d.strip()}


def detect_language(file_path: Path) -> str | None:
    suffix = file_path.suffix.lower()
    for language, extensions in SUPPORTED_EXTENSIONS_BY_LANGUAGE.items():
        if suffix in extensions:
            return language
    return None


def iter_repo_files(repo_path: str, extensions: set[str], skip_dirs: set[str] = frozenset()) -> Iterator[Path]:
    """Walk *repo_path*, skipping IGNORED_DIRS and dotdirs, yielding files whose
    suffix (case-insensitive) is in *extensions*. Shared by any caller that
    needs a filtered repo walk (source files, documentation files, ...)."""
    root = Path(repo_path).resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in extensions:
                yield p


def iter_source_files(repo_path: str) -> Iterator[Path]:
    skip = excluded_dirs()
    for p in iter_repo_files(repo_path, SUPPORTED_EXTENSIONS, skip_dirs=skip):
        if "tests" in skip and _TEST_FILE.match(p.name):
            continue
        yield p


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
