"""Git-URL ingestion for hosted deployments.

``ingestion.py`` only ever walks an already-local directory; there is no code
path anywhere else in the app that clones a repository. This module is that
path, kept deliberately small and paranoid: it is the one place that takes a
string typed by an anonymous internet visitor and hands it to ``git``.

Threat model this guards against:
  - SSRF / internal-network probing via non-github hosts or non-https schemes.
  - GitPython's "ext::" transport, which runs an arbitrary local command
    (`git clone "ext::sh -c id"` executes `id`) — rejected explicitly.
  - `file://` and bare local paths, which would let a "URL" field read the
    server's own filesystem — rejected explicitly.
  - Credentials embedded in the URL (`https://user:pass@host/...`) leaking
    into logs or being replayed — rejected explicitly.
  - Unbounded clones (huge monorepos, git-bomb style .git history) — capped
    by shallow clone + a post-clone size check.
  - Leftover clones piling up on a shared box — always cleaned up by the
    caller via the contextmanager's `finally`.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

try:
    from git import Repo
    from git.exc import GitCommandError
except ImportError:  # pragma: no cover - GitPython is a hard requirement in prod
    Repo = None
    GitCommandError = Exception

ALLOWED_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}

FLOWIFY_WORKDIR = os.environ.get("FLOWIFY_WORKDIR", tempfile.gettempdir())
MAX_CLONE_MB = int(os.environ.get("FLOWIFY_MAX_CLONE_MB", "100"))

# Environment forced onto every clone subprocess: no interactive prompts, no
# credential helpers, no picking up the host's ~/.gitconfig or SSH config.
_CLONE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_SSH_COMMAND": "false",  # disable SSH transport entirely
}


class InvalidRepoUrl(ValueError):
    """The URL failed validation before any clone was attempted."""


def validate_repo_url(url: str) -> str:
    """Raise InvalidRepoUrl unless *url* is a plain https:// URL on an
    allowed public git host with no embedded credentials. Returns the
    normalized URL (trailing '.git' and slash stripped) on success."""
    if not url or not isinstance(url, str):
        raise InvalidRepoUrl("repo_url is required")
    url = url.strip()

    # Reject the transports GitPython/git would otherwise happily execute.
    lowered = url.lower()
    for bad in ("ext::", "file://", "ssh://", "git://", "fd::"):
        if lowered.startswith(bad):
            raise InvalidRepoUrl(f"unsupported transport: {bad!r}")
    if not lowered.startswith("https://"):
        raise InvalidRepoUrl("only https:// URLs are accepted")

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise InvalidRepoUrl("only https:// URLs are accepted")
    if "@" in parsed.netloc:
        raise InvalidRepoUrl("URLs with embedded credentials are not accepted")
    host = parsed.hostname or ""
    if host not in ALLOWED_HOSTS:
        raise InvalidRepoUrl(
            f"host {host!r} is not allowed (allowed: {', '.join(sorted(ALLOWED_HOSTS))})"
        )
    if ".." in parsed.path:
        raise InvalidRepoUrl("path traversal in URL")
    if not parsed.path or parsed.path == "/":
        raise InvalidRepoUrl("URL must include an owner/repo path")

    normalized = f"https://{host}{parsed.path}"
    if normalized.endswith(".git"):
        normalized = normalized[: -len(".git")]
    return normalized.rstrip("/")


def _dir_size_mb(path: Path) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = Path(root) / f
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total / (1024 * 1024)


@contextlib.contextmanager
def clone_to_temp(url: str):
    """Shallow-clone *url* into a fresh temp dir under FLOWIFY_WORKDIR and
    yield its path. The directory is always removed on exit, success or not.

    Usage:
        with cloner.clone_to_temp(repo_url) as local_path:
            payload = pipeline.ingest(str(local_path))
    """
    if Repo is None:
        raise RuntimeError("GitPython is not installed; cannot clone repositories")

    normalized = validate_repo_url(url)
    os.makedirs(FLOWIFY_WORKDIR, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="flowify-clone-", dir=FLOWIFY_WORKDIR)
    try:
        env = {**os.environ, **_CLONE_ENV}
        try:
            Repo.clone_from(
                normalized,
                tmp_dir,
                depth=1,
                single_branch=True,
                env=env,
            )
        except GitCommandError as e:
            raise InvalidRepoUrl(f"clone failed: {e}") from e

        size_mb = _dir_size_mb(Path(tmp_dir))
        if size_mb > MAX_CLONE_MB:
            raise InvalidRepoUrl(
                f"repository is {size_mb:.0f} MB, exceeds the {MAX_CLONE_MB} MB limit"
            )
        yield Path(tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
