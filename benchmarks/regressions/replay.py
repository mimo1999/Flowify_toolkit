"""Would the PR check have pointed at the place a real regression broke?

For a (culprit PR, fix PR) pair from a repo's history, where the fix says which PR
broke things:

  1. build the graph of the tree *at the culprit commit* (via `git archive`, so the
     checkout is never touched),
  2. run the PR check on the culprit's own diff -> changed functions + everything upstream,
  3. find the functions the *fix* had to edit (that is where it broke),
  4. score where those sit relative to what the check reported.

Verdicts, strictest first:
  dependent  a fixed function is a (transitive) caller of a changed one: the check listed
             the place that broke although the culprit diff never touched it. The real win.
  possible   not in the resolved upstream set, but the fixed function calls a changed
             function's *name*: it was among the "unconfirmed callers" the check warns about.
  same       the fix edits a function the culprit itself changed. The check lists it, but so
             does the diff; no credit claimed.
  file       only a same-file match (fix edits another function in a file the culprit touched).
  missed     the fix is somewhere the check gave no pointer to.
  n/a        culprit or fix has no Python function change (C++/CUDA kernels, config, docs).

    python benchmarks/regressions/replay.py <repo> --pairs benchmarks/regressions/vllm_pairs.json --out results.json
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

_STORE = tempfile.mkdtemp(prefix="flowify_replay_")
os.environ.setdefault("FLOWIFY_STORE", _STORE)
# The store holds every snapshot's graph (~3 GB over a full run) and nothing else needs it
# afterwards, so it goes when the run ends, however it ends.
import atexit  # noqa: E402
import shutil  # noqa: E402
os.environ["LLM_PROVIDER"] = "heuristic"
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.models import GraphPayload  # noqa: E402,F401  (models import must come first)
from app import change_risk, pipeline, pr_impact, storage, trace  # noqa: E402


def _remove_store() -> None:
    try:
        storage._conn().close()  # Windows cannot delete a sqlite file that is still open
    except Exception:
        pass
    shutil.rmtree(_STORE, ignore_errors=True)


atexit.register(_remove_store)

_HUNK_BOTH = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def git(repo: str, *args: str, binary: bool = False):
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def sha_of_pr(repo: str, pr: int) -> str | None:
    out = git(repo, "log", "--format=%H", "-E", f"--grep=\\(#{pr}\\)$").split()
    return out[-1] if out else None  # oldest match = the original merge, not a later revert-of-revert


def checkout_tree(repo: str, sha: str, dirs: list[str], dest: Path) -> None:
    """Materialise `dirs` at `sha` under dest without touching the repo's index or worktree."""
    data = git(repo, "archive", "--format=tar", sha, "--", *dirs, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest)  # our own `git archive` output; the "data" filter needs Python >= 3.10.12


def enclosing_functions(src: str, ranges: list[tuple[int, int]]) -> set[str]:
    """Qualified names (Class.method, outer.inner) of the innermost defs overlapping `ranges`."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    found: set[str] = set()

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}{child.name}"
                inner_before = len(found)
                walk(child, qual + ".")
                is_fn = not isinstance(child, ast.ClassDef)
                if is_fn and len(found) == inner_before and any(
                        child.lineno <= b and a <= (child.end_lineno or child.lineno) for a, b in ranges):
                    found.add(qual)
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def fix_locations(repo: str, fix: str) -> tuple[set[tuple[str, str]], set[str]]:
    """({(file, qualname)} the fix edited, {python files it touched}). Old-side lines are
    resolved in the pre-fix tree and new-side lines in the fixed tree, so both edits and
    pure additions map to a function."""
    diff = git(repo, "diff", "-U0", "--no-color", f"{fix}^", fix, "--", "*.py")
    locs: set[tuple[str, str]] = set()
    files: set[str] = set()
    old_path = new_path = None
    old_r: dict[str, list] = {}
    new_r: dict[str, list] = {}
    for line in diff.splitlines():
        if line.startswith("--- "):
            old_path = None if line == "--- /dev/null" else line[6:]
        elif line.startswith("+++ "):
            new_path = None if line == "+++ /dev/null" else line[6:]
        elif m := _HUNK_BOTH.match(line):
            a, an, b, bn = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
            if old_path and an:
                old_r.setdefault(old_path, []).append((a, a + an - 1))
            if new_path and bn:
                new_r.setdefault(new_path, []).append((b, b + bn - 1))
    for rev, table in ((f"{fix}^", old_r), (fix, new_r)):
        for path, ranges in table.items():
            files.add(path)
            try:
                src = git(repo, "show", f"{rev}:{path}")
            except subprocess.CalledProcessError:
                continue
            locs |= {(path, q) for q in enclosing_functions(src, ranges)}
    return locs, files


_graphs: dict[str, tuple] = {}


def graph_at(repo: str, sha: str, dirs: list[str]):
    if sha not in _graphs:
        work = Path(tempfile.mkdtemp(prefix=f"replay_{sha[:8]}_"))
        checkout_tree(repo, sha, dirs, work)
        t = time.time()
        import contextlib
        try:
            with contextlib.redirect_stdout(sys.stderr):
                payload = pipeline.ingest(str(work))
        finally:
            shutil.rmtree(work, ignore_errors=True)  # the graph is in `payload` and the store; the files are not needed
        callers: dict = {}
        callee_names: dict = {}
        for e in payload.function_edges:
            if e.relationship == "INVOKES" or e.type == "CALLS":
                callers.setdefault(e.target_id, []).append(e.source_id)
        for n in payload.function_nodes:
            for u in n.adapter_metadata.get("unresolved_calls", []) or []:
                name = u.get("callee") if isinstance(u, dict) else u
                if isinstance(u, dict) and (u.get("candidates") or 0) > storage.MAX_NAME_CANDIDATES:
                    continue  # same cut-off the product applies: too common a name to be a pointer
                if name:
                    callee_names.setdefault(n.id, set()).add(str(name).split(".")[-1])
        _graphs[sha] = (payload, callers, callee_names, round(time.time() - t, 1))
    return _graphs[sha]


def replay(repo: str, culprit_pr: int, fix_pr: int, dirs: list[str]) -> dict:
    out: dict = {"culprit_pr": culprit_pr, "fix_pr": fix_pr}
    c, f = sha_of_pr(repo, culprit_pr), sha_of_pr(repo, fix_pr)
    if not c or not f:
        return {**out, "verdict": "n/a", "why": "commit not found"}
    out |= {"culprit": c[:10], "fix": f[:10],
            "culprit_title": git(repo, "log", "-1", "--format=%s", c).strip(),
            "fix_title": git(repo, "log", "-1", "--format=%s", f).strip()}

    payload, callers, callee_names, secs = graph_at(repo, c, dirs)
    diff = git(repo, "diff", "-U0", "--no-color", f"{c}^", c)
    files, _deleted = pr_impact.parse_diff(diff)
    resolve = change_risk.service_resolver([n.file_path for n in payload.function_nodes if n.kind == "file"], [], pr_impact._is_test)
    rows = pr_impact.analyze(payload, files, payload.graph_id, resolve)
    nodes = {n.id: n for n in payload.function_nodes}
    changed = {r["id"] for r in rows}
    up: set = set()
    for cid in changed:
        up |= trace.upstream(callers, cid)
    up -= changed
    changed_names = {nodes[i].name for i in changed}

    locs, fix_files = fix_locations(repo, f)
    by_qual = {(n.file_path, n.qualified_name or n.name): n.id for n in nodes.values()}
    by_qual |= {(n.file_path, n.id.split("::", 1)[-1]): n.id for n in nodes.values()}
    mapped = {by_qual[loc] for loc in locs if loc in by_qual}
    risk = [trace.risk_level(r["upstream"], "medium", nodes[r["id"]].adapter_metadata.get("semantic_kind", "CALLS"), r["possible"]) for r in rows]

    out |= {
        "ingest_seconds": secs, "graph_functions": len(nodes),
        "changed_functions": len(changed), "upstream_functions": len(up),
        "unconfirmed_callers": sum(r["possible"] for r in rows),
        "top_risk": max(risk, key=["low", "medium", "high", "critical"].index) if risk else None,
        "fix_functions": sorted(f"{p}::{q}" for p, q in locs),
        "fix_functions_in_graph": len(mapped),
    }
    dep = mapped & up
    same = mapped & changed
    poss = {i for i in mapped - up - changed if callee_names.get(i, set()) & changed_names}
    if not changed:
        return {**out, "verdict": "n/a", "why": "culprit changed no Python function the graph tracks"}
    if not locs:
        return {**out, "verdict": "n/a", "why": "fix changed no Python function"}
    if dep:
        v, hits = "dependent", dep
    elif poss:
        v, hits = "possible", poss
    elif same:
        v, hits = "same", same
    elif fix_files & {nodes[i].file_path for i in changed | up}:
        v, hits = "file", set()
    else:
        v, hits = "missed", set()
    tests_hit = sorted(i for i in dep if pr_impact._TEST_PATH.search(nodes[i].file_path))
    out |= {"verdict": v, "hits": sorted(hits)[:8], "hit_is_test": bool(tests_hit) and len(tests_hit) == len(dep)}
    return {**out, **_scores(payload, rows, files, diff, dep, same, callers)}


def _scores(payload, rows, files, diff, dep, same, callers) -> dict:
    """Calibration data for change_risk.BANDS: every chunk's score (base factors only: no git
    checkout here, so no contract or history points) and where the culprit chunk lands. The culprit
    is the changed function the fix edited ("same"), or the changed function the fixed dependent
    reaches ("dependent"); best-ranked one if several."""
    nodes = {n.id: n for n in payload.function_nodes}
    before = pr_impact.removed_lines(diff)
    has_tests = any(pr_impact._is_test(n.file_path) for n in payload.function_nodes if n.kind == "file")
    importance = pr_impact._importance(payload.graph_id)
    for r in rows:
        n = nodes[r["id"]]
        a, b = pr_impact._span(n)
        r["hunks"] = [[max(s, a), min(e, b)] for s, e in files.get(n.file_path, []) if s <= b and a <= e]
        r["removed"] = [{"at": at, "lines": ls} for at, ls in before.get(n.file_path, []) if a <= at <= b + 1]
        pr_impact.score_row(r, n, has_tests, importance)
    rows.sort(key=lambda r: -r["score"])
    pr_impact.mark_top(rows)
    culprits = [r for r in rows if r["id"] in same or (dep and trace.upstream(callers, r["id"]) & dep)]
    best = culprits[0] if culprits else None
    return {"scores": [r["score"] for r in rows], "must_check": sum(pr_impact.bucket(r) == "must" for r in rows),
            "culprit_chunk": best and {"name": best["name"], "score": best["score"], "band": best["band"],
                                 "rank": rows.index(best) + 1, "bucket": pr_impact.bucket(best)}}


def main(argv: list) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("repo")
    p.add_argument("--pairs", required=True, help="JSON: {dirs: [...], pairs: [[culprit_pr, fix_pr], ...]}")
    p.add_argument("--out")
    p.add_argument("--only", type=int, help="replay only the pair with this fix PR")
    a = p.parse_args(argv)
    spec = json.loads(Path(a.pairs).read_text())
    results = []
    for culprit, fix in spec["pairs"]:
        if a.only and fix != a.only:
            continue
        try:
            r = replay(a.repo, culprit, fix, spec["dirs"])
        except Exception as e:  # one bad commit must not lose the rest of a long run
            r = {"culprit_pr": culprit, "fix_pr": fix, "verdict": "error", "why": f"{type(e).__name__}: {e}"}
        results.append(r)
        print(f"#{culprit} -> #{fix}: {r['verdict']}"
              f" (changed {r.get('changed_functions')}, upstream {r.get('upstream_functions')}) {r.get('why', '')}", flush=True)
        if a.out:
            Path(a.out).write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
