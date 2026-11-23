"""How accurate is the static call graph? Compare it with what really ran.

The repo's own test suite is executed under a call tracer, which records every
project-function -> project-function call that actually happened and every source
line that made a call. The static graph is then scored against that:

  recall     runtime calls the graph contains / runtime calls
  precision  per resolver tier: edges whose call really happened /
             edges the tests could judge (call happened, or the call-site line
             ran without reaching that callee)

Edges whose call site the tests never ran are "unjudged" and left out of
precision; they are reported so nobody reads the numbers as more than they are.

    python benchmarks/accuracy/measure.py <repo> [--src src] [--tests tests] [--out result.json]

The repo's dependencies must be importable (the tests run in this process).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("FLOWIFY_EXCLUDE_DIRS", "tests,test,__tests__,docs,examples,scripts,benchmarks")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.models import FunctionNode, GraphPayload  # noqa: E402,F401  (models import must come first)
from app import graph_analytics, graph_builder  # noqa: E402

_COMPREHENSION = {"<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>", "<lambda>"}
_TIERS = [
    ("self-call", "self-call"),
    ("attribute-type", "recorded type"),
    ("receiver-class", "receiver class"),
    ("module-alias", "module alias"),
    ("same-file", "same-file"),
    ("unique-repo-wide", "unique repo-wide function"),
    ("constructor", "constructor call"),
    ("unique-method", "unique repo-wide method"),
]


def tier_of(reason: str | None) -> str:
    for name, needle in _TIERS:
        if reason and needle in reason:
            return name
    return "other"


class Tracer:
    """Records project-to-project calls made while a test runs."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: set = set()      # (caller_key, callee_key)
        self.lines: set = set()      # (caller_key, line) where a call was made
        self._rel: dict = {}

    def rel(self, filename: str):
        r = self._rel.get(filename, 0)
        if r == 0:
            try:
                p = Path(filename).resolve().relative_to(self.root)
                r = None if any(part in ("tests", "test", "__tests__") for part in p.parts) else p.as_posix()
            except (ValueError, OSError):
                r = None
            self._rel[filename] = r
        return r

    def _key(self, frame):
        while frame is not None and frame.f_code.co_name in _COMPREHENSION:
            frame = frame.f_back
        if frame is None:
            return None, None
        rel = self.rel(frame.f_code.co_filename)
        if rel is None or frame.f_code.co_name.startswith("<"):
            return None, frame
        return (rel, frame.f_code.co_firstlineno, frame.f_code.co_name), frame

    def __call__(self, frame, event, arg):
        if event == "c_call":
            key, f = self._key(frame)
            if key:
                self.lines.add((key, frame.f_lineno))
        elif event == "call":
            if frame.f_code.co_name in _COMPREHENSION:
                return
            callee, _ = self._key(frame)
            if callee is None:
                return
            back = frame.f_back
            caller, _ = self._key(back)
            if caller is not None:
                self.calls.add((caller, callee))
                self.lines.add((caller, back.f_lineno))


def run_tests(root: Path, tests: str, tracer: Tracer) -> int:
    import pytest

    class Plugin:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            sys.setprofile(tracer)
            threading.setprofile(tracer)
            try:
                yield
            finally:
                sys.setprofile(None)
                threading.setprofile(None)

    os.chdir(root)
    return pytest.main(["-q", "-p", "no:cacheprovider", "--no-header", tests], plugins=[Plugin()])


def dead_code_check(nodes, edges, called: set, root: Path) -> dict:
    """How many "no static callers" candidates were in fact called while the tests ran.

    Only calls made by project code are recorded, so a function called solely from a test
    counts as unobserved: the false-positive rate is a lower bound. The UI shows the first 50."""
    payload = GraphPayload(graph_id="m", repo_path=str(root), function_nodes=nodes, function_edges=edges,
                           module_nodes=[], module_edges=[], module_to_functions={})
    found = graph_analytics.compute_analytics(payload)["dead_code"]
    wrong = [d for d in found if d["id"] in called]
    return {"candidates": len(found), "called_at_runtime": len(wrong),
            "false_positive_rate": round(len(wrong) / len(found), 3) if found else None,
            "examples": [d["name"] for d in wrong[:8]]}


def measure(repo: str, src: str | None, tests: str) -> dict:
    root = Path(repo).resolve()
    for p in (root / src if src else root, root):
        sys.path.insert(0, str(p))

    _, nodes, edges = graph_builder.build_function_graph(str(root))
    callables = [n for n in nodes if n.kind in ("function", "callable")]
    by_file_name = collections.defaultdict(list)
    for n in callables:
        by_file_name[(n.file_path, n.name)].append((n.lineno or 0, n.id))
    node_by_id = {n.id: n for n in callables}

    tracer = Tracer(root)
    rc = run_tests(root, tests, tracer)

    def node_id(key):
        file, first, name = key
        near = [(ln - first, nid) for ln, nid in by_file_name.get((file, name), ()) if 0 <= ln - first <= 25]
        return min(near)[1] if near else None

    runtime = set()
    executed_lines = set()
    for a, b in tracer.calls:
        ia, ib = node_id(a), node_id(b)
        if ia and ib and ia != ib:
            runtime.add((ia, ib))
    for key, line in tracer.lines:
        i = node_id(key)
        if i:
            executed_lines.add((i, line))

    static = {}
    for e in edges:
        if e.relationship == "INVOKES" and e.source_id in node_by_id and e.target_id in node_by_id:
            md = e.adapter_metadata or {}
            static.setdefault((e.source_id, e.target_id), (md.get("line"), md.get("resolution_confidence"), md.get("resolution_reasoning")))

    # recall: what ran but the graph lacks, split into "known ambiguous" and "invisible"
    found = [p for p in runtime if p in static]
    missed = [p for p in runtime if p not in static]
    ambiguous = invisible = 0
    causes: collections.Counter = collections.Counter()
    for src_id, dst_id in missed:
        names = {u["callee"] for u in node_by_id[src_id].adapter_metadata.get("unresolved_calls", [])}
        callee = node_by_id[dst_id]
        if callee.name in names:
            ambiguous += 1
        else:
            invisible += 1
            causes["constructor (__init__)" if callee.name == "__init__" else "other dunder" if callee.name.startswith("__") else "method" if callee.type == "method" else "function"] += 1

    tiers = collections.defaultdict(lambda: collections.Counter())
    buckets = collections.defaultdict(lambda: collections.Counter())
    for (s, t), (line, conf, reason) in static.items():
        if (s, t) in runtime:
            verdict = "verified"
        elif line is not None and (s, line) in executed_lines:
            verdict = "refuted"
        else:
            verdict = "unjudged"
        tiers[tier_of(reason)][verdict] += 1
        buckets[conf][verdict] += 1

    def rate(c):
        j = c["verified"] + c["refuted"]
        return round(c["verified"] / j, 3) if j else None

    return {
        "repo": root.name,
        "pytest_exit": rc,
        "functions": len(callables),
        "static_edges": len(static),
        "runtime_edges": len(runtime),
        "recall": round(len(found) / len(runtime), 3) if runtime else None,
        "dead_code": dead_code_check(nodes, edges, {b for _, b in runtime}, root),
        "missed": {"known_ambiguous": ambiguous, "invisible": invisible, "invisible_by_kind": dict(causes)},
        "precision_by_tier": {
            t: {**dict(c), "precision": rate(c)} for t, c in sorted(tiers.items(), key=lambda kv: -sum(kv[1].values()))
        },
        "precision_by_confidence": {
            str(k): {**dict(c), "precision": rate(c)} for k, c in sorted(buckets.items(), key=lambda kv: (kv[0] is None, kv[0]))
        },
        "overall_precision": rate(sum(tiers.values(), collections.Counter())),
        "judged_share": round(
            sum(c["verified"] + c["refuted"] for c in tiers.values()) / max(1, len(static)), 3
        ),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo")
    ap.add_argument("--src", help="package root to put on sys.path, e.g. src")
    ap.add_argument("--tests", default="tests")
    ap.add_argument("--out")
    a = ap.parse_args()
    result = measure(a.repo, a.src, a.tests)
    text = json.dumps(result, indent=2)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
