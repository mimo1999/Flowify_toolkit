"""FastAPI entrypoint — Phase 9 + MCP Integration."""
from __future__ import annotations
import hashlib
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import bob_export, pipeline, storage, retrieval, module_abstractor, learning
from .models import (
    BobGraphRequest, IngestRequest, UpdateRequest, QueryRequest, QueryResponse,
    FeedbackRequest, MCPIngestResponse, MCPQueryResponse,
    ImpactAnalysis,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = FastAPI(title="Flowify AI", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.get("/")
def root():
    return {"service": "flowify", "graphs": storage.list_graphs()}


@app.get("/provider_info")
def provider_info():
    """Return the active LLM provider so the UI can show a stub warning."""
    from . import llm_provider
    p = llm_provider.get_provider()
    name = type(p).__name__.replace("Provider", "").lower()
    # Ollama: surface the model name in the badge
    extra = {}
    if name == "ollama":
        extra["model"] = getattr(p, "model", "")
        extra["host"]  = getattr(p, "host", "http://localhost:11434")
    return {
        "provider": name,
        "is_stub": name == "heuristic",
        "display_name": {
            "heuristic": "No LLM (stub mode)",
            "ollama":    f"Ollama ({extra.get('model', 'local')})",
            "bob":       "IBM watsonx / Bob",
            "anthropic": "Anthropic Claude",
            "openai":    "OpenAI",
            "copilot":   "GitHub Copilot",
            "openclaw":  "OpenClaw",
        }.get(name, name),
        **extra,
    }


def _generate_repo_id(repo_path: str, custom_id: str | None = None) -> str:
    """Generate stable repo_id from path or use custom ID."""
    if custom_id:
        return custom_id
    return hashlib.sha256(repo_path.encode()).hexdigest()[:12]


def _serialize_payload(payload):
    """Return (func_by_id_dict, function_edges_list) ready for module_abstractor helpers."""
    func_by_id = {n.id: n.model_dump() for n in payload.function_nodes}
    function_edges = [e.model_dump() for e in payload.function_edges]
    return func_by_id, function_edges


@app.post("/ingest_repo")
def ingest_repo(req: IngestRequest):
    """Legacy ingest endpoint - maintained for backward compatibility."""
    payload = pipeline.ingest(req.repo_path)
    
    # Load repository context
    repo_context = storage.load_meta(payload.graph_id, "repo_context")
    
    response = {
        "graph_id": payload.graph_id,
        "function_count": len(payload.function_nodes),
        "module_count": len(payload.module_nodes),
    }
    
    # Include repository context if available
    if repo_context:
        response["repo_context"] = repo_context
    
    return response


@app.post("/mcp/ingest", response_model=MCPIngestResponse)
def mcp_ingest_repo(req: IngestRequest):
    """MCP-optimized ingest endpoint with stable repo_id and normalized response.
    
    This endpoint provides:
    - Stable repo_id for idempotent operations
    - Consistent error format
    - Structured response for MCP tools
    """
    try:
        logger.info(f"MCP ingest request for repo: {req.repo_path}")
        
        # Generate stable repo_id
        repo_id = _generate_repo_id(req.repo_path, req.repo_id)
        
        # Check if already ingested (idempotent).
        for graph_id in storage.list_graphs():
            payload = storage.load_light(graph_id)
            if payload and payload.repo_path == req.repo_path:
                logger.info(f"Repository already ingested with graph_id: {graph_id}")
                repo_context = storage.load_meta(graph_id, "repo_context")
                return MCPIngestResponse(
                    success=True,
                    graph_id=graph_id,
                    repo_id=repo_id,
                    repo_path=req.repo_path,
                    function_count=len(payload.function_nodes),
                    module_count=len(payload.module_nodes),
                    repo_context=repo_context
                )
        
        # Perform ingestion
        payload = pipeline.ingest(req.repo_path)
        repo_context = storage.load_meta(payload.graph_id, "repo_context")
        
        logger.info(f"Ingestion complete: graph_id={payload.graph_id}, functions={len(payload.function_nodes)}")
        
        return MCPIngestResponse(
            success=True,
            graph_id=payload.graph_id,
            repo_id=repo_id,
            repo_path=req.repo_path,
            function_count=len(payload.function_nodes),
            module_count=len(payload.module_nodes),
            repo_context=repo_context
        )
        
    except Exception as e:
        logger.error(f"Ingestion failed for {req.repo_path}: {str(e)}", exc_info=True)
        return MCPIngestResponse(
            success=False,
            graph_id="",
            repo_id=_generate_repo_id(req.repo_path, req.repo_id),
            repo_path=req.repo_path,
            function_count=0,
            module_count=0,
            error=str(e)
        )


@app.post("/bob/graph")
def build_graph_for_bob(req: BobGraphRequest):
    """Ingest a repo root and return a Bob-ready generated graph.

    This endpoint is intended for Bob agents, MCP servers, and external repo
    tooling that need the graph payload immediately instead of a graph id to
    query through the Flowify UI endpoints.
    """
    try:
        return bob_export.build_bob_graph_response(
            req.repo_path,
            depth=req.depth,
            include_full_graph=req.include_full_graph,
            include_view=req.include_view,
            include_llm_ingestion=req.include_llm_ingestion,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/entry_points")
def get_entry_points(graph_id: str, max_count: int = 4):
    """Return up to `max_count` inferred entry-point files for the initial view."""
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    func_by_id, function_edges = _serialize_payload(payload)
    declared = (storage.load_meta(graph_id, "repo_context") or {}).get("key_entry_points", [])
    nodes = module_abstractor.find_entry_files(
        func_by_id, function_edges, declared_entry_points=declared, max_count=max_count,
    )
    return {"graph_id": graph_id, "nodes": nodes, "edges": []}


@app.get("/expand")
def expand_graph_node(graph_id: str, node_id: str, action: str = "callees"):
    """Lazy-expand a node — returns its direct children and relevant edges.

    For file-level nodes (id starts with `file::`):
      action=callees   → show files this file calls
      action=functions → drill into the file's symbols
    """
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    func_by_id, function_edges = _serialize_payload(payload)

    if node_id.startswith("file::"):
        return module_abstractor.expand_file_node(
            func_by_id, function_edges, node_id[len("file::"):], action=action,
        )
    return module_abstractor.expand_node(
        payload.module_nodes, func_by_id, function_edges, node_id,
    )


@app.get("/graph")
def get_graph(graph_id: str, depth: int = 1):
    """Get graph at specified depth with enhanced module metadata.

    Depth 1: Shows modules with entry point tagging and control flow summaries
    Depth 2: Shows file-level submodules
    Depth 3: Shows individual functions
    """
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    depth = max(1, min(3, depth))
    func_by_id, function_edges = _serialize_payload(payload)
    view = module_abstractor.collapse_for_depth(
        payload.module_nodes, payload.module_edges,
        payload.module_to_functions, func_by_id, depth, function_edges,
    )
    view["graph_id"] = graph_id
    view["repo_path"] = payload.repo_path
    
    # Add module metadata summary
    entry_modules = [m for m in payload.module_nodes if m.is_entry_point]
    view["metadata"] = {
        "total_modules": len(payload.module_nodes),
        "entry_point_modules": len(entry_modules),
        "modules_with_control_flow": len([m for m in payload.module_nodes if m.control_flow_groups]),
    }
    
    return view


@app.post("/query", response_model=QueryResponse)
def query_graph(req: QueryRequest):
    """Query endpoint — returns graph-grounded explanation + structured execution path."""
    payload = storage.load_light(req.graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")

    # Retrieve with learning tracking; graph returned for step building
    ordered, sub, query_id, g = retrieval.retrieve_subgraph(payload, req.query, max_hops=req.depth)
    explanation = retrieval.explain(payload, req.query, ordered)
    execution_steps = retrieval.build_execution_steps(payload, ordered, g)

    return QueryResponse(
        explanation=explanation,
        subgraph=sub,
        path=ordered,
        query_id=query_id,
        execution_steps=execution_steps,
        graph_nodes_consulted=len(ordered),
    )


@app.post("/mcp/query", response_model=MCPQueryResponse)
def mcp_query_repo(req: QueryRequest):
    """MCP-optimized query endpoint with normalized response.
    
    This endpoint provides:
    - Consistent error format
    - Structured function metadata
    - Clear execution path
    """
    try:
        logger.info(f"MCP query request: graph_id={req.graph_id}, query={req.query}")
        
        # Validate graph exists
        payload = storage.load_light(req.graph_id)
        if payload is None:
            return MCPQueryResponse(
                success=False,
                graph_id=req.graph_id,
                query=req.query,
                explanation="",
                error="Graph not found"
            )
        
        # Retrieve subgraph with learning tracking
        ordered, sub, query_id, _g = retrieval.retrieve_subgraph(payload, req.query, max_hops=req.depth)
        explanation = retrieval.explain(payload, req.query, ordered)
        
        # Build enriched function list
        func_by_id = {n.id: n for n in payload.function_nodes}
        relevant_functions = []
        for node_id in ordered[:20]:  # Limit to top 20 for MCP response
            node = func_by_id.get(node_id)
            if node:
                func_data = {
                    "id": node.id,
                    "name": node.name,
                    "file_path": node.file_path,
                    "type": node.type,
                    "summary": node.summary or "",
                }
                # Add semantic metadata if available
                if node.semantics:
                    func_data["intent"] = node.semantics.intent
                    func_data["complexity"] = node.semantics.complexity
                    func_data["criticality"] = node.semantics.criticality
                relevant_functions.append(func_data)
        
        logger.info(f"Query complete: {len(relevant_functions)} functions retrieved")
        
        return MCPQueryResponse(
            success=True,
            graph_id=req.graph_id,
            query=req.query,
            explanation=explanation,
            relevant_functions=relevant_functions,
            execution_path=ordered,
            query_id=query_id
        )
        
    except Exception as e:
        logger.error(f"Query failed for graph_id={req.graph_id}: {str(e)}", exc_info=True)
        return MCPQueryResponse(
            success=False,
            graph_id=req.graph_id,
            query=req.query,
            explanation="",
            error=str(e)
        )


@app.get("/impact", response_model=ImpactAnalysis)
def get_impact(graph_id: str, node_id: str):
    """Change-impact analysis for a given node.

    Returns:
      - callers: functions that directly call this node
      - callees: functions this node calls
      - db_interactions: DB-touching callees
      - affected_modules: module names that would be impacted
      - risk_level: low / medium / high / critical
    """
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")

    func_by_id = {n.id: n for n in payload.function_nodes}
    target = func_by_id.get(node_id)
    if target is None:
        raise HTTPException(404, "node not found")

    # Build adjacency structures
    callers_map: dict[str, list[str]] = {}
    callees_map: dict[str, list[str]] = {}
    for e in payload.function_edges:
        if e.relationship == "INVOKES" or e.type == "CALLS":
            callees_map.setdefault(e.source_id, []).append(e.target_id)
            callers_map.setdefault(e.target_id, []).append(e.source_id)

    # Direct callers
    direct_callers = callers_map.get(node_id, [])
    callers_data = []
    for cid in direct_callers[:30]:
        n = func_by_id.get(cid)
        if n:
            callers_data.append({
                "id": n.id, "name": n.name, "file_path": n.file_path,
                "semantic_kind": n.adapter_metadata.get("semantic_kind", "CALLS"),
            })

    # Direct callees
    direct_callees = callees_map.get(node_id, [])
    callees_data = []
    db_interactions = []
    for cid in direct_callees[:30]:
        n = func_by_id.get(cid)
        if n:
            sk = n.adapter_metadata.get("semantic_kind", "CALLS")
            callees_data.append({
                "id": n.id, "name": n.name, "file_path": n.file_path,
                "semantic_kind": sk,
            })
            if sk == "USES_DB":
                db_interactions.append(n.name)
        # Also check via semantic metadata side_effects
        if n and n.semantics and "database" in (n.semantics.side_effects or []):
            if n.name not in db_interactions:
                db_interactions.append(n.name)

    # BFS downstream to count all affected nodes
    visited: set[str] = {node_id}
    frontier = list(direct_callees)
    while frontier:
        nid = frontier.pop()
        if nid in visited:
            continue
        visited.add(nid)
        frontier.extend(callees_map.get(nid, []))
    downstream_count = len(visited) - 1  # exclude self

    # Affected modules
    affected_module_names = []
    for m in payload.module_nodes:
        affected_fids = set(m.linked_function_ids) & visited
        if affected_fids and node_id not in m.linked_function_ids:
            affected_module_names.append(m.name)

    # Risk level
    target_semantic_kind = target.adapter_metadata.get("semantic_kind", "CALLS")
    criticality = target.semantics.criticality if target.semantics else "medium"
    if (downstream_count > 30 or criticality == "critical" or
            target_semantic_kind in ("EXPOSES_API", "USES_DB")):
        risk = "critical"
    elif downstream_count > 10 or criticality == "high":
        risk = "high"
    elif downstream_count > 3 or criticality == "medium":
        risk = "medium"
    else:
        risk = "low"

    return ImpactAnalysis(
        node_id=node_id,
        node_name=target.name,
        file_path=target.file_path,
        callers=callers_data,
        callees=callees_data,
        db_interactions=db_interactions,
        affected_modules=list(dict.fromkeys(affected_module_names))[:10],  # dedup, preserve order
        caller_count=len(direct_callers),
        risk_level=risk,
        semantic_kind=target_semantic_kind,
    )


@app.post("/update")
def update_graph(req: UpdateRequest):
    payload = pipeline.update(req.graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    return {
        "graph_id": payload.graph_id,
        "function_count": len(payload.function_nodes),
        "module_count": len(payload.module_nodes),
    }


@app.post("/generate_descriptions")
def generate_descriptions(graph_id: str):
    """Generate (or regenerate) LLM 1-line descriptions for every node in an
    existing graph that currently has no real summary (stub or empty).

    Uses the currently configured LLM provider; safe to call repeatedly
    (already-generated descriptions are not overwritten).
    Returns counts of how many were newly generated vs already present.
    """
    payload = storage.load(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")

    from . import llm_provider as lp

    provider = lp.get_provider()
    is_real_llm = not getattr(provider, "_is_heuristic", False)

    total = 0
    generated = 0
    skipped = 0

    for n in payload.function_nodes:
        if not pipeline._is_callable(n):
            continue
        total += 1
        if not pipeline._is_stub(n.summary, n):  # now also catches heuristic-tagged nodes
            skipped += 1
            continue
        if n.code_snippet:
            n.summary = provider.summarize_function(
                n.name,
                n.code_snippet,
                kind=n.kind or n.type or "function",
            )
        else:
            from .llm_provider import _heuristic_one_liner
            n.summary = _heuristic_one_liner(n.name, n.kind or "function")
        n.adapter_metadata["summary_source"] = "llm" if is_real_llm else "heuristic"
        generated += 1

    if generated:
        storage.save(payload)

    return {
        "graph_id": graph_id,
        "total_callable_nodes": total,
        "newly_generated": generated,
        "already_had_description": skipped,
    }


@app.get("/repo_context")
def get_repo_context(graph_id: str):
    """Get repository context analysis for a graph.
    
    Returns the Bob-analyzed (or heuristic) repository metadata including
    project type, domain, architecture, tech stack, and purpose.
    """
    # Check if graph exists
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")

    # Load repository context
    repo_context = storage.load_meta(graph_id, "repo_context")
    if repo_context is None:
        raise HTTPException(404, "repository context not found - graph may have been created before Phase 1 implementation")
    
    return {
        "graph_id": graph_id,
        "repo_path": payload.repo_path,
        "context": repo_context
    }


@app.get("/semantic_analysis")
def get_semantic_analysis(graph_id: str):
    """Get semantic analysis results for a graph.
    
    Returns semantic metadata for all functions and semantic edges.
    Phase 2 feature.
    """
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")

    # Collect semantic metadata
    functions_with_semantics = []
    for node in payload.function_nodes:
        if node.semantics:
            functions_with_semantics.append({
                "id": node.id,
                "name": node.name,
                "file_path": node.file_path,
                "intent": node.semantics.intent,
                "complexity": node.semantics.complexity,
                "criticality": node.semantics.criticality,
                "patterns": node.semantics.patterns,
                "side_effects": node.semantics.side_effects,
                "confidence": node.semantics.confidence,
            })
    
    # Collect semantic edges
    semantic_edges_data = [
        {
            "type": edge.type,
            "source_id": edge.source_id,
            "target_id": edge.target_id,
            "confidence": edge.confidence,
            "description": edge.description,
            "inferred_by": edge.inferred_by,
        }
        for edge in payload.semantic_edges
    ]
    
    return {
        "graph_id": graph_id,
        "functions_analyzed": len(functions_with_semantics),
        "semantic_edges_count": len(semantic_edges_data),
        "functions": functions_with_semantics,
        "semantic_edges": semantic_edges_data,
    }


@app.get("/llm_ingestion")
def get_llm_ingestion(graph_id: str):
    """Get the node-level JSON returned by the LLM ingestion stage."""
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    llm_ingestion = storage.load_meta(graph_id, "llm_ingestion")
    if llm_ingestion is None:
        raise HTTPException(404, "llm ingestion metadata not found")
    return llm_ingestion


@app.get("/llm_ingestion_prompt")
def get_llm_ingestion_prompt(graph_id: str):
    """Get the prompt used for the LLM ingestion stage."""
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    prompt_data = storage.load_meta(graph_id, "llm_ingestion_prompt")
    if prompt_data is None:
        raise HTTPException(404, "llm ingestion prompt not found")
    return {
        "graph_id": graph_id,
        "prompt_version": prompt_data.get("version"),
        "prompt": prompt_data.get("prompt", ""),
    }



# Phase 3: Learning and Feedback Endpoints

@app.post("/feedback")
def submit_feedback(req: FeedbackRequest):
    """Submit user feedback on query results.

    Helps the system learn which results are helpful and improve over time.
    """
    # Normalise numeric ratings (1-5 stars) to the string literals the
    # learning layer expects.  String ratings pass through unchanged.
    rating = req.rating
    if isinstance(rating, (int, float)):
        rating = "helpful" if rating >= 4 else "unhelpful" if rating <= 2 else "neutral"

    success = learning.record_feedback(
        req.graph_id,
        req.query_id,
        rating,
        req.comment,
        req.corrections
    )
    if not success:
        raise HTTPException(404, "query not found")
    return {"status": "feedback recorded", "query_id": req.query_id}


@app.get("/module_details")
def get_module_details(graph_id: str, module_id: str):
    """Get detailed information about a specific module including control flow groups.
    
    Returns module metadata, entry points, control flow patterns, and function listings.
    """
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    
    # Find the module
    module = None
    for m in payload.module_nodes:
        if m.id == module_id:
            module = m
            break
    
    if module is None:
        raise HTTPException(404, "module not found")
    
    # Get function details
    func_by_id = {n.id: n for n in payload.function_nodes}
    functions = []
    for fid in module.linked_function_ids:
        node = func_by_id.get(fid)
        if node:
            functions.append({
                "id": node.id,
                "name": node.name,
                "type": node.type,
                "file_path": node.file_path,
                "summary": node.summary or "",
                "is_entry_point": fid in module.entry_functions,
            })
    
    # Build control flow details
    control_flow_details = []
    if module.control_flow_groups:
        from . import control_flow_analyzer
        control_flow_details = control_flow_analyzer.create_control_flow_submodules(
            {fid: {} for fid in module.linked_function_ids},  # Simplified for display
            func_by_id
        )
        
        # Enhance with actual groups from module
        for pattern_type, func_ids in module.control_flow_groups.items():
            functions_in_pattern = []
            for fid in func_ids:
                node = func_by_id.get(fid)
                if node:
                    functions_in_pattern.append({
                        "id": node.id,
                        "name": node.name,
                        "type": node.type,
                        "file_path": node.file_path,
                        "summary": node.summary or "",
                    })
            
            control_flow_details.append({
                "pattern_type": pattern_type,
                "description": control_flow_analyzer._pattern_description(pattern_type),
                "function_count": len(func_ids),
                "functions": functions_in_pattern,
            })
    
    return {
        "graph_id": graph_id,
        "module": {
            "id": module.id,
            "name": module.name,
            "description": module.description,
            "is_entry_point": module.is_entry_point,
            "submodule_type": module.submodule_type,
            "function_count": len(module.linked_function_ids),
            "entry_function_count": len(module.entry_functions),
        },
        "functions": functions,
        "control_flow_groups": control_flow_details,
    }


@app.get("/analytics")
def get_analytics(graph_id: str):
    """Get learning analytics for a graph.
    
    Returns statistics about queries, helpful rate, learned terms, etc.
    """
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    analytics = learning.get_analytics(graph_id)
    return analytics


@app.get("/hot_nodes")
def get_hot_nodes(graph_id: str, limit: int = 10):
    """Get most frequently accessed nodes.

    Shows which functions are queried most often.
    """
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    
    hot_nodes = learning.get_hot_nodes(graph_id, limit)
    
    # Enrich with function details
    func_by_id = {n.id: n for n in payload.function_nodes}
    enriched = []
    for hn in hot_nodes:
        node = func_by_id.get(hn["node_id"])
        if node:
            enriched.append({
                **hn,
                "name": node.name,
                "file_path": node.file_path,
                "intent": node.semantics.intent if node.semantics else "unknown",
                "criticality": node.semantics.criticality if node.semantics else "medium",
            })
    
    return {"graph_id": graph_id, "hot_nodes": enriched}


@app.get("/common_paths")
def get_common_paths(graph_id: str, min_frequency: int = 3):
    """Get frequently traversed execution paths.
    
    Shows common patterns in how users explore the codebase.
    """
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")

    paths = learning.get_common_paths(graph_id, min_frequency)
    
    # Enrich with function names
    func_by_id = {n.id: n for n in payload.function_nodes}
    enriched_paths = []
    for path in paths:
        enriched_path = []
        for node_id in path:
            node = func_by_id.get(node_id)
            if node:
                enriched_path.append({
                    "id": node_id,
                    "name": node.name,
                    "file_path": node.file_path,
                })
        if enriched_path:
            enriched_paths.append(enriched_path)
    
    return {"graph_id": graph_id, "common_paths": enriched_paths}


@app.post("/update_importance")
def update_node_importance(graph_id: str):
    """Update node criticality based on usage patterns.
    
    Adjusts semantic metadata based on actual usage data.
    """
    payload = storage.load(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    
    learning.update_node_importance(graph_id)

    return {"status": "importance updated", "graph_id": graph_id}


@app.delete("/graphs/{graph_id}")
def delete_graph(graph_id: str):
    """Permanently delete a stored graph and all its associated data."""
    if not storage.delete(graph_id):
        raise HTTPException(404, "graph not found")
    return {"deleted": graph_id}


# ---------------------------------------------------------------------------
# MCP helper endpoints
# ---------------------------------------------------------------------------

@app.get("/mcp/graphs")
def mcp_list_graphs():
    """MCP: List every ingested repository with summary metadata.

    Returns graph_id, repo_path, function/module counts, and the
    LLM-inferred project_type + purpose so an agent can pick the right
    graph without loading the full payload.
    """
    result = []
    for graph_id in storage.list_graphs():
        payload = storage.load_light(graph_id)
        if not payload:
            continue
        ctx = storage.load_meta(graph_id, "repo_context") or {}
        result.append({
            "graph_id":       graph_id,
            "repo_path":      payload.repo_path,
            "function_count": len(payload.function_nodes),
            "module_count":   len(payload.module_nodes),
            "project_type":   ctx.get("project_type", "unknown"),
            "architecture":   ctx.get("architecture", "unknown"),
            "purpose":        ctx.get("purpose", ""),
        })
    return {"graphs": result, "count": len(result)}


@app.get("/mcp/impact")
def mcp_impact_by_name(graph_id: str, function_name: str):
    """MCP: Change-impact analysis addressed by *function name* rather than node_id.

    Resolves the human-readable name to a node_id internally (first
    exact match wins; falls back to case-insensitive prefix match) and
    then delegates to the standard impact analysis logic.
    """
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")

    # Exact match first, then case-insensitive
    node_id: str | None = None
    lower_name = function_name.lower()
    for n in payload.function_nodes:
        if n.name == function_name:
            node_id = n.id
            break
    if node_id is None:
        for n in payload.function_nodes:
            if n.name.lower() == lower_name:
                node_id = n.id
                break
    if node_id is None:
        # prefix fallback
        for n in payload.function_nodes:
            if n.name.lower().startswith(lower_name):
                node_id = n.id
                break
    if node_id is None:
        raise HTTPException(
            404,
            f"No function named '{function_name}' found in graph {graph_id}. "
            "Use query_repo to locate the correct name.",
        )

    # Delegate to existing impact endpoint (direct call — same process)
    return get_impact(graph_id=graph_id, node_id=node_id)
