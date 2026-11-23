"""Incident memory: files that past reverts and hotfixes touched, from local git history only."""
from __future__ import annotations

import re
import subprocess
from functools import lru_cache

_INCIDENT = re.compile(r"\b(revert(ed|s)?|hot-?fix(es)?|roll(ed)?[ -]?back|incident|post-?mortem)\b", re.I)
_SEP, _FS = "\x1e", "\x1f"


@lru_cache(maxsize=8)
def incidents(repo: str, head: str, months: int = 12) -> dict:
    """{path: {"count": n, "latest": [sha, subject]}} for commits in the last `months` whose subject
    says revert, hotfix, rollback, incident or postmortem. `head` only keys the cache.
    ponytail: file-level and subject-based. A revert touches the files of the commit it reverts,
    so those count too without a second lookup. Per-function history (`git log -L`) is too slow."""
    r = subprocess.run(["git", "log", f"--since={months}.months", "--no-renames", "--relative",
                        f"--format={_SEP}%h{_FS}%s", "--name-only"],
                       cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return {}
    out: dict = {}
    for rec in r.stdout.split(_SEP)[1:]:
        head_line, _, files = rec.partition("\n")
        sha, _, subject = head_line.partition(_FS)
        if not _INCIDENT.search(subject):
            continue
        for f in filter(None, (x.strip() for x in files.splitlines())):
            e = out.setdefault(f, {"count": 0, "latest": [sha, subject[:80]]})  # git log is newest first
            e["count"] += 1
    return out
