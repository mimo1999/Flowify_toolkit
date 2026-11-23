"""The reviewer's queue and the author's checks. Local checkouts only.

  branches / triage      branches ahead of the base, scored from the loaded (base) graph without
                         checking anything out: an estimate, labelled as one
  open_worktree / remove Flowify-managed `git worktree`s for a full review, at most MAX_WORKTREES,
                         least recently used evicted; nothing is left behind on remove
  run_tests              the pytest tests that reach a chunk, run with a list argv (no shell)
  read_notes / write_notes  `.flowify/review.json`, the author's notes and test results for a reviewer
  parts                  groups of changed functions that share nothing: "this could be N PRs"
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from app import change_risk, pr_impact

MAX_WORKTREES = 5
NOTES_PATH = ".flowify/review.json"
_ID = re.compile(r"^[\w./-]+\.py(::[\w\[\]-]+)+$")
_SEP = "\x1f"


def _git(repo, *args, check=False) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode:
        raise subprocess.CalledProcessError(r.returncode, ["git", *args], r.stdout, r.stderr)
    return r.stdout if r.returncode == 0 else ""


# ── queue ──────────────────────────────────────────────────────────────────────────────────────

def branches(repo: str, base: str, days: int = 30) -> list:
    """Local and remote branches with commits not in `base`, touched in the last `days`, newest first."""
    out = _git(repo, "for-each-ref", "--sort=-committerdate", "refs/heads", "refs/remotes",
               f"--format=%(refname:short){_SEP}%(objectname:short){_SEP}%(committerdate:unix){_SEP}%(authorname){_SEP}%(subject)")
    base_sha = _git(repo, "rev-parse", "--short", base).strip()
    cutoff, seen, rows = time.time() - days * 86400, set(), []
    for line in out.splitlines():
        ref, sha, when, author, subject = (line.split(_SEP) + [""] * 5)[:5]
        if not ref or ref.endswith("/HEAD") or ref == base or sha == base_sha or sha in seen or int(when or 0) < cutoff:
            continue
        ahead = int((_git(repo, "rev-list", "--count", f"{base}..{ref}").strip() or 0))
        if ahead:
            seen.add(sha)  # a local branch and its remote at the same commit are one row
            rows.append({"ref": ref, "head": sha, "ahead": ahead, "when": int(when), "author": author, "subject": subject})
    return rows


def triage(payload, base: str, ref: str) -> dict:
    """Score a branch's change from the base graph: the base-side hunks of `base...ref` mapped onto
    the functions the base defines. Added functions are counted, not scored (they are not in the graph)."""
    diff = _git(payload.repo_path, "diff", "-U0", "--no-color", "--relative", f"{base}...{ref}", check=True)
    old = pr_impact.old_ranges(diff)
    by_id = {n.id: n for n in payload.function_nodes}
    resolve = change_risk.service_resolver([n.file_path for n in payload.function_nodes if n.kind == "file"], [], pr_impact._is_test)
    has_tests = any(pr_impact._is_test(n.file_path) for n in payload.function_nodes if n.kind == "file")
    scored = []
    for r in pr_impact.analyze(payload, old, payload.graph_id, resolve):
        n = by_id[r["id"]]
        a, b = pr_impact._span(n)
        lines = sum(min(e, b) - max(s, a) + 1 for s, e in old.get(n.file_path, []) if s <= b and a <= e)
        s = change_risk.score_chunk(
            upstream=r["upstream"], endpoints=len(r["endpoints"]), semantic_kind=n.adapter_metadata.get("semantic_kind", "CALLS"),
            has_tests=has_tests, tested=r["tested"], header_changed=any(s <= a <= e for s, e in old.get(n.file_path, [])),
            lines_changed=lines, services_crossed=r["impact"]["crosses"], possible=r["possible"], name_shared=r["name_shared"])
        scored.append({"name": r["name"], "file": r["file"], "score": s["score"], "band": s["band"]})
    scored.sort(key=lambda c: -c["score"])
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    new_defs = len(re.findall(r"^\+\s*(?:async\s+)?def \w+", diff, re.M)) - len(re.findall(r"^-\s*(?:async\s+)?def \w+", diff, re.M))
    return {"ref": ref, "chunks": len(scored), "worst": scored[0] if scored else None, "top": scored[:3],
            "added": added, "removed": removed, "new_functions": max(0, new_defs), "estimate": True}


# ── worktrees ──────────────────────────────────────────────────────────────────────────────────

def _root(repo: str) -> Path:
    from app import storage
    # a dot directory: ingestion never walks into it, even when the reviewed repo holds the store
    return storage._STORE_DIR.resolve() / ".worktrees" / hashlib.sha1(str(Path(repo).resolve()).encode()).hexdigest()[:10]


def worktree_path(repo: str, ref: str) -> Path:
    return _root(repo) / re.sub(r"[^A-Za-z0-9_.-]", "_", ref)[:80]


def open_worktree(repo: str, ref: str) -> Path:
    """A detached worktree of `ref` that Flowify owns, updated to the ref's current commit."""
    path = worktree_path(repo, ref)
    _git(repo, "worktree", "prune")
    want = _git(repo, "rev-parse", ref).strip()
    if path.is_dir():
        _git(str(path), "checkout", "-q", "--detach", "--force", ref)  # Flowify's own copy: safe to force
    if not path.is_dir() or _git(str(path), "rev-parse", "HEAD").strip() != want:
        remove_worktree(repo, ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", "-q", "--detach", str(path), ref, check=True)
    os.utime(path)  # least-recently-used order
    _evict(repo, keep=path)
    return path


def worktrees(repo: str) -> list:
    root = _root(repo)
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: -p.stat().st_mtime) if root.is_dir() else []


def remove_worktree(repo: str, ref: str | None = None, path: Path | None = None) -> None:
    """Remove the worktree, its directory and its graph. Safe to call when nothing is there."""
    from app import storage
    path = path or worktree_path(repo, ref)
    gid = storage.find_graph_by_repo_path(str(path))
    if gid:
        storage.delete(gid)
    _git(repo, "worktree", "remove", "--force", str(path))
    shutil.rmtree(path, ignore_errors=True)
    _git(repo, "worktree", "prune")
    try:
        path.parent.rmdir()  # the per-repo folder, once its last checkout is gone
    except OSError:
        pass


def _evict(repo: str, keep: Path) -> None:
    for p in worktrees(repo)[MAX_WORKTREES:]:
        if p != keep:
            remove_worktree(repo, path=p)


def disk_usage(repo: str) -> int:
    return sum(f.stat().st_size for p in worktrees(repo) for f in p.rglob("*") if f.is_file())


# ── author checks ──────────────────────────────────────────────────────────────────────────────

def _python(repo: str) -> str:
    """The repo's own virtualenv when it has one (its tests need its dependencies), else ours."""
    if os.environ.get("FLOWIFY_TEST_PYTHON"):
        return os.environ["FLOWIFY_TEST_PYTHON"]
    for cand in (".venv/Scripts/python.exe", ".venv/bin/python", "venv/Scripts/python.exe", "venv/bin/python"):
        p = Path(repo) / cand
        if p.is_file() and subprocess.run([str(p), "-c", "import pytest"], capture_output=True, timeout=30).returncode == 0:
            return str(p)  # a venv without pytest cannot run the tests: fall through
    return sys.executable


def run_tests(repo: str, ids: list, timeout: int = 300) -> dict:
    """Run pytest node ids (which come from the graph, never from a request) and report each one."""
    ids = [i for i in ids if _ID.match(i) and not i.startswith("-")][:200]
    if not ids:
        return {"outcomes": {}, "returncode": None, "output": "No runnable pytest tests reach this change."}
    with tempfile.TemporaryDirectory(prefix="flowify_junit_") as d:
        junit = Path(d) / "r.xml"
        t0 = time.monotonic()
        run = list(ids)
        try:
            for _ in range(2):  # pytest refuses the whole run when one id is gone: drop those once, retry
                p = subprocess.run([_python(repo), "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", *run],
                                   cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
                gone = [g.replace("\\", "/") for g in re.findall(r"not found: (.+)", p.stdout + p.stderr)]  # absolute, may hold spaces
                if p.returncode != 4 or not gone:
                    break
                run = [i for i in run if not any(g == i or g.endswith("/" + i) for g in gone)]
                if not run:
                    break
        except subprocess.TimeoutExpired:
            return {"outcomes": {i: "timeout" for i in ids}, "returncode": None, "output": f"Stopped after {timeout} s."}
        cases = []
        if junit.is_file():
            try:
                cases = list(ET.parse(junit).getroot().iter("testcase"))
            except ET.ParseError:
                pass
    outcomes = {}
    for i in ids:
        path, *parts = i.split("::")
        cls = ".".join([path[:-3].replace("/", "."), *parts[:-1]])
        mine = [c for c in cases if c.get("classname") == cls and (c.get("name") == parts[-1] or c.get("name", "").startswith(parts[-1] + "["))]
        if not mine:
            outcomes[i] = "not run"
        elif any(c.find(t) is not None for c in mine for t in ("failure", "error")):
            outcomes[i] = "failed"
        elif all(c.find("skipped") is not None for c in mine):
            outcomes[i] = "skipped"
        else:
            outcomes[i] = "passed"
    out = {"outcomes": outcomes, "returncode": p.returncode, "seconds": round(time.monotonic() - t0, 1),
           "output": "\n".join((p.stdout.strip() + "\n" + p.stderr.strip()).strip().splitlines()[-15:])}
    if all(o == "not run" for o in outcomes.values()):  # pytest itself failed (not installed, a collection error)
        last = (p.stderr.strip() or p.stdout.strip() or "no output").splitlines()[-1]
        out["error"] = f"pytest could not run the tests: {last[:300]}"
    return out


def read_notes(repo: str) -> dict:
    try:
        data = json.loads((Path(repo) / NOTES_PATH).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("chunks"), dict) else {}
    except (OSError, ValueError):
        return {}


def write_notes(repo: str, head: str, chunks: dict) -> dict:
    """Validate and write the author's notes. Keys are `<file>::<qualname>`; values carry the chunk
    hash they were written against, so a reviewer can tell when the code moved on."""
    clean = {}
    for key, v in list(chunks.items())[:500]:
        if not isinstance(key, str) or "::" not in key or not isinstance(v, dict) or not isinstance(v.get("hash"), str):
            raise ValueError(f"bad entry: {key!r}")
        tests = [{"id": str(t["id"])[:300], "outcome": str(t["outcome"])[:20]} for t in (v.get("tests") or [])[:200]
                 if isinstance(t, dict) and "id" in t and "outcome" in t]
        clean[key[:400]] = {"hash": v["hash"][:32], "note": str(v.get("note") or "")[:2000], "tests": tests,
                            "head": head, "at": int(time.time())}
    data = {"version": 1, "head": head, "chunks": clean}
    p = Path(repo) / NOTES_PATH
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def parts(rows: list) -> list:
    """Changed functions grouped by what connects them (a call between two of them, or a shared
    direct caller). Two or more groups mean the change could be split."""
    ids = [r["id"] for r in rows]
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner_of_caller: dict = {}
    for r in rows:
        for c in r["direct"]:
            if c["id"] in parent:
                parent[find(c["id"])] = find(r["id"])
            elif c["id"] in owner_of_caller:
                parent[find(owner_of_caller[c["id"]])] = find(r["id"])
            else:
                owner_of_caller[c["id"]] = r["id"]
    groups: dict = {}
    for r in rows:
        g = groups.setdefault(find(r["id"]), {"chunks": [], "files": set(), "lines": 0})
        g["chunks"].append(r["name"])
        g["files"].add(r["file"])
        g["lines"] += sum(e - s + 1 for s, e in r["hunks"])
    out = [{"chunks": g["chunks"], "files": sorted(g["files"]), "lines": g["lines"]} for g in groups.values()]
    return sorted(out, key=lambda g: -g["lines"])
