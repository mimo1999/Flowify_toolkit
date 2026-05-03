"""Phase 7 + 8: GraphRAG retrieval and flow explanation."""
from __future__ import annotations
from typing import Dict, List, Tuple
import time

import networkx as nx

from .models import GraphPayload
from . import bob_client, learning


def _is_code_symbol(data: dict) -> bool:
    return data.get("kind") in ("function", "callable", "type") or data.get("type") in ("function", "method", "class")


def _is_invocation(data: dict) -> bool:
    return data.get("relationship") == "INVOKES" or data.get("type") == "CALLS"


def _graph_from_payload(payload: GraphPayload) -> nx.DiGraph:
    g = nx.DiGraph()
    for n in payload.function_nodes:
        g.add_node(n.id, **n.model_dump())
    for e in payload.function_edges:
        if g.has_node(e.source_id) and g.has_node(e.target_id):
            g.add_edge(e.source_id, e.target_id, type=e.type, relationship=e.relationship)
    return g


def _entry_nodes(g: nx.DiGraph, query: str, graph_id: str) -> List[str]:
    """Find entry nodes for query, enhanced with learning data."""
    candidates_by_name: Dict[str, List[str]] = {}
    for nid, d in g.nodes(data=True):
        if _is_code_symbol(d):
            candidates_by_name.setdefault(d["name"], []).append(nid)
    
    names = list(candidates_by_name.keys())
    
    # Phase 3: Get terminology suggestions from learning
    learned_suggestions = learning.get_terminology_suggestions(graph_id, query)
    
    # Combine Bob's interpretation with learned suggestions
    chosen = bob_client.interpret_query(query, names)
    
    # Add learned suggestions that aren't already in chosen
    for suggestion in learned_suggestions:
        if suggestion not in chosen:
            chosen.append(suggestion)
    
    entries: List[str] = []
    for n in chosen:
        for nid in candidates_by_name.get(n, []):
            entries.append(nid)
    
    if not entries and names:
        # fallback: take a few nodes whose summary mentions any query word
        q_tokens = {t.lower() for t in query.split() if len(t) > 2}
        for nid, d in g.nodes(data=True):
            blob = (d.get("summary") or "") + " " + (d.get("name") or "")
            if any(t in blob.lower() for t in q_tokens):
                entries.append(nid)
                if len(entries) >= 5:
                    break
    
    return entries


def retrieve_subgraph(payload: GraphPayload, query: str, max_hops: int = 2) -> Tuple[List[str], dict, str]:
    """Retrieve subgraph for query with learning tracking.
    
    Returns: (ordered_nodes, subgraph_dict, query_id)
    """
    start_time = time.time()
    
    g = _graph_from_payload(payload)
    entries = _entry_nodes(g, query, payload.graph_id)
    visited: Dict[str, int] = {}
    order: List[str] = []
    frontier = [(e, 0) for e in entries]
    while frontier:
        nid, hops = frontier.pop(0)
        if nid in visited:
            continue
        visited[nid] = hops
        order.append(nid)
        if hops >= max_hops:
            continue
        for _, tgt, d in g.out_edges(nid, data=True):
            if _is_invocation(d):
                frontier.append((tgt, hops + 1))

    # Build subgraph payload
    func_by_id = {n.id: n for n in payload.function_nodes}
    sub_nodes = [func_by_id[n].model_dump() for n in order if n in func_by_id]
    sub_edges = []
    visited_set = set(visited)
    for e in payload.function_edges:
        if e.source_id in visited_set and e.target_id in visited_set and e.relationship == "INVOKES":
            sub_edges.append(e.model_dump())
    
    # Phase 3: Record query for learning
    response_time_ms = int((time.time() - start_time) * 1000)
    query_id = learning.record_query(
        payload.graph_id,
        query,
        order,
        response_time_ms
    )
    
    return order, {"nodes": sub_nodes, "edges": sub_edges, "entries": entries}, query_id


def explain(payload: GraphPayload, query: str, ordered_ids: List[str]) -> str:
    by_id = {n.id: n for n in payload.function_nodes}
    summaries = []
    for nid in ordered_ids[:15]:
        n = by_id.get(nid)
        if not n:
            continue
        s = n.summary or n.name
        summaries.append(f"{n.name} ({n.file_path}): {s}")
    if not summaries:
        return "No relevant functions found in the graph for that query."
    return bob_client.explain_flow(query, summaries)
