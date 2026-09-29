"""Shared ingest-limit exceptions.

Lives outside pipeline.py so graph_builder (which pipeline imports) can also
raise these without a circular import. All are ValueError subclasses so
existing `except ValueError` handlers (e.g. agent_export's callers) keep
working without changes.
"""
from __future__ import annotations


class RepoLimitError(ValueError):
    """Base for every ingest-limit rejection on a memory/CPU-constrained deployment."""


class RepoSourceTooLargeError(RepoLimitError):
    """Raised by the pre-parse fast-fail guard, before any file is parsed."""

    def __init__(self, total_bytes: int, max_bytes: int, file_count: int):
        self.total_bytes = total_bytes
        self.max_bytes = max_bytes
        self.file_count = file_count
        mb = total_bytes / 1e6
        limit_mb = max_bytes / 1e6
        super().__init__(
            f"repo source is {mb:.0f} MB across {file_count} files, exceeding the "
            f"{limit_mb:.0f} MB limit for this deployment — ingest it locally instead "
            f"(see the README's Deployment section) or run against a smaller subset of the repo"
        )


class RepoTooLargeError(RepoLimitError):
    """Raised mid-parse once the running node count exceeds the caller's max_nodes."""

    def __init__(self, node_count: int, max_nodes: int):
        self.node_count = node_count
        self.max_nodes = max_nodes
        super().__init__(
            f"repo has {node_count}+ parseable nodes, exceeding the {max_nodes}-node limit "
            f"for this deployment — ingest it locally instead (see the README's Deployment "
            f"section) or run against a smaller subset of the repo"
        )


class RepoTooSlowError(RepoLimitError):
    """Raised mid-parse once wall-clock time exceeds the caller's max_seconds.

    Catches dense-but-small repos the byte/node guards miss — CPU is the
    binding constraint on a 0.1 CPU instance well before memory is.
    """

    def __init__(self, elapsed: float, max_seconds: float):
        self.elapsed = elapsed
        self.max_seconds = max_seconds
        super().__init__(
            f"repo parsing exceeded the {max_seconds:.0f}s time budget for this deployment "
            f"(still running at {elapsed:.0f}s) — ingest it locally instead (see the README's "
            f"Deployment section) or run against a smaller subset of the repo"
        )
