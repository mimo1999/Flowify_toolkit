"""End-to-end ingestion pipeline (called from API endpoints)."""
from __future__ import annotations
import re
from pathlib import Path
from typing import Dict, List

import networkx as nx

from . import llm_provider as bob_client, storage, graph_builder, module_abstractor, git_updater, llm_ingestion, learning
from .models import (
    GraphPayload, FunctionNode, RepositoryContext, SemanticMetadata, SemanticEdge,
    FlowSummary, FlowStep, ModuleNode, ModuleEdge,
)


_CRITICALITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _is_callable(node: FunctionNode) -> bool:
    return node.kind in ("function", "callable") or node.type in ("function", "method")


_STUB_PREFIXES = ("(stub) ", "(stub)")

def _is_stub(s: str | None, node: "FunctionNode | None" = None) -> bool:
    """Return True if the summary is absent, a raw stub, or a heuristic placeholder.

    A node whose summary was produced by the heuristic provider is flagged via
    adapter_metadata["summary_source"] == "heuristic".  We treat those as stubs
    so they are overwritten when a real LLM becomes available.
    """
    if not s:
        return True
    if any(s.startswith(p) for p in _STUB_PREFIXES):
        return True
    if node is not None:
        return node.adapter_metadata.get("summary_source") == "heuristic"
    return False

def _summarize_functions(nodes: list[FunctionNode]) -> None:
    """Generate 1-line descriptions for callable nodes that lack a real summary."""
    provider = bob_client.get_provider()
    is_real_llm = not getattr(provider, "_is_heuristic", False)
    for n in nodes:
        if not _is_callable(n) or not n.code_snippet:
            continue
        if not _is_stub(n.summary, n):
            continue
        n.summary = provider.summarize_function(
            n.name,
            n.code_snippet,
            kind=n.kind or n.type or "function",
        )
        # Tag the source so future calls can detect heuristic-generated summaries
        n.adapter_metadata["summary_source"] = "llm" if is_real_llm else "heuristic"


def _analyze_semantics(
    nodes: list[FunctionNode],
    g: nx.DiGraph,
    repo_context: RepositoryContext
) -> list[SemanticEdge]:
    """Phase 2: Perform semantic analysis on function nodes.
    
    Returns list of semantic edges discovered during analysis.
    """
    print(f"[Phase 2] Performing semantic analysis on {len(nodes)} functions...")
    semantic_edges = []
    analyzed_count = 0
    callable_count = sum(1 for n in nodes if _is_callable(n))
    
    for n in nodes:
        if not _is_callable(n) or not n.code_snippet:
            continue
        
        # Get neighbors for context
        neighbors = []
        if n.id in g.nodes:
            neighbors = [g.nodes[nid].get("name", "") for nid in g.successors(n.id)][:5]
        
        # Perform semantic analysis
        semantics_dict = bob_client.analyze_function_semantics(
            n.name,
            n.code_snippet,
            repo_context.model_dump(),
            neighbors
        )
        
        # Extract semantic edges before creating metadata
        semantic_edge_dicts = semantics_dict.pop("semantic_edges", [])

        # Create semantic metadata. Bob may return values outside our Literals
        # (e.g. an unknown intent label); fall back to defaults rather than crash.
        try:
            n.semantics = SemanticMetadata(**semantics_dict)
        except Exception:
            n.semantics = SemanticMetadata(confidence=semantics_dict.get("confidence", 0.0))

        # Create semantic edges
        for edge_dict in semantic_edge_dicts:
            target_name = edge_dict.get("target_name", "")
            target_id = None
            for node in nodes:
                if node.name == target_name:
                    target_id = node.id
                    break

            if not target_id:
                continue
            try:
                semantic_edges.append(SemanticEdge(
                    type=edge_dict.get("type", "DEPENDS_ON"),
                    source_id=n.id,
                    target_id=target_id,
                    description=edge_dict.get("description"),
                    confidence=semantics_dict.get("confidence", 0.5),
                    inferred_by="bob" if semantics_dict.get("confidence", 0) > 0.5 else "heuristic"
                ))
            except Exception:
                # Skip edges with invalid type / fields rather than abort ingest.
                continue
        
        analyzed_count += 1
        if analyzed_count % 10 == 0:
            print(f"  - Analyzed {analyzed_count}/{callable_count} functions")
    
    print(f"  - Semantic analysis complete: {analyzed_count} functions, {len(semantic_edges)} semantic edges")
    return semantic_edges


# A flow summary is meant to be skimmable — cap how many modules appear in both
# the narrative and the diagram so neither becomes an unreadable wall.
_MAX_FLOW_MODULES = 18

_TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_|_test\.")


def _is_test_module(m: ModuleNode, func_by_id: Dict[str, FunctionNode]) -> bool:
    """True if a majority of a module's functions live in test files.

    Test code isn't part of the runtime data flow, so it shouldn't crowd out
    real modules in a high-level architecture view.
    """
    paths = [
        func_by_id[i].file_path
        for i in m.linked_function_ids
        if i in func_by_id and func_by_id[i].file_path
    ]
    if not paths:
        return False
    test_paths = sum(1 for p in paths if _TEST_PATH_RE.search(p))
    return test_paths > len(paths) / 2


def _select_significant_modules(
    module_nodes: List[ModuleNode],
    module_edges: List[ModuleEdge],
    func_by_id: Dict[str, FunctionNode],
    limit: int = _MAX_FLOW_MODULES,
) -> List[ModuleNode]:
    """Return the most significant runtime modules, BFS-ordered from entry points.

    Significance = entry points first, then by number of linked functions.  We
    seed the BFS frontier in significance order so the most important reachable
    modules survive the *limit* cap, then append the biggest leftovers.  Test
    modules are excluded unless there aren't enough runtime modules to fill the
    cap (they're appended last so the diagram stays focused on real flow).
    """
    by_id = {m.id: m for m in module_nodes}
    adjacency: Dict[str, List[str]] = {}
    for e in module_edges:
        adjacency.setdefault(e.source_module_id, []).append(e.target_module_id)

    def size(m: ModuleNode) -> int:
        return len(m.linked_function_ids)

    runtime = [m for m in module_nodes if not _is_test_module(m, func_by_id)]
    pool = runtime or module_nodes  # fall back if everything looks like tests

    entries = [m.id for m in sorted(pool, key=size, reverse=True) if m.is_entry_point]
    if not entries:
        entries = [m.id for m in sorted(pool, key=size, reverse=True)[:1]]
    pool_ids = {m.id for m in pool}

    ordered: List[ModuleNode] = []
    seen: set = set()
    frontier = list(entries)
    while frontier and len(ordered) < limit:
        mid = frontier.pop(0)
        if mid in seen or mid not in by_id or mid not in pool_ids:
            continue
        seen.add(mid)
        ordered.append(by_id[mid])
        # Visit larger neighbours first (runtime modules only)
        nbrs = sorted(
            (i for i in adjacency.get(mid, []) if i in pool_ids),
            key=lambda i: size(by_id[i]) if i in by_id else 0,
            reverse=True,
        )
        frontier.extend(nbrs)

    # Fill remaining slots with the biggest runtime modules not yet reached
    if len(ordered) < limit:
        for m in sorted(pool, key=size, reverse=True):
            if len(ordered) >= limit:
                break
            if m.id not in seen:
                seen.add(m.id)
                ordered.append(m)
    return ordered


def _representative_fn(m: ModuleNode, func_by_id: Dict[str, FunctionNode]) -> str:
    """Return the highest-criticality function name in a module, or ''."""
    fns = sorted(
        (func_by_id[i] for i in m.linked_function_ids if i in func_by_id),
        key=lambda n: _CRITICALITY_RANK.get(n.semantics.criticality if n.semantics else "low", 0),
        reverse=True,
    )
    return fns[0].name if fns else ""


def _disambiguate_labels(
    selected: List[ModuleNode],
    func_by_id: Dict[str, FunctionNode],
) -> Dict[str, str]:
    """Build unique, human-readable labels for modules whose names collide.

    Module names are directory-derived and often repeat (e.g. many "backend/app").
    A bare diagram of nine identical labels is useless, so duplicated names are
    enriched with the module's most critical function ("backend/app · ingest");
    any residual collisions get a short id suffix.
    """
    from collections import Counter
    counts = Counter(m.name for m in selected)
    labels: Dict[str, str] = {}
    used: set = set()
    for m in selected:
        label = m.name
        if counts[m.name] > 1:
            rep = _representative_fn(m, func_by_id)
            if rep:
                label = f"{m.name} · {rep}"
        final, n = label, 2
        while final in used:
            final = f"{label} ({m.id[-4:]})" if n == 2 else f"{label} #{n}"
            n += 1
        used.add(final)
        labels[m.id] = final
    return labels


def _module_mermaid(
    selected: List[ModuleNode],
    module_edges: List[ModuleEdge],
    labels: Dict[str, str],
) -> str:
    """Render a high-level, deterministic Mermaid flowchart for *selected* modules.

    Built purely from the module graph so it can never show a dependency that
    doesn't exist in module_edges.  Only edges between selected modules are drawn,
    keeping the diagram high-level.  Entry-point modules get a distinct class.
    """
    def sid(mid: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", mid)[:40]

    selected_ids = {m.id for m in selected}
    lines = ["flowchart TD"]
    for m in selected:
        label = labels.get(m.id, m.name).replace('"', "'")
        cls = ":::entry" if m.is_entry_point else ""
        lines.append(f'    {sid(m.id)}["{label}"]{cls}')
    for e in module_edges:
        if e.source_module_id in selected_ids and e.target_module_id in selected_ids:
            lines.append(f"    {sid(e.source_module_id)} --> {sid(e.target_module_id)}")
    lines.append("    classDef entry fill:#10b981,color:#fff,stroke:#059669")
    return "\n".join(lines)


def _flow_evidence(
    payload: GraphPayload,
    repo_context: RepositoryContext,
    selected: List[ModuleNode],
    labels: Dict[str, str],
) -> dict:
    """Assemble the compact, module-level evidence pack for the LLM.

    Keeps token cost bounded by working over the *selected* modules only and
    attaching just each module's top-5 functions by criticality.  Modules are
    keyed by their disambiguated *label* so same-named modules stay distinct.
    """
    func_by_id = {n.id: n for n in payload.function_nodes}

    modules = []
    for m in selected:
        fns = sorted(
            (func_by_id[i] for i in m.linked_function_ids if i in func_by_id),
            key=lambda n: _CRITICALITY_RANK.get(
                n.semantics.criticality if n.semantics else "low", 0
            ),
            reverse=True,
        )[:5]
        modules.append({
            "module": labels.get(m.id, m.name),
            "description": m.description,
            "is_entry": m.is_entry_point,
            "key_functions": [
                {
                    "name": f.name,
                    "summary": f.summary or "",
                    "intent": f.semantics.intent if f.semantics else None,
                }
                for f in fns
            ],
        })

    return {
        "purpose": repo_context.purpose,
        "project_type": repo_context.project_type,
        "domain": repo_context.domain,
        "tech_stack": repo_context.tech_stack,
        "modules": modules,
    }


def _build_flow_summary(
    graph_id: str,
    repo_path: str,
    payload: GraphPayload,
    repo_context: RepositoryContext,
) -> FlowSummary:
    """Produce and return the FlowSummary (prose via LLM, diagram deterministic)."""
    func_by_id = {n.id: n for n in payload.function_nodes}
    selected = _select_significant_modules(payload.module_nodes, payload.module_edges, func_by_id)
    labels = _disambiguate_labels(selected, func_by_id)
    evidence = _flow_evidence(payload, repo_context, selected, labels)
    provider = bob_client.get_provider()
    fallback_used = getattr(provider, "_is_heuristic", False)

    llm = bob_client.summarize_repository_flow(evidence)
    module_flows = llm.get("module_flows", {}) or {}

    flows = [
        FlowStep(
            step=i + 1,
            module=m["module"],
            description=module_flows.get(m["module"]) or m.get("description", ""),
            key_functions=[f["name"] for f in m["key_functions"]],
        )
        for i, m in enumerate(evidence["modules"])
    ]

    return FlowSummary(
        graph_id=graph_id,
        repo_path=repo_path,
        what=llm.get("what") or repo_context.purpose,
        usecase=llm.get("usecase", ""),
        flows=flows,
        architecture_mermaid=_module_mermaid(selected, payload.module_edges, labels),
        tech_stack=repo_context.tech_stack,
        techniques=llm.get("techniques", []) or [],
        fallback_used=fallback_used,
    )


def ingest(repo_path: str) -> GraphPayload:
    # Phase 1: Analyze repository context before ingestion
    print(f"[Phase 1] Analyzing repository: {repo_path}")
    repo_context_dict = bob_client.analyze_repository(repo_path)
    try:
        repo_context = RepositoryContext(**repo_context_dict)
    except Exception as e:
        print(f"  - repo context validation failed ({e}); falling back to defaults")
        repo_context = RepositoryContext(fallback_used=True)
    
    print(f"  - Project type: {repo_context.project_type}")
    print(f"  - Domain: {repo_context.domain}")
    print(f"  - Architecture: {repo_context.architecture}")
    print(f"  - Tech stack: {', '.join(repo_context.tech_stack) if repo_context.tech_stack else 'unknown'}")
    print(f"  - Confidence: {repo_context.confidence:.2f}")
    print(f"  - Fallback used: {repo_context.fallback_used}")
    
    # Build function graph with context awareness
    g, function_nodes, function_edges = graph_builder.build_function_graph(repo_path)
    _summarize_functions(function_nodes)
    
    # Phase 2: Semantic analysis
    semantic_edges = _analyze_semantics(function_nodes, g, repo_context)
    
    # Push summaries and semantics back into the networkx graph
    for n in function_nodes:
        if n.id in g.nodes:
            g.nodes[n.id]["summary"] = n.summary
            if n.semantics:
                g.nodes[n.id]["intent"] = n.semantics.intent
                g.nodes[n.id]["complexity"] = n.semantics.complexity
                g.nodes[n.id]["criticality"] = n.semantics.criticality
    
    # Build modules with entry point detection and control flow analysis
    module_nodes, module_edges, mod_to_funcs = module_abstractor.build_modules(
        g,
        function_nodes=function_nodes,
        function_edges=function_edges,
        declared_entry_points=repo_context.key_entry_points,
    )

    graph_id = storage.new_graph_id()
    payload = GraphPayload(
        graph_id=graph_id,
        repo_path=repo_path,
        function_nodes=function_nodes,
        function_edges=function_edges,
        module_nodes=module_nodes,
        module_edges=module_edges,
        module_to_functions=mod_to_funcs,
        semantic_edges=semantic_edges,
    )
    storage.save(payload)

    # Store repository context metadata
    storage.store_meta(graph_id, "repo_context", repo_context.model_dump())

    llm_result, llm_prompt = llm_ingestion.ingest_ast_results(
        repo_context,
        function_nodes,
        function_edges,
        module_nodes,
        mod_to_funcs,
        semantic_edges,
    )
    llm_result.graph_id = graph_id
    llm_result.repo_path = repo_path
    storage.store_meta(graph_id, "llm_ingestion", llm_result.model_dump())
    storage.store_meta(graph_id, "llm_ingestion_prompt", {"prompt": llm_prompt, "version": llm_result.prompt_version})
    
    # Track current git head for incremental updates.
    _, head = git_updater.changed_files_since(repo_path, None)
    if head:
        storage.store_meta(graph_id, "git", {"head": head})

    # Fix 1: Pre-seed terminology map so the FIRST query benefits from it.
    try:
        learning.seed_terminology_from_graph(graph_id)
    except Exception:
        pass  # Non-critical — ingestion must not fail because of learning

    # Generate the repository flow summary (prose via LLM, diagram deterministic).
    try:
        flow_summary = _build_flow_summary(graph_id, repo_path, payload, repo_context)
        storage.store_meta(graph_id, "flow_summary", flow_summary.model_dump())
    except Exception as e:
        print(f"  - flow summary generation failed ({e}); skipping")

    return payload


def update(graph_id: str) -> GraphPayload | None:
    payload = storage.load(graph_id)
    if payload is None:
        return None
    meta = storage.load_meta(graph_id, "git") or {}
    last = meta.get("head")
    changed, new_head = git_updater.changed_files_since(payload.repo_path, last)
    if not changed:
        return payload
    # Re-ingest changed files only.
    repo_root = Path(payload.repo_path).resolve()
    keep_nodes = [n for n in payload.function_nodes if n.file_path not in changed]
    kept_ids = {n.id for n in keep_nodes}
    keep_edges = [
        e for e in payload.function_edges
        if e.source_id in kept_ids and e.target_id in kept_ids
    ]
    new_nodes, new_edges = [], []
    for rel in changed:
        fp = repo_root / rel
        if fp.exists():
            ns, es = graph_builder.parse_file(fp, repo_root)
            new_nodes.extend(ns)
            new_edges.extend(es)

    # Re-resolve symbol edges using the merged node set.
    merged_nodes = keep_nodes + new_nodes
    name_index: Dict[str, list[str]] = {}
    for n in merged_nodes:
        name_index.setdefault(n.name, []).append(n.id)
    resolved_new = []
    for e in new_edges:
        if e.target_id.startswith("<symbol>::"):
            short = e.target_id.split("::", 1)[1]
            for cid in name_index.get(short, []):
                if cid != e.source_id:
                    resolved_new.append(type(e)(
                        type=e.type,
                        relationship=e.relationship,
                        source_id=e.source_id,
                        target_id=cid,
                        adapter_metadata=e.adapter_metadata,
                    ))
        elif not e.target_id.startswith("<"):
            resolved_new.append(e)

    merged_edges = keep_edges + resolved_new
    _summarize_functions([n for n in new_nodes if _is_callable(n)])

    # Build graph for semantic analysis
    g = nx.DiGraph()
    for n in merged_nodes:
        g.add_node(n.id, **n.model_dump())
    for e in merged_edges:
        if g.has_node(e.source_id) and g.has_node(e.target_id):
            g.add_edge(e.source_id, e.target_id, type=e.type, relationship=e.relationship)
    
    # Phase 2: Semantic analysis on new/changed nodes
    repo_context_dict = storage.load_meta(graph_id, "repo_context") or {}
    try:
        repo_context = RepositoryContext(**repo_context_dict) if repo_context_dict else RepositoryContext(fallback_used=True)
    except Exception:
        repo_context = RepositoryContext(fallback_used=True)
    if repo_context_dict:
        new_semantic_edges = _analyze_semantics(new_nodes, g, repo_context)
        keep_semantic_edges = [
            e for e in payload.semantic_edges
            if e.source_id in kept_ids and e.target_id in kept_ids
        ]
        merged_semantic_edges = keep_semantic_edges + new_semantic_edges
    else:
        merged_semantic_edges = []
    
    # Push semantics to graph
    for n in merged_nodes:
        if n.id in g.nodes:
            g.nodes[n.id]["summary"] = n.summary
            if n.semantics:
                g.nodes[n.id]["intent"] = n.semantics.intent
                g.nodes[n.id]["complexity"] = n.semantics.complexity
                g.nodes[n.id]["criticality"] = n.semantics.criticality

    # Build modules with entry point detection and control flow analysis
    module_nodes, module_edges, mod_to_funcs = module_abstractor.build_modules(
        g,
        function_nodes=merged_nodes,
        function_edges=merged_edges,
        declared_entry_points=repo_context.key_entry_points,
    )
    payload = GraphPayload(
        graph_id=graph_id,
        repo_path=payload.repo_path,
        function_nodes=merged_nodes,
        function_edges=merged_edges,
        module_nodes=module_nodes,
        module_edges=module_edges,
        module_to_functions=mod_to_funcs,
        semantic_edges=merged_semantic_edges,
    )
    storage.save(payload)
    llm_result, llm_prompt = llm_ingestion.ingest_ast_results(
        repo_context,
        merged_nodes,
        merged_edges,
        module_nodes,
        mod_to_funcs,
        merged_semantic_edges,
    )
    llm_result.graph_id = graph_id
    llm_result.repo_path = payload.repo_path
    storage.store_meta(graph_id, "llm_ingestion", llm_result.model_dump())
    storage.store_meta(graph_id, "llm_ingestion_prompt", {"prompt": llm_prompt, "version": llm_result.prompt_version})
    if new_head:
        storage.store_meta(graph_id, "git", {"head": new_head})
    return payload
