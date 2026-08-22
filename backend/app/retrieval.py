"""Phase 7 + 8: GraphRAG retrieval and flow explanation."""
from __future__ import annotations
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
import math
import re
import time

import networkx as nx

from .models import GraphPayload, ExecutionStep
from . import llm_provider as bob_client, learning


# ---------------------------------------------------------------------------
# Semantic synonym table — expands query tokens into code-domain equivalents
# so "authentication" finds nodes named verify_token, check_credentials, etc.
# ---------------------------------------------------------------------------
_SYNONYMS: Dict[str, List[str]] = {
    "auth":         ["authenticate", "authorization", "login", "verify", "credential", "token", "session", "jwt"],
    "authenticat":  ["login", "verify", "credential", "token", "session", "jwt", "password"],
    "login":        ["signin", "authenticate", "session", "credential", "user"],
    "password":     ["credential", "hash", "bcrypt", "verify", "salt"],
    "user":         ["account", "profile", "member", "customer", "person", "principal"],
    "request":      ["http", "endpoint", "route", "handler", "controller", "api"],
    "api":          ["endpoint", "route", "handler", "controller", "rest", "http", "url"],
    "endpoint":     ["route", "handler", "api", "url", "path"],
    "database":     ["db", "sql", "query", "repository", "dao", "store", "persist", "orm"],
    "db":           ["sql", "query", "repository", "persist", "store", "model"],
    "query":        ["search", "filter", "find", "fetch", "retrieve", "select"],
    "search":       ["find", "query", "lookup", "retrieve", "fetch", "filter"],
    "error":        ["exception", "failure", "invalid", "reject", "raise", "catch", "handle"],
    "exception":    ["error", "failure", "invalid", "reject"],
    "create":       ["new", "insert", "add", "build", "make", "register", "init"],
    "delete":       ["remove", "drop", "destroy", "cleanup", "purge"],
    "update":       ["modify", "change", "set", "edit", "patch", "save"],
    "send":         ["emit", "publish", "dispatch", "notify", "push", "fire"],
    "event":        ["emit", "publish", "dispatch", "notify", "subscribe", "listen"],
    "validate":     ["check", "verify", "ensure", "assert", "sanitize", "parse"],
    "parse":        ["decode", "deserialize", "read", "extract", "transform"],
    "cache":        ["store", "memory", "buffer", "redis", "memcache", "ttl"],
    "file":         ["path", "directory", "folder", "disk", "read", "write", "io"],
    "ingest":       ["import", "load", "process", "pipeline", "scan"],
    "graph":        ["node", "edge", "network", "tree", "traverse"],
    "test":         ["assert", "verify", "check", "mock", "spec"],
    "config":       ["settings", "options", "env", "parameter", "setup"],
    "connect":      ["open", "init", "setup", "establish", "pool"],
    "close":        ["disconnect", "cleanup", "teardown", "finalize", "shutdown"],
    "load":         ["read", "import", "fetch", "deserialize", "restore"],
    "save":         ["write", "persist", "store", "serialize", "dump"],
    "render":       ["display", "format", "output", "template", "view"],
    "process":      ["handle", "transform", "pipeline", "run", "execute"],
    "model":        ["schema", "entity", "struct", "class", "type"],
    "token":        ["jwt", "session", "credential", "bearer", "access"],
    "hash":         ["encrypt", "bcrypt", "sha", "md5", "digest", "checksum"],
    "log":          ["logger", "audit", "trace", "debug", "monitor"],
    "upload":       ["file", "multipart", "stream", "blob", "store"],
    "download":     ["fetch", "stream", "read", "export", "get"],
    "migrate":      ["schema", "upgrade", "version", "alembic", "flyway"],
    "notify":       ["email", "webhook", "push", "alert", "message"],
    "payment":      ["charge", "billing", "stripe", "invoice", "order"],
    "image":        ["photo", "resize", "compress", "thumbnail", "media"],
}


def _tokenize(text: str) -> List[str]:
    """Split identifier text on word-boundaries, underscores, dots, and camelCase."""
    parts = re.findall(r"[A-Za-z][a-z0-9]*|[0-9]+", text)
    return [p.lower() for p in parts if len(p) > 1]


def _expand_tokens(raw_tokens: List[str]) -> set:
    """Return the original tokens plus their code-domain synonyms."""
    expanded: set = set(raw_tokens)
    for t in raw_tokens:
        # Exact lookup
        if t in _SYNONYMS:
            expanded.update(_SYNONYMS[t])
        # Prefix / substring lookup (e.g. "authenticat" covers "authenticate")
        for key, syns in _SYNONYMS.items():
            if len(t) >= 5 and (key.startswith(t) or t.startswith(key)):
                expanded.update(syns)
                expanded.add(key)
    return expanded


def _build_idf(node_docs: List[str]) -> Dict[str, float]:
    """Compute log-IDF for every token seen across *node_docs*.

    IDF(t) = log((N+1) / (df(t)+1)) + 1   (Scikit-learn smooth variant)
    High IDF → rare, discriminative term.  Low IDF → ubiquitous (get/set/init).
    """
    N = len(node_docs)
    df: Dict[str, int] = defaultdict(int)
    for doc in node_docs:
        for tok in set(_tokenize(doc)):
            df[tok] += 1
    return {t: math.log((N + 1) / (cnt + 1)) + 1.0 for t, cnt in df.items()}


def _score_node(d: dict, query_tokens: set, idf: Dict[str, float]) -> float:
    """Multi-signal relevance score: name (3×) + summary (1.5×) + path (0.5×) + intent (1×)."""
    name_toks    = set(_tokenize(d.get("name", "")))
    summary_toks = set(_tokenize(d.get("summary") or ""))
    path_toks    = set(_tokenize(d.get("file_path") or ""))
    semantics    = d.get("semantics") or {}
    if isinstance(semantics, dict):
        intent = semantics.get("intent", "")
    else:
        intent = getattr(semantics, "intent", "") or ""
    intent_toks  = set(_tokenize(intent))

    hits_name    = query_tokens & name_toks
    hits_summary = query_tokens & summary_toks
    hits_path    = query_tokens & path_toks
    hits_intent  = query_tokens & intent_toks

    score  = sum(3.0 * idf.get(t, 1.0) for t in hits_name)
    score += sum(1.5 * idf.get(t, 1.0) for t in hits_summary)
    score += sum(0.5 * idf.get(t, 1.0) for t in hits_path)
    score += sum(1.0 * idf.get(t, 1.0) for t in hits_intent)
    return score


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
    """Find entry nodes using multi-signal TF-IDF scoring + semantic synonym expansion.

    Pipeline:
    1. Expand query tokens with code-domain synonyms (auth → jwt, token, …)
    2. Score every node: name (3×) + summary (1.5×) + path (0.5×) via TF-IDF
    3. Pre-select top-30 candidates and pass them WITH summaries to the LLM
       so it can make semantic judgements, not just name matches
    4. Merge LLM selections with the top-5 scored nodes so we always have seeds
    """
    # ── 1. Collect code-symbol nodes ─────────────────────────────────────────
    code_nodes: Dict[str, dict] = {}          # nid → node data
    for nid, d in g.nodes(data=True):
        if _is_code_symbol(d):
            code_nodes[nid] = d

    if not code_nodes:
        return []

    # ── 2. Expand query tokens with synonyms ─────────────────────────────────
    raw_tokens = _tokenize(query)
    query_tokens = _expand_tokens(raw_tokens)

    # ── 3. Compute IDF across all node documents ──────────────────────────────
    node_docs = [
        (d.get("name") or "") + " " + (d.get("summary") or "") + " " + (d.get("file_path") or "")
        for d in code_nodes.values()
    ]
    idf = _build_idf(node_docs)

    # ── 4. Score and rank all nodes ───────────────────────────────────────────
    scored: List[Tuple[float, str]] = sorted(
        ((_score_node(d, query_tokens, idf), nid) for nid, d in code_nodes.items()),
        reverse=True,
    )

    # ── 5. Build shortlist of top-30 candidates for the LLM ──────────────────
    top_nids = [nid for _, nid in scored[:30]]
    name_to_nids: Dict[str, List[str]] = {}
    for nid in top_nids:
        name = code_nodes[nid].get("name", nid)
        name_to_nids.setdefault(name, []).append(nid)

    # Summary context lets the LLM match semantically (not just by name)
    context: Dict[str, str] = {
        code_nodes[nid].get("name", nid): (code_nodes[nid].get("summary") or "")[:80]
        for nid in top_nids
    }

    # ── 6. LLM re-ranks the shortlist ────────────────────────────────────────
    learned = learning.get_terminology_suggestions(graph_id, query)
    chosen_names = bob_client.interpret_query(query, list(name_to_nids.keys()), context)
    for s in learned:
        if s not in chosen_names:
            chosen_names.append(s)

    # ── 7. Collect node IDs: LLM selections first ────────────────────────────
    entries: List[str] = []
    seen: set = set()
    for name in chosen_names:
        for nid in name_to_nids.get(name, []):
            if nid not in seen:
                entries.append(nid)
                seen.add(nid)

    # ── 8. Always seed with the top-5 TF-IDF scorers that have score > 0 ─────
    # This ensures we get results even when the LLM picks nothing useful.
    for score, nid in scored[:5]:
        if score > 0 and nid not in seen:
            entries.append(nid)
            seen.add(nid)

    return entries


def retrieve_subgraph(payload: GraphPayload, query: str, max_hops: int = 2) -> Tuple[List[str], dict, str, nx.DiGraph, Dict[str, str]]:
    """Retrieve subgraph for query with learning tracking.

    Returns: (ordered_nodes, subgraph_dict, query_id, graph)
    """
    start_time = time.time()

    g = _graph_from_payload(payload)
    entries = _entry_nodes(g, query, payload.graph_id)
    visited: Dict[str, int] = {}
    # BFS discovery parent — the node whose call actually reached this one,
    # not just "whatever came before it in `order`". BFS visits nodes level
    # by level, so a node's immediate predecessor in `order` is frequently a
    # sibling with no edge to it at all; only `parent` reflects a real edge.
    # explain()/build_execution_steps() key off this, not list adjacency.
    parent: Dict[str, str] = {}
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
                if tgt not in visited and tgt not in parent:
                    parent[tgt] = nid
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

    return order, {"nodes": sub_nodes, "edges": sub_edges, "entries": entries}, query_id, g, parent


def build_execution_steps(
    payload: GraphPayload,
    ordered_ids: List[str],
    g: nx.DiGraph,
    parent_map: Optional[Dict[str, str]] = None,
) -> List[ExecutionStep]:
    """Build ordered, structured execution steps from traversal results."""
    by_id = {n.id: n for n in payload.function_nodes}
    steps: List[ExecutionStep] = []
    parent_map = parent_map or {}

    for nid in ordered_ids[:20]:
        n = by_id.get(nid)
        if not n:
            continue
        # Determine semantic kind from adapter_metadata
        semantic_kind = n.adapter_metadata.get("semantic_kind", "CALLS")
        if n.semantics:
            # Override with LLM-derived intent where we can map it
            intent_to_kind = {
                "persistence": "USES_DB",
                "retrieval": "USES_DB",
                "orchestration": "CALLS",
            }
            semantic_kind = intent_to_kind.get(n.semantics.intent, semantic_kind)

        # Find what edge type leads from previous step to this one — keyed
        # off the BFS discovery parent (a real edge), not the previous item
        # in `ordered_ids`: BFS visits siblings before children, so the
        # list's immediate predecessor is frequently unconnected to `nid`.
        edge_label = None
        prev_id = parent_map.get(nid)
        if prev_id and g.has_edge(prev_id, nid):
            edge_data = g.get_edge_data(prev_id, nid) or {}
            edge_label = edge_data.get("type", "CALLS")

        steps.append(ExecutionStep(
            id=nid,
            name=n.name,
            file_path=n.file_path,
            semantic_kind=semantic_kind,
            intent=n.semantics.intent if n.semantics else None,
            summary=n.summary,
            edge_label=edge_label,
        ))
    return steps


def explain(
    payload: GraphPayload,
    query: str,
    ordered_ids: List[str],
    prior_turns: Optional[List[dict]] = None,
    g: Optional[nx.DiGraph] = None,
    parent_map: Optional[Dict[str, str]] = None,
) -> str:
    by_id = {n.id: n for n in payload.function_nodes}
    summaries = []
    edge_info = []
    parent_map = parent_map or {}

    # Build graph for edge relationship info, unless the caller already has
    # one from retrieve_subgraph (same repo, same call) — avoid rebuilding it.
    if g is None:
        g = _graph_from_payload(payload)

    for nid in ordered_ids[:15]:
        n = by_id.get(nid)
        if not n:
            continue
        s = n.summary or n.name
        semantic_kind = n.adapter_metadata.get("semantic_kind", "CALLS")
        intent = n.semantics.intent if n.semantics else "unknown"
        summaries.append(
            f"{n.name} ({n.file_path}) [{semantic_kind}/{intent}]: {s}"
        )
        # Keyed off the real BFS discovery parent, not list position — see
        # build_execution_steps for why list-adjacency doesn't hold for BFS.
        prev_id = parent_map.get(nid)
        if prev_id and g.has_edge(prev_id, nid):
            ed = g.get_edge_data(prev_id, nid) or {}
            prev_node = by_id.get(prev_id)
            if prev_node:
                edge_info.append(f"  {prev_node.name} --[{ed.get('type','CALLS')}]--> {n.name}")

    if not summaries:
        return "No relevant functions found in the graph for that query."
    return bob_client.explain_flow_with_graph(query, summaries, edge_info, prior_turns)
