"""Dry-run trace: walk the call graph from one function in execution order.

A static graph cannot say which branch a real run takes, so a trace shows the
whole plan instead: calls in the order they appear in the source, each tagged
with the control flow it sits in ("cond", "loop", "try", "except"), plus the
calls the graph could not resolve. Breadth is limited on purpose — a trace is
meant to be read by a person, not to enumerate every path.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from . import storage

_ENTRY_NAMES = {"main", "run", "start", "serve", "handle", "handler", "cli", "app", "execute"}
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__)(/|$)|(^|/)test_|_test\.")
_INF = 10 ** 9

_cache: Dict[str, dict] = {}


def index(graph_id: str) -> dict:
    """The graph's call index, built once and reused across trace requests."""
    idx = _cache.get(graph_id)
    if idx is None:
        if len(_cache) >= 3:
            _cache.pop(next(iter(_cache)))
        idx = _cache[graph_id] = storage.call_index(graph_id)
    return idx


def invalidate(graph_id: str) -> None:
    _cache.pop(graph_id, None)


def _callees(idx: dict, nid: str) -> List[tuple]:
    """Distinct callees of `nid` in source order (the first call site wins)."""
    first: Dict[str, tuple] = {}
    for pos, (tgt, line, ctx, conf, reason) in enumerate(idx["calls"].get(nid, ())):
        if tgt != nid and tgt not in first:
            first[tgt] = (line if line is not None else _INF, pos, ctx, conf, reason)
    return sorted(((t, v) for t, v in first.items()), key=lambda kv: kv[1][:2])


def dry_run(graph_id: str, start_id: str, max_depth: int = 6, max_steps: int = 80, fanout: int = 6) -> Optional[dict]:
    """Depth-first, source-ordered walk from `start_id`; None if it isn't a node.

    The fan-out shrinks with depth (fanout, fanout-1, ... never below 2) so a
    wide top level cannot use the whole step budget before the other branches
    are reached. A node reached a second time is listed but not re-expanded.
    """
    idx = index(graph_id)
    steps: List[dict] = []
    first: Dict[str, int] = {}
    on_path: set = set()

    def visit(nid: str, depth: int, via: Optional[dict], parent: Optional[int]) -> None:
        i = len(steps)
        step = {"index": i, "id": nid, "depth": depth, "via": via, "parent": parent}
        steps.append(step)
        if nid in on_path:
            step["cycle"] = True
            return
        if nid in first:
            step["repeat_of"] = first[nid]
            return
        first[nid] = i
        callees = _callees(idx, nid)
        if not callees:
            return
        if depth >= max_depth:
            step["cut"] = len(callees)
            return
        limit = max(2, fanout - depth)
        if len(callees) > limit:
            step["hidden_calls"] = len(callees) - limit
        on_path.add(nid)
        for tgt, (line, _pos, ctx, conf, reason) in callees[:limit]:
            if len(steps) >= max_steps:
                step["truncated"] = True
                break
            visit(tgt, depth + 1, _via(line, ctx, conf, reason), i)
        on_path.discard(nid)

    visit(start_id, 0, None, None)

    stats = _decorate(graph_id, idx, steps, list(first))
    if stats is None:
        return None
    stats.update(unique_nodes=len(first), truncated=any(s.get("truncated") for s in steps))
    return {"graph_id": graph_id, "start": start_id, "steps": steps, "stats": stats}


def _via(line, ctx, conf, reason) -> dict:
    return {"line": None if line == _INF else line, "ctx": ctx, "confidence": conf, "reason": reason}


def _decorate(graph_id: str, idx: dict, steps: List[dict], ids: List[str]) -> Optional[dict]:
    """Attach name/file/module/ambiguity to each step; returns the shared stats,
    or None if the first step is not a node of this graph."""
    brief = storage.nodes_brief(graph_id, ids)
    if steps[0]["id"] not in brief:
        return None
    module_of = idx["module_of"]
    unresolved_total = 0
    for s in steps:
        b = brief.get(s["id"], {})
        s["name"] = b.get("name") or s["id"].split("::")[-1]
        s["file_path"] = b.get("file_path", "")
        s["type"] = b.get("type")
        s["lineno"] = b.get("lineno")
        s["summary"] = (b.get("summary") or "")[:160]
        s["semantic_kind"] = b.get("semantic_kind")
        s["module"] = module_of.get(s["id"])
        parent = steps[s["parent"]] if s["parent"] is not None else None
        s["enters_module"] = bool(parent and s["module"] != parent["module"])
        if "repeat_of" not in s and "cycle" not in s:
            un = b.get("unresolved_calls") or []
            unresolved_total += len(un)
            if un:
                s["unresolved"] = un[:5]
                s["unresolved_count"] = len(un)
    modules: List[str] = []
    for s in steps:
        if s["module"] and (not modules or modules[-1] != s["module"]):
            modules.append(s["module"])
    return {
        "steps": len(steps),
        "max_depth": max(s["depth"] for s in steps),
        "modules": modules,
        "unresolved_calls": unresolved_total,
    }


def path(graph_id: str, source_id: str, target_id: str, max_len: int = 12) -> Optional[dict]:
    """Shortest resolved call chain from `source_id` to `target_id`, in the same
    step format as a dry run. `found` is False when the resolved graph has none —
    which can also mean the link runs through a call the graph could not resolve."""
    idx = index(graph_id)
    calls = idx["calls"]
    parent: Dict[str, Optional[str]] = {source_id: None}
    ways: Dict[str, int] = {source_id: 1}  # number of distinct shortest paths to each node
    frontier = [source_id]
    depth = 0
    while frontier and target_id not in parent and depth < max_len:
        depth += 1
        nxt: List[str] = []
        layer: set = set()
        for n in frontier:
            for t, *_ in calls.get(n, ()):
                if t == n:
                    continue
                if t not in parent:
                    parent[t] = n
                    ways[t] = ways[n]
                    nxt.append(t)
                    layer.add(t)
                elif t in layer:  # another shortest route to a node found this round
                    ways[t] += ways[n]
        frontier = nxt

    if target_id not in parent:
        chain = [source_id]
    else:
        chain, n = [], target_id
        while n is not None:
            chain.append(n)
            n = parent[n]
        chain.reverse()

    steps = [{"index": i, "id": nid, "depth": i, "via": None, "parent": i - 1 if i else None} for i, nid in enumerate(chain)]
    for i in range(1, len(steps)):
        line, ctx, conf, reason = next(((l, c, f, r) for t, l, c, f, r in calls[chain[i - 1]] if t == chain[i]), (None, None, None, None))
        steps[i]["via"] = _via(line if line is not None else _INF, ctx, conf, reason)
    stats = _decorate(graph_id, idx, steps, chain)
    if stats is None:
        return None
    found = target_id in parent
    stats.update(found=found, alternatives=min(ways.get(target_id, 0), 99) if found else 0)
    return {"graph_id": graph_id, "start": source_id, "target": target_id, "steps": steps, "stats": stats}


def starts(graph_id: str, limit: int = 12) -> List[dict]:
    """Good places to begin a trace: functions nobody calls that do call others,
    ranked by API endpoints and conventional entry names, then by how much they reach."""
    idx = index(graph_id)
    calls, callers = idx["calls"], idx["callers"]
    roots = sorted((n for n in calls if not callers.get(n)), key=lambda n: -len(calls[n]))[:80]
    brief = storage.nodes_brief(graph_id, roots)

    def reach(start: str, cap: int = 400) -> int:
        seen, frontier = {start}, [start]
        while frontier and len(seen) < cap:
            nxt = []
            for n in frontier:
                for t, *_ in calls.get(n, ()):
                    if t not in seen:
                        seen.add(t)
                        nxt.append(t)
            frontier = nxt
        return len(seen) - 1

    out = []
    for n in roots:
        b = brief.get(n)
        if not b:
            continue
        api = b["semantic_kind"] == "EXPOSES_API"
        entry = b["name"] in _ENTRY_NAMES
        r = reach(n)
        score = r + (300 if api else 0) + (150 if entry else 0) - (1000 if _TEST_PATH.search(b["file_path"] or "") else 0)
        out.append({
            "id": n, "name": b["name"], "file_path": b["file_path"], "module": idx["module_of"].get(n),
            "reach": r, "score": score,
            "reason": "API endpoint" if api else "entry function" if entry else "not called by anything",
        })
    out.sort(key=lambda d: -d["score"])
    return out[:limit]


def upstream(callers: Dict[str, list], start: str) -> set:
    """Every function that can reach `start` through resolved calls (transitive callers)."""
    seen, todo = {start}, [start]
    while todo:
        for c in callers.get(todo.pop(), ()):
            if c not in seen:
                seen.add(c)
                todo.append(c)
    seen.discard(start)
    return seen


_LEVELS = ["low", "medium", "high", "critical"]


def risk_level(upstream_count: int, criticality: str, semantic_kind: str, unresolved: int) -> str:
    """Triage label for changing a function. A change breaks its *callers*, so the size of the
    upstream set drives it; an API/DB role and unconfirmed callers can only raise it, because
    a missing edge must never read as safe."""
    lvl = 3 if upstream_count > 30 else 2 if upstream_count > 10 else 1 if upstream_count > 3 else 0
    floor = {"EXPOSES_API": 2, "USES_DB": 1}.get(semantic_kind, 0)
    if criticality == "critical":
        floor = max(floor, 2)
    elif criticality == "high":
        floor = max(floor, 1)
    lvl = max(lvl, floor)
    if unresolved >= 3 and lvl == 0:
        lvl = 1
    return _LEVELS[lvl]
