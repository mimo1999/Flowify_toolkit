"""End-to-end ingestion pipeline (called from API endpoints)."""
from __future__ import annotations
from pathlib import Path
from typing import Dict

import networkx as nx

from . import llm_provider as bob_client, storage, graph_builder, module_abstractor, git_updater, llm_ingestion
from .models import GraphPayload, FunctionNode, RepositoryContext, SemanticMetadata, SemanticEdge


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
