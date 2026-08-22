"""Advanced graph analytics: centrality, god nodes, cycles, dead code, coupling.

Pure NetworkX computation over the stored call graph — no LLM calls. Results
are JSON-serializable dicts intended to be cached in graph metadata and served
by /graph_analytics, the architecture report, and the MCP analytics tools.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional

import networkx as nx

from .models import GraphPayload, FunctionNode
from .retrieval import _is_code_symbol, _is_invocation

# Betweenness is O(V·E); cap exact computation and sample beyond it.
_BETWEENNESS_EXACT_LIMIT = 800
_BETWEENNESS_SAMPLE_K = 200
_MAX_CYCLES = 25
_MAX_CYCLE_LEN = 8
# nx.simple_cycles enumerates candidates faster than _keep_cycle can reject
# them (real repos measured well over 100k/sec) — a strongly-connected
# cluster of same-named overload variants (`@overload def command(...)`
# repeated per signature is common in typed public APIs) can produce a
# cycle space where the overwhelming majority of candidates are rejected,
# so the old "stop once we've kept _MAX_CYCLES" break never fires: ingest
# hung for over an hour on a real-world repo before this cap existed. Both
# a candidate-count and a wall-clock ceiling bound it regardless of how
# adversarial the graph's naming/connectivity turns out to be.
_MAX_CYCLE_CANDIDATES = 200_000
_MAX_CYCLE_SECONDS = 5.0

_COMPLEXITY_RANK = {"low": 0, "medium": 1, "high": 2, "very_high": 3}
_CRITICALITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _call_graph(payload: GraphPayload) -> nx.DiGraph:
    """Directed graph of callable/type nodes connected by INVOKES edges.

    Uses retrieval.py's node/edge predicates so "what counts as a code symbol
    or call edge" has one definition shared across query retrieval and analytics.
    """
    g = nx.DiGraph()
    for n in payload.function_nodes:
        if _is_code_symbol({"kind": n.kind, "type": n.type}):
            g.add_node(n.id, name=n.name, file_path=n.file_path)
    for e in payload.function_edges:
        if _is_invocation({"relationship": e.relationship, "type": e.type}) and \
                g.has_node(e.source_id) and g.has_node(e.target_id):
            g.add_edge(e.source_id, e.target_id)
    return g


def _node_brief(n: FunctionNode) -> dict:
    return {
        "id": n.id,
        "name": n.name,
        "file_path": n.file_path,
        "summary": (n.summary or "")[:160],
        "criticality": n.semantics.criticality if n.semantics else None,
    }


def _top_module_dir(file_path: str) -> str:
    parts = file_path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if parts else "")


def compute_analytics(payload: GraphPayload) -> dict:
    """Compute the full analytics bundle for a graph."""
    g = _call_graph(payload)
    func_by_id = {n.id: n for n in payload.function_nodes}
    n_nodes = g.number_of_nodes()
    n_edges = g.number_of_edges()

    # ── Centrality ────────────────────────────────────────────────────────────
    in_deg = dict(g.in_degree())
    out_deg = dict(g.out_degree())
    try:
        pagerank = nx.pagerank(g, alpha=0.85) if n_nodes else {}
    except Exception:
        pagerank = {}
    try:
        if n_nodes <= _BETWEENNESS_EXACT_LIMIT:
            betweenness = nx.betweenness_centrality(g)
        elif n_nodes:
            betweenness = nx.betweenness_centrality(g, k=min(_BETWEENNESS_SAMPLE_K, n_nodes))
        else:
            betweenness = {}
    except Exception:
        betweenness = {}

    def composite(nid: str) -> float:
        return (
            pagerank.get(nid, 0.0) * 100
            + in_deg.get(nid, 0) * 0.5
            + out_deg.get(nid, 0) * 0.25
            + betweenness.get(nid, 0.0) * 50
        )

    scores = {nid: composite(nid) for nid in g.nodes}
    ranked = sorted(g.nodes, key=scores.get, reverse=True)

    god_nodes = []
    for nid in ranked[:20]:
        n = func_by_id.get(nid)
        if not n:
            continue
        god_nodes.append({
            **_node_brief(n),
            "score": round(scores[nid], 3),
            "pagerank": round(pagerank.get(nid, 0.0), 5),
            "in_degree": in_deg.get(nid, 0),
            "out_degree": out_deg.get(nid, 0),
            "betweenness": round(betweenness.get(nid, 0.0), 5),
        })

    # ── Cycles ────────────────────────────────────────────────────────────────
    # Cycles where every node shares one name are almost always artifacts of the
    # heuristic name-based call resolution (foo() matches every `foo`), not real
    # circular dependencies — drop them.
    def _keep_cycle(cyc: List[str]) -> bool:
        if len(cyc) < 2:
            return False
        names = {func_by_id[nid].name for nid in cyc if nid in func_by_id}
        return len(names) > 1

    def _cycle_payload(cyc: List[str]) -> List[dict]:
        return [
            {"id": nid, "name": func_by_id[nid].name, "file_path": func_by_id[nid].file_path}
            for nid in cyc if nid in func_by_id
        ]

    cycles: List[List[dict]] = []
    cycles_truncated = False
    try:
        t0 = time.monotonic()
        for i, cyc in enumerate(nx.simple_cycles(g, length_bound=_MAX_CYCLE_LEN)):
            if _keep_cycle(cyc):
                cycles.append(_cycle_payload(cyc))
            if len(cycles) >= _MAX_CYCLES:
                break
            # Check wall-clock only every so often — time.monotonic() every
            # iteration would itself be a measurable cost at this loop's rate.
            if i >= _MAX_CYCLE_CANDIDATES or (i % 5000 == 0 and time.monotonic() - t0 > _MAX_CYCLE_SECONDS):
                cycles_truncated = True
                break
    except Exception:
        pass

    # ── Critical bridges (articulation points of the undirected call graph) ──
    bridges = []
    try:
        und = g.to_undirected()
        arts = sorted(
            nx.articulation_points(und),
            key=lambda nid: in_deg.get(nid, 0) + out_deg.get(nid, 0),
            reverse=True,
        )
        for nid in arts[:15]:
            n = func_by_id.get(nid)
            if n:
                bridges.append({
                    **_node_brief(n),
                    "degree": in_deg.get(nid, 0) + out_deg.get(nid, 0),
                })
    except Exception:
        pass

    # ── Dead code + high-risk components (single pass over g.nodes) ──────────
    # Dead code: callables never invoked, not entry-shaped, not tests, not API handlers.
    # High risk: nodes matching 2+ of {large fan-out, heavily depended upon, high complexity}.
    entry_names = {"main", "__init__", "__main__", "setup", "run", "app", "handler"}
    dead_code = []
    high_risk = []
    for nid in g.nodes:
        n = func_by_id.get(nid)
        if not n:
            continue
        fan_out = out_deg.get(nid, 0)
        fan_in = in_deg.get(nid, 0)

        if n.kind != "type" and n.type != "class":
            meta = n.adapter_metadata or {}
            is_dead = (
                fan_in == 0
                and n.name.lower() not in entry_names and not n.name.startswith("test")
                and "test" not in n.file_path.lower()
                and meta.get("semantic_kind") not in ("EXPOSES_API", "CONSUMES_EVENT")
                and not meta.get("decorators")  # decorated functions are usually framework-invoked
            )
            if is_dead:
                dead_code.append(_node_brief(n))

        complexity = n.semantics.complexity if n.semantics else "medium"
        reasons = []
        if fan_out >= 10:
            reasons.append(f"large dependency fan-out ({fan_out})")
        if fan_in >= 15:
            reasons.append(f"heavily depended upon ({fan_in} callers)")
        if complexity in ("high", "very_high"):
            reasons.append(f"{complexity} complexity")
        if len(reasons) >= 2:
            high_risk.append({**_node_brief(n), "fan_in": fan_in, "fan_out": fan_out, "reasons": reasons})
    dead_code = dead_code[:50]
    high_risk.sort(key=lambda d: d["fan_in"] + d["fan_out"], reverse=True)
    high_risk = high_risk[:20]

    # ── Surprising cross-module coupling ──────────────────────────────────────
    # Directory pairs joined by very few call edges: thin threads between
    # otherwise-unrelated subsystems often reveal hidden architectural coupling.
    pair_edges: Dict[tuple, List[tuple]] = defaultdict(list)
    for s, t in g.edges:
        sm = _top_module_dir(func_by_id[s].file_path) if s in func_by_id else ""
        tm = _top_module_dir(func_by_id[t].file_path) if t in func_by_id else ""
        if sm and tm and sm != tm:
            pair_edges[(sm, tm)].append((s, t))
    surprising = []
    for (sm, tm), es in sorted(pair_edges.items(), key=lambda kv: len(kv[1])):
        if len(es) > 2:
            continue
        s, t = es[0]
        surprising.append({
            "from_module": sm,
            "to_module": tm,
            "edge_count": len(es),
            "example": {
                "source": func_by_id[s].name if s in func_by_id else s,
                "target": func_by_id[t].name if t in func_by_id else t,
            },
        })
        if len(surprising) >= 15:
            break

    # ── Language distribution ─────────────────────────────────────────────────
    lang_counts = Counter(
        n.source_language for n in payload.function_nodes
        if n.source_language and n.source_language != "unknown" and n.kind != "external"
    )

    # ── Per-node metrics ──────────────────────────────────────────────────────
    # Unlike god_nodes/high_risk (top-N only), this covers every callable node
    # so the interactive graph can encode importance/complexity on ANY node the
    # user expands to, not just the top 20. Cheap: just a few numbers per node.
    node_metrics = {}
    for nid in g.nodes:
        n = func_by_id.get(nid)
        if not n:
            continue
        node_metrics[nid] = {
            "pagerank": round(pagerank.get(nid, 0.0), 5),
            "in_degree": in_deg.get(nid, 0),
            "out_degree": out_deg.get(nid, 0),
            "betweenness": round(betweenness.get(nid, 0.0), 5),
            "complexity": n.semantics.complexity if n.semantics else None,
            "criticality": n.semantics.criticality if n.semantics else None,
        }

    # ── Per-file metrics (same heatmap signal, aggregated up to file level) ──
    # PageRank sums validly across a group (it's a probability mass, so a
    # file's total is "how much of the graph's importance lives here").
    # Degree sums the same way; complexity/criticality take the file's worst
    # offender, since one hotspot function makes the whole file worth a look.
    file_acc: Dict[str, dict] = {}
    for nid in g.nodes:
        n = func_by_id.get(nid)
        if not n or not n.file_path:
            continue
        acc = file_acc.setdefault(n.file_path, {
            "pagerank": 0.0, "in_degree": 0, "out_degree": 0,
            "complexity": None, "criticality": None,
        })
        acc["pagerank"] += pagerank.get(nid, 0.0)
        acc["in_degree"] += in_deg.get(nid, 0)
        acc["out_degree"] += out_deg.get(nid, 0)
        if n.semantics:
            if _COMPLEXITY_RANK.get(n.semantics.complexity, -1) > _COMPLEXITY_RANK.get(acc["complexity"], -1):
                acc["complexity"] = n.semantics.complexity
            if _CRITICALITY_RANK.get(n.semantics.criticality, -1) > _CRITICALITY_RANK.get(acc["criticality"], -1):
                acc["criticality"] = n.semantics.criticality
    file_metrics = {
        fp: {**acc, "pagerank": round(acc["pagerank"], 5)}
        for fp, acc in file_acc.items()
    }

    return {
        "graph_id": payload.graph_id,
        "stats": {
            "callable_nodes": n_nodes,
            "call_edges": n_edges,
            "total_nodes": len(payload.function_nodes),
            "modules": len(payload.module_nodes),
            "languages": dict(lang_counts.most_common()),
            "cycle_count": len(cycles),
            "cycles_truncated": cycles_truncated,
            "dead_code_count": len(dead_code),
        },
        "god_nodes": god_nodes,
        "cycles": cycles,
        "bridges": bridges,
        "dead_code": dead_code,
        "surprising_couplings": surprising,
        "high_risk": high_risk,
        "node_metrics": node_metrics,
        "file_metrics": file_metrics,
    }


def shortest_path(payload: GraphPayload, source_name: str, target_name: str) -> Optional[List[dict]]:
    """Shortest call path between two functions addressed by name (None if none)."""
    g = _call_graph(payload)
    func_by_id = {n.id: n for n in payload.function_nodes}

    def resolve(name: str) -> Optional[str]:
        lname = name.lower()
        exact = [nid for nid in g.nodes if func_by_id.get(nid) and func_by_id[nid].name == name]
        if exact:
            return exact[0]
        ci = [nid for nid in g.nodes if func_by_id.get(nid) and func_by_id[nid].name.lower() == lname]
        if ci:
            return ci[0]
        prefix = [nid for nid in g.nodes if func_by_id.get(nid) and func_by_id[nid].name.lower().startswith(lname)]
        return prefix[0] if prefix else None

    src, tgt = resolve(source_name), resolve(target_name)
    if not src or not tgt:
        return None
    try:
        path = nx.shortest_path(g, src, tgt)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None
    return [
        {"id": nid, "name": func_by_id[nid].name, "file_path": func_by_id[nid].file_path,
         "summary": (func_by_id[nid].summary or "")[:120]}
        for nid in path if nid in func_by_id
    ]
