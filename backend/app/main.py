"""FastAPI entrypoint — Phase 9 + MCP Integration.

Routes are declared on ``api`` (an APIRouter) and mounted twice: once at
``/api`` (the path the production frontend and reverse proxies use) and once
at root with no prefix (kept for backward compatibility — the MCP server,
``tests/test_integration.py`` and ``tests/test_mcp_endpoints.py`` all call
root paths like ``/mcp/ingest`` directly). If a built frontend is present at
``FLOWIFY_FRONTEND_DIST`` it is mounted at ``/`` last, so it does not shadow
either router.
"""
from __future__ import annotations
import hashlib
import logging
import os
import re as _re
import uuid
from pathlib import Path as _Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import (
    bob_export, pipeline, storage, retrieval, module_abstractor, learning,
    knowledge as knowledge_mod, graph_analytics, report as report_mod,
)
# Imported directly (not via the `pipeline` module reference above) so it
# stays a real exception class in tests that mock out app.main.pipeline
# wholesale — `pipeline.RepoTooLargeError` would otherwise resolve to a
# MagicMock attribute, which `except` can't catch.
from .pipeline import RepoTooLargeError
from .models import (
    BobGraphRequest, IngestRequest, UpdateRequest, QueryRequest, QueryResponse,
    FeedbackRequest, MCPIngestResponse, MCPQueryResponse,
    ImpactAnalysis,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Deployment mode ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# "local"  — single-user dev/desktop use. Local filesystem paths allowed,
#            /shutdown enabled, all graphs visible to everyone.
# "server" — public hosted instance. Git-URL ingest only, local paths
#            restricted to FLOWIFY_ALLOWED_ROOTS, /shutdown disabled, graphs
#            scoped to the caller's X-Flowify-Session header.
FLOWIFY_MODE = os.environ.get("FLOWIFY_MODE", "local").lower().strip()
IS_SERVER_MODE = FLOWIFY_MODE == "server"

_cors_env = os.environ.get("FLOWIFY_CORS_ORIGINS", "").strip()
CORS_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()] or ["*"]

app = FastAPI(title="Flowify AI", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)

api = APIRouter()

# ── Session scoping (server mode only) ───────────────────────────────────────
# This is isolation, not authentication: it stops one anonymous visitor from
# casually reading another visitor's graph (and its code snippets) via
# /export, /graph, /query, etc. Anyone who guesses a session id still sees
# that session's graphs — there is no login. Sessions come from a header set
# by a middleware (not a route dependency) so the ~40 existing handlers don't
# each need a `request: Request` parameter added just to read it.
import contextvars

_session_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("flowify_session", default="")


@app.middleware("http")
async def _session_middleware(request: Request, call_next):
    token = _session_ctx.set(request.headers.get("x-flowify-session", ""))
    try:
        return await call_next(request)
    finally:
        _session_ctx.reset(token)


@app.middleware("http")
async def _byo_provider_middleware(request: Request, call_next):
    """Bring-your-own-key: a request carrying X-Flowify-Provider (+
    X-Flowify-Api-Key / X-Flowify-Model / X-Flowify-Base-Url) gets that
    provider for every LLM call made while handling it — ingest, query,
    module summaries, everything — via llm_provider's ContextVar override.
    No key is ever logged or persisted; it lives only in this request's
    context and is discarded when the request finishes.

    This is what lets a hosted instance run with zero server-side LLM spend
    (LLM_PROVIDER=heuristic) while still giving visitors real LLM answers if
    they supply their own key — see README's Deployment section.
    """
    provider_name = request.headers.get("x-flowify-provider", "").strip()
    if not provider_name:
        return await call_next(request)

    from . import llm_provider

    try:
        override = llm_provider.provider_from_spec(
            provider_name,
            api_key=request.headers.get("x-flowify-api-key", ""),
            model=request.headers.get("x-flowify-model", ""),
            base_url=request.headers.get("x-flowify-base-url", ""),
        )
    except ValueError as e:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": str(e)})

    with llm_provider.provider_override(override):
        return await call_next(request)


def _check_owned(graph_id: str) -> None:
    """Raise 404 (never 403 — don't confirm the graph exists to a non-owner)
    if this graph belongs to a different session. No-op outside server mode."""
    if not IS_SERVER_MODE:
        return
    owner = storage.get_owner(graph_id)
    if owner and owner != _session_ctx.get():
        raise HTTPException(status_code=404, detail="Graph not found")


@api.get("/status")
def root():
    """Service status. Formerly mounted at '/' — moved so '/' is free for the
    built frontend (see the StaticFiles mount at the bottom of this file)."""
    scope_owner = _session_ctx.get() if IS_SERVER_MODE else None
    return {"service": "flowify", "mode": FLOWIFY_MODE, "graphs": storage.list_graphs(owner=scope_owner)}


@api.get("/config")
def get_config():
    """Deployment-mode flags the frontend needs before the user does anything:
    whether to show the shutdown button, whether local paths are accepted."""
    return {
        "mode": FLOWIFY_MODE,
        "server_mode": IS_SERVER_MODE,
        "accepts_local_paths": not IS_SERVER_MODE,
        "accepts_git_urls": True,
    }


@api.get("/provider_info")
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


@api.post("/shutdown")
def shutdown():
    """Dev convenience: stop the backend, and best-effort stop the frontend dev
    server (the process listening on the Vite port).

    Triggered by the UI's shutdown button.  Returns immediately, then a daemon
    thread tears down both processes so the HTTP response can flush first.

    Disabled entirely in server mode (FLOWIFY_MODE=server) — this kills the
    process with no auth check, which is fine for a single-user local
    instance and not fine for a shared public one.
    """
    if IS_SERVER_MODE:
        raise HTTPException(status_code=404, detail="Not found")

    import os, platform, subprocess, threading, time

    frontend_port = int(os.environ.get("FLOWIFY_FRONTEND_PORT", "5173"))

    def _kill_port(port: int) -> None:
        """Best-effort terminate whatever LISTENs on *port* (not ourselves)."""
        try:
            if platform.system() == "Windows":
                out = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True, timeout=5
                ).stdout
                pids = {
                    line.split()[-1]
                    for line in out.splitlines()
                    if f":{port} " in line and "LISTENING" in line
                }
                for pid in pids:
                    if pid and pid not in ("0", str(os.getpid())):
                        subprocess.run(["taskkill", "/PID", pid, "/F"],
                                       capture_output=True, timeout=5)
            else:
                out = subprocess.run(
                    ["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=5
                ).stdout
                for pid in out.split():
                    if pid and pid != str(os.getpid()):
                        os.kill(int(pid), 15)
        except Exception:
            pass

    def _teardown() -> None:
        time.sleep(0.4)            # let the response flush to the browser
        _kill_port(frontend_port)  # frontend (Vite dev server)
        os._exit(0)                # backend (this process)

    threading.Thread(target=_teardown, daemon=True).start()
    return {"status": "shutting_down", "frontend_port": frontend_port}


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


_ALLOWED_ROOTS = [
    _Path(p).resolve() for p in os.environ.get("FLOWIFY_ALLOWED_ROOTS", "").split(os.pathsep)
    if p.strip()
]

# Server-mode-only cap on parsed node count (see _do_ingest). 800 matches
# graph_analytics.py's own _BETWEENNESS_EXACT_LIMIT — already this
# codebase's established line for "big enough that exact graph algorithms
# get expensive" — and sits safely below the ~2275-node repo that OOM'd a
# 512 MB free-tier container.
_MAX_INGEST_NODES = int(os.environ.get("FLOWIFY_MAX_NODES", "800"))


def _validate_local_path(repo_path: str) -> str:
    """In server mode, resolve *repo_path* and reject anything that doesn't
    exist or falls outside FLOWIFY_ALLOWED_ROOTS. Without this, POST
    /ingest_repo {"repo_path": "/"} is an arbitrary host-filesystem reader —
    see backend/app/cloner.py's docstring for the matching threat model on
    the git-URL side.

    Local mode passes *repo_path* through unchanged (no resolution, no
    existence check) — that's a single-user desktop instance where the
    caller already controls the filesystem, and it preserves the exact
    ingest behavior every existing test and MCP client already relies on.
    """
    if not IS_SERVER_MODE:
        return repo_path

    resolved = _Path(repo_path).resolve()
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"repo_path is not a directory: {repo_path}")
    if not _ALLOWED_ROOTS:
        raise HTTPException(
            status_code=403,
            detail="local filesystem paths are not accepted on this server; pass repo_url instead",
        )
    if not any(resolved == root or root in resolved.parents for root in _ALLOWED_ROOTS):
        raise HTTPException(status_code=403, detail="repo_path is outside the allowed roots")
    return str(resolved)


def _do_ingest(req: IngestRequest):
    """Resolve an IngestRequest (repo_path OR repo_url) to a local directory,
    run the existing synchronous pipeline.ingest() unchanged, and tag the
    result with the caller's session (server mode only).

    For a git URL: clone to a throwaway temp dir, ingest it, delete the
    clone, then rewrite the stored repo_path to the origin URL — so exports
    and the UI show "github.com/..." rather than a temp path that no longer
    exists.
    """
    from . import cloner

    owner = (_session_ctx.get() or None) if IS_SERVER_MODE else None
    # File count is a poor proxy for the memory a large repo actually costs
    # (click's 79 files parse into 2275 nodes / 18721 edges, which OOM'd this
    # app's 512 MB free-tier container during module clustering) — so this
    # caps the real cost driver, checked right after parsing, before any of
    # the memory-heavy downstream stages run. Unset in local mode: that's
    # your own machine's resources to spend.
    max_nodes = _MAX_INGEST_NODES if IS_SERVER_MODE else None

    try:
        if req.repo_url:
            try:
                display_path = cloner.validate_repo_url(req.repo_url)
                with cloner.clone_to_temp(req.repo_url) as local_path:
                    payload = pipeline.ingest(str(local_path), max_nodes=max_nodes)
            except cloner.InvalidRepoUrl as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            storage.set_repo_path(payload.graph_id, display_path)
            payload.repo_path = display_path
        elif req.repo_path:
            resolved = _validate_local_path(req.repo_path)
            payload = pipeline.ingest(resolved, max_nodes=max_nodes)
        else:
            raise HTTPException(status_code=400, detail="repo_path or repo_url is required")
    except RepoTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e)) from e

    if owner:
        storage.set_owner(payload.graph_id, owner)
    return payload


@api.post("/ingest_repo")
def ingest_repo(req: IngestRequest):
    """Legacy ingest endpoint - maintained for backward compatibility."""
    payload = _do_ingest(req)

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


@api.post("/mcp/ingest", response_model=MCPIngestResponse)
def mcp_ingest_repo(req: IngestRequest):
    """MCP-optimized ingest endpoint with stable repo_id and normalized response.
    
    This endpoint provides:
    - Stable repo_id for idempotent operations
    - Consistent error format
    - Structured response for MCP tools
    """
    source = req.repo_path or req.repo_url or ""
    try:
        logger.info(f"MCP ingest request for repo: {source}")

        # Generate stable repo_id
        repo_id = _generate_repo_id(source, req.repo_id)

        # Check if already ingested (idempotent) — an indexed lookup, not a
        # scan of every stored graph.
        existing_id = storage.find_graph_by_repo_path(source)
        if existing_id:
            payload = storage.load_light(existing_id)
            if payload:
                logger.info(f"Repository already ingested with graph_id: {existing_id}")
                repo_context = storage.load_meta(existing_id, "repo_context")
                return MCPIngestResponse(
                    success=True,
                    graph_id=existing_id,
                    repo_id=repo_id,
                    repo_path=payload.repo_path,
                    function_count=len(payload.function_nodes),
                    module_count=len(payload.module_nodes),
                    repo_context=repo_context
                )

        # Perform ingestion
        payload = _do_ingest(req)
        repo_context = storage.load_meta(payload.graph_id, "repo_context")

        logger.info(f"Ingestion complete: graph_id={payload.graph_id}, functions={len(payload.function_nodes)}")

        return MCPIngestResponse(
            success=True,
            graph_id=payload.graph_id,
            repo_id=repo_id,
            repo_path=payload.repo_path,
            function_count=len(payload.function_nodes),
            module_count=len(payload.module_nodes),
            repo_context=repo_context
        )

    except HTTPException as e:
        logger.error(f"Ingestion rejected for {source}: {e.detail}")
        return MCPIngestResponse(
            success=False,
            graph_id="",
            repo_id=_generate_repo_id(source, req.repo_id),
            repo_path=source,
            function_count=0,
            module_count=0,
            error=str(e.detail)
        )
    except Exception as e:
        logger.error(f"Ingestion failed for {source}: {str(e)}", exc_info=True)
        return MCPIngestResponse(
            success=False,
            graph_id="",
            repo_id=_generate_repo_id(source, req.repo_id),
            repo_path=source,
            function_count=0,
            module_count=0,
            error=str(e)
        )


@api.post("/bob/graph")
def build_graph_for_bob(req: BobGraphRequest):
    """Ingest a repo root and return a Bob-ready generated graph.

    This endpoint is intended for Bob agents, MCP servers, and external repo
    tooling that need the graph payload immediately instead of a graph id to
    query through the Flowify UI endpoints.
    """
    resolved = _validate_local_path(req.repo_path)
    # In server mode, full-graph responses (every code_snippet, inline) are
    # multi-MB and unauthenticated — require it to be asked for explicitly
    # rather than defaulting to True as the model does for local/MCP use.
    include_full = req.include_full_graph and not IS_SERVER_MODE
    try:
        return bob_export.build_bob_graph_response(
            resolved,
            depth=req.depth,
            include_full_graph=include_full,
            include_view=req.include_view,
            include_llm_ingestion=req.include_llm_ingestion,
            max_nodes=_MAX_INGEST_NODES if IS_SERVER_MODE else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@api.get("/entry_points")
def get_entry_points(graph_id: str, max_count: int = 4):
    """Return up to `max_count` inferred entry-point files for the initial view."""
    _check_owned(graph_id)
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    func_by_id, function_edges = _serialize_payload(payload)
    declared = (storage.load_meta(graph_id, "repo_context") or {}).get("key_entry_points", [])
    nodes = module_abstractor.find_entry_files(
        func_by_id, function_edges, declared_entry_points=declared, max_count=max_count,
    )
    return {"graph_id": graph_id, "nodes": nodes, "edges": []}


@api.get("/expand")
def expand_graph_node(graph_id: str, node_id: str, action: str = "callees"):
    """Lazy-expand a node — returns its direct children and relevant edges.

    For file-level nodes (id starts with `file::`):
      action=callees   → show files this file calls
      action=functions → drill into the file's symbols
    """
    _check_owned(graph_id)
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


@api.get("/graph")
def get_graph(graph_id: str, depth: int = 1):
    """Get graph at specified depth with enhanced module metadata.

    Depth 1: Shows modules with entry point tagging and control flow summaries
    Depth 2: Shows file-level submodules
    Depth 3: Shows individual functions
    """
    _check_owned(graph_id)
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


@api.post("/query", response_model=QueryResponse)
def query_graph(req: QueryRequest):
    """Query endpoint — returns graph-grounded explanation + structured execution path."""
    _check_owned(req.graph_id)
    payload = storage.load_light(req.graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")

    # Conversation context: continue existing thread or start a new one
    conv_id = req.conversation_id or uuid.uuid4().hex[:12]
    prior_turns: list = learning.load_conversation(req.graph_id, conv_id) if req.conversation_id else []

    # Retrieve with learning tracking; graph returned for step building
    ordered, sub, query_id, g = retrieval.retrieve_subgraph(payload, req.query, max_hops=req.depth)
    explanation = retrieval.explain(payload, req.query, ordered, prior_turns=prior_turns)
    execution_steps = retrieval.build_execution_steps(payload, ordered, g)

    # Persist turn so follow-up queries see it
    prior_turns.append({"query": req.query, "summary": explanation[:300]})
    learning.save_conversation(req.graph_id, conv_id, prior_turns)

    return QueryResponse(
        explanation=explanation,
        subgraph=sub,
        path=ordered,
        query_id=query_id,
        conversation_id=conv_id,
        execution_steps=execution_steps,
        graph_nodes_consulted=len(ordered),
    )


@api.post("/mcp/query", response_model=MCPQueryResponse)
def mcp_query_repo(req: QueryRequest):
    """MCP-optimized query endpoint with normalized response.
    
    This endpoint provides:
    - Consistent error format
    - Structured function metadata
    - Clear execution path
    """
    _check_owned(req.graph_id)
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
        
        # Conversation context: continue thread or start new
        conv_id = req.conversation_id or uuid.uuid4().hex[:12]
        prior_turns: list = learning.load_conversation(req.graph_id, conv_id) if req.conversation_id else []

        # Retrieve subgraph with learning tracking
        ordered, sub, query_id, _g = retrieval.retrieve_subgraph(payload, req.query, max_hops=req.depth)
        explanation = retrieval.explain(payload, req.query, ordered, prior_turns=prior_turns)

        # Persist turn
        prior_turns.append({"query": req.query, "summary": explanation[:300]})
        learning.save_conversation(req.graph_id, conv_id, prior_turns)

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
            query_id=query_id,
            conversation_id=conv_id,
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


@api.get("/impact", response_model=ImpactAnalysis)
def get_impact(graph_id: str, node_id: str):
    """Change-impact analysis for a given node.

    Returns:
      - callers: functions that directly call this node
      - callees: functions this node calls
      - db_interactions: DB-touching callees
      - affected_modules: module names that would be impacted
      - risk_level: low / medium / high / critical
    """
    _check_owned(graph_id)
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


@api.post("/update")
def update_graph(req: UpdateRequest):
    _check_owned(req.graph_id)
    existing = storage.load_light(req.graph_id)
    if existing and existing.repo_path.startswith(("https://", "http://")):
        # pipeline.update() diffs an on-disk git checkout via GitPython; a
        # URL-sourced graph has no such checkout (it was cloned to a temp
        # dir that's already been deleted). Re-ingesting from the URL is a
        # full re-clone, not an incremental diff, so ask the caller to use
        # /ingest_repo (or /ingest) again rather than silently 500ing here.
        raise HTTPException(
            status_code=400,
            detail="This graph was ingested from a git URL; re-run ingest with the "
                   "same repo_url to refresh it instead of /update.",
        )
    payload = pipeline.update(req.graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    return {
        "graph_id": payload.graph_id,
        "function_count": len(payload.function_nodes),
        "module_count": len(payload.module_nodes),
    }


@api.post("/generate_descriptions")
def generate_descriptions(graph_id: str):
    """Generate (or regenerate) LLM 1-line descriptions for every node in an
    existing graph that currently has no real summary (stub or empty).

    Uses the currently configured LLM provider; safe to call repeatedly
    (already-generated descriptions are not overwritten).
    Returns counts of how many were newly generated vs already present.
    """
    _check_owned(graph_id)
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


@api.get("/repo_context")
def get_repo_context(graph_id: str):
    """Get repository context analysis for a graph.
    
    Returns the Bob-analyzed (or heuristic) repository metadata including
    project type, domain, architecture, tech stack, and purpose.
    """
    _check_owned(graph_id)
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


@api.get("/semantic_analysis")
def get_semantic_analysis(graph_id: str):
    """Get semantic analysis results for a graph.
    
    Returns semantic metadata for all functions and semantic edges.
    Phase 2 feature.
    """
    _check_owned(graph_id)
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


@api.get("/llm_ingestion")
def get_llm_ingestion(graph_id: str):
    """Get the node-level JSON returned by the LLM ingestion stage."""
    _check_owned(graph_id)
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    llm_ingestion = storage.load_meta(graph_id, "llm_ingestion")
    if llm_ingestion is None:
        raise HTTPException(404, "llm ingestion metadata not found")
    return llm_ingestion


@api.get("/llm_ingestion_prompt")
def get_llm_ingestion_prompt(graph_id: str):
    """Get the prompt used for the LLM ingestion stage."""
    _check_owned(graph_id)
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

@api.post("/feedback")
def submit_feedback(req: FeedbackRequest):
    """Submit user feedback on query results.

    Helps the system learn which results are helpful and improve over time.
    """
    _check_owned(req.graph_id)
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


@api.get("/module_details")
def get_module_details(graph_id: str, module_id: str):
    """Get detailed information about a specific module including control flow groups.
    
    Returns module metadata, entry points, control flow patterns, and function listings.
    """
    _check_owned(graph_id)
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


@api.get("/analytics")
def get_analytics(graph_id: str):
    """Get learning analytics for a graph.
    
    Returns statistics about queries, helpful rate, learned terms, etc.
    """
    _check_owned(graph_id)
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    analytics = learning.get_analytics(graph_id)
    return analytics


@api.get("/hot_nodes")
def get_hot_nodes(graph_id: str, limit: int = 10):
    """Get most frequently accessed nodes.

    Shows which functions are queried most often.
    """
    _check_owned(graph_id)
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


@api.get("/common_paths")
def get_common_paths(graph_id: str, min_frequency: int = 3):
    """Get frequently traversed execution paths.
    
    Shows common patterns in how users explore the codebase.
    """
    _check_owned(graph_id)
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


@api.post("/update_importance")
def update_node_importance(graph_id: str):
    """Update node criticality based on usage patterns.
    
    Adjusts semantic metadata based on actual usage data.
    """
    _check_owned(graph_id)
    payload = storage.load(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    
    learning.update_node_importance(graph_id)

    return {"status": "importance updated", "graph_id": graph_id}


@api.delete("/graphs/{graph_id}")
def delete_graph(graph_id: str):
    """Permanently delete a stored graph and all its associated data."""
    _check_owned(graph_id)
    if not storage.delete(graph_id):
        raise HTTPException(404, "graph not found")
    return {"deleted": graph_id}


# ---------------------------------------------------------------------------
# Graph export endpoint
# ---------------------------------------------------------------------------

@api.get("/export/{graph_id}")
def export_graph(
    graph_id: str,
    format: str = "json",
    max_nodes: int = 80,
):
    """Export the call graph as a downloadable Mermaid diagram or simplified JSON.

    format:    "json"    — JSON download with nodes/edges and semantic metadata
               "mermaid" — Mermaid flowchart TD text, suitable for pasting into
                           mermaid.live or any Markdown renderer
    max_nodes: cap how many nodes to include (default 80)
    """
    _check_owned(graph_id)
    from datetime import datetime as _dt
    from fastapi.responses import JSONResponse, PlainTextResponse

    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")

    # Rank nodes by out-degree so the most-connected (important) ones come first
    out_degree: dict[str, int] = {}
    for e in payload.function_edges:
        if e.relationship == "INVOKES" or e.type == "CALLS":
            out_degree[e.source_id] = out_degree.get(e.source_id, 0) + 1

    top_nodes = sorted(
        payload.function_nodes,
        key=lambda n: out_degree.get(n.id, 0),
        reverse=True,
    )[:max_nodes]
    node_id_set = {n.id for n in top_nodes}

    relevant_edges = [
        e for e in payload.function_edges
        if e.source_id in node_id_set
        and e.target_id in node_id_set
        and (e.relationship == "INVOKES" or e.type == "CALLS")
    ]

    if format == "json":
        data = {
            "graph_id": graph_id,
            "repo_path": payload.repo_path,
            "exported_at": _dt.utcnow().isoformat(),
            "nodes": [
                {
                    "id": n.id,
                    "name": n.name,
                    "file_path": n.file_path,
                    "type": n.type,
                    "semantic_kind": n.adapter_metadata.get("semantic_kind", "CALLS"),
                    "summary": n.summary or "",
                    "intent": n.semantics.intent if n.semantics else None,
                    "criticality": n.semantics.criticality if n.semantics else None,
                }
                for n in top_nodes
            ],
            "edges": [
                {"source": e.source_id, "target": e.target_id, "type": e.type}
                for e in relevant_edges[:500]
            ],
        }
        return JSONResponse(
            content=data,
            headers={
                "Content-Disposition": f'attachment; filename="graph-{graph_id}.json"',
            },
        )

    elif format == "mermaid":
        # Counter-based id map, not a truncated regex-sanitized node id: two
        # distinct node ids that only differ after character 40 used to
        # collapse into the same Mermaid node id via `[:40]` truncation,
        # silently merging unrelated functions in the diagram.
        _id_map: dict[str, str] = {}

        def _safe(nid: str) -> str:
            if nid not in _id_map:
                _id_map[nid] = f"n{len(_id_map)}"
            return _id_map[nid]

        lines = ["flowchart TD"]
        for n in top_nodes:
            nid = _safe(n.id)
            label = n.name.replace('"', "'")
            kind = n.adapter_metadata.get("semantic_kind", "CALLS")
            if kind == "EXPOSES_API":
                lines.append(f'    {nid}["{label}"]:::api')
            elif kind == "USES_DB":
                lines.append(f'    {nid}["{label}"]:::db')
            elif kind in ("EMITS_EVENT", "CONSUMES_EVENT"):
                lines.append(f'    {nid}["{label}"]:::event')
            else:
                lines.append(f'    {nid}["{label}"]')

        for e in relevant_edges[:200]:
            lines.append(f"    {_safe(e.source_id)} --> {_safe(e.target_id)}")

        lines += [
            "    classDef api fill:#10b981,color:#fff,stroke:#059669",
            "    classDef db fill:#8b5cf6,color:#fff,stroke:#7c3aed",
            "    classDef event fill:#f59e0b,color:#fff,stroke:#d97706",
        ]

        return PlainTextResponse(
            content="\n".join(lines),
            headers={
                "Content-Disposition": f'attachment; filename="graph-{graph_id}.mmd"',
            },
        )

    elif format == "llm":
        # The zero-LLM-cost path: a single self-contained Markdown document
        # (repo context, architecture, god nodes, cycles, rationale notes)
        # meant to be pasted directly into any chatbot — this is the actual
        # "export the graph to use in another LLM" product, not a stub.
        markdown = report_mod.build_report(graph_id, payload)
        return PlainTextResponse(
            content=markdown,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="GRAPH_REPORT-{graph_id}.md"',
            },
        )

    raise HTTPException(400, f"Unknown format '{format}'. Use 'json', 'mermaid', or 'llm'.")


# ---------------------------------------------------------------------------
# Flow summary endpoints
# ---------------------------------------------------------------------------

@api.get("/flow_summary/{graph_id}")
def get_flow_summary(graph_id: str):
    """Return the repository flow summary generated at ingest time.

    404 if the graph doesn't exist, or if it was ingested before the flow
    summary feature existed (use POST to generate one on demand).
    """
    _check_owned(graph_id)
    data = storage.load_meta(graph_id, "flow_summary")
    if data is None:
        if storage.load_light(graph_id) is None:
            raise HTTPException(404, "graph not found")
        raise HTTPException(404, "flow summary not available — POST to generate one")
    return data


@api.post("/flow_summary/{graph_id}")
def generate_flow_summary(graph_id: str):
    """(Re)generate the flow summary for an existing graph.

    Useful for graphs ingested before the feature shipped, or to refresh after
    switching to a better LLM provider.
    """
    _check_owned(graph_id)
    from .models import RepositoryContext

    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")

    ctx_dict = storage.load_meta(graph_id, "repo_context") or {}
    try:
        repo_context = RepositoryContext(**ctx_dict)
    except Exception:
        repo_context = RepositoryContext(fallback_used=True)

    summary = pipeline._build_flow_summary(graph_id, payload.repo_path, payload, repo_context)
    storage.store_meta(graph_id, "flow_summary", summary.model_dump())
    return summary.model_dump()


# ---------------------------------------------------------------------------
# Knowledge layer: documents, rationale, provenance, analytics, report
# ---------------------------------------------------------------------------

def _require_payload(graph_id: str):
    """Load a graph payload (light) or raise 404. Shared by every endpoint
    below that needs "the graph, or a 404" — keeps that one-liner from being
    re-typed at each call site."""
    payload = storage.load_light(graph_id)
    if payload is None:
        raise HTTPException(404, "graph not found")
    _check_owned(graph_id)
    return payload


def _get_or_build_knowledge(graph_id: str, rebuild: bool = False, payload=None) -> dict:
    """Load the cached knowledge index, building it on demand for old graphs.

    Pass `payload` when the caller already has it loaded, to avoid loading
    the same graph twice within one request.
    """
    if not rebuild:
        data = storage.load_meta(graph_id, "knowledge")
        if data is not None:
            return data
    if payload is None:
        payload = _require_payload(graph_id)
    kidx = knowledge_mod.build_knowledge(payload.repo_path, payload.function_nodes)
    kidx.graph_id = graph_id
    data = kidx.model_dump()
    storage.store_meta(graph_id, "knowledge", data)
    return data


@api.get("/knowledge/{graph_id}")
def get_knowledge(graph_id: str):
    """Return the documentation + rationale knowledge layer for a graph.

    Documents (README/ADR/RFC/guides/wikis) are first-class nodes; doc_edges
    connect them to the code nodes they reference, each with provenance
    (source, confidence, evidence). Built at ingest; auto-built here for
    graphs that predate the feature.
    """
    # No separate existence check needed: cached metadata only exists for
    # graphs that were ingested, and a cache miss falls through to
    # _require_payload inside _get_or_build_knowledge.
    return _get_or_build_knowledge(graph_id)


@api.post("/knowledge/{graph_id}")
def rebuild_knowledge(graph_id: str):
    """Force-rebuild the knowledge layer (after docs change on disk)."""
    return _get_or_build_knowledge(graph_id, rebuild=True)


@api.get("/node_references")
def get_node_references(graph_id: str, node_id: str):
    """Everything the knowledge layer knows about one code node:

    - documents that reference it (with section, line, confidence, evidence)
    - rationale notes (TODO/FIXME/HACK/WHY/...) inside its source span
    - provenance for its incoming/outgoing call edges
    """
    payload = _require_payload(graph_id)

    func_by_id = {n.id: n for n in payload.function_nodes}
    target = func_by_id.get(node_id)
    if target is None:
        raise HTTPException(404, "node not found")

    data = _get_or_build_knowledge(graph_id, payload=payload)
    docs_by_id = {d["id"]: d for d in data.get("documents", [])}

    # Documents referencing this node (directly, or its file via MENTIONS_FILE)
    file_target = f"file::{target.file_path}"
    referenced_in = []
    for e in data.get("doc_edges", []):
        if e["target_id"] == node_id or e["target_id"] == file_target:
            doc = docs_by_id.get(e["source_id"])
            if not doc:
                continue
            referenced_in.append({
                "document": doc["title"],
                "doc_path": doc["file_path"],
                "doc_type": doc["doc_type"],
                "section": e.get("section"),
                "line": e.get("line"),
                "direct": e["target_id"] == node_id,
                "provenance": e.get("provenance", {}),
            })
    # Direct references first, then by confidence
    referenced_in.sort(
        key=lambda r: (r["direct"], r["provenance"].get("confidence", 0)), reverse=True
    )

    rationale = [
        r for r in data.get("rationale_notes", [])
        if r.get("attached_node_id") == node_id
        or (r["file_path"] == target.file_path and not r.get("attached_node_id")
            and target.kind == "file")
    ]

    # Provenance-tagged call edges touching this node
    edges = []
    for e in payload.function_edges:
        if node_id in (e.source_id, e.target_id) and (e.relationship == "INVOKES" or e.type == "CALLS"):
            other_id = e.target_id if e.source_id == node_id else e.source_id
            other = func_by_id.get(other_id)
            edges.append({
                "direction": "out" if e.source_id == node_id else "in",
                "other": other.name if other else other_id,
                "type": e.type,
                "provenance": knowledge_mod.edge_provenance(e.adapter_metadata).model_dump(),
            })
    for e in payload.semantic_edges:
        if node_id in (e.source_id, e.target_id):
            other_id = e.target_id if e.source_id == node_id else e.source_id
            other = func_by_id.get(other_id)
            edges.append({
                "direction": "out" if e.source_id == node_id else "in",
                "other": other.name if other else other_id,
                "type": e.type,
                "description": e.description,
                "provenance": knowledge_mod.semantic_edge_provenance(
                    e.inferred_by, e.confidence
                ).model_dump(),
            })

    return {
        "graph_id": graph_id,
        "node_id": node_id,
        "node_name": target.name,
        "referenced_in": referenced_in[:20],
        "rationale": rationale[:20],
        "edges": edges[:40],
    }


@api.get("/graph_analytics/{graph_id}")
def get_graph_analytics(graph_id: str, refresh: bool = False):
    """Centrality metrics, god nodes, cycles, bridges, dead code, and
    surprising cross-module couplings. Cached at ingest; refresh=true recomputes.
    """
    if not refresh:
        data = storage.load_meta(graph_id, "analytics")
        if data is not None:
            return data
    payload = _require_payload(graph_id)
    data = graph_analytics.compute_analytics(payload)
    storage.store_meta(graph_id, "analytics", data)
    return data


@api.get("/architecture_report/{graph_id}")
def get_architecture_report(graph_id: str, format: str = "markdown"):
    """Generate the repository architecture report (GRAPH_REPORT.md).

    format=markdown → downloadable Markdown; format=json → {"markdown": ...}.
    """
    from fastapi.responses import PlainTextResponse

    payload = _require_payload(graph_id)
    md = report_mod.build_report(graph_id, payload)
    if format == "json":
        return {"graph_id": graph_id, "markdown": md}
    return PlainTextResponse(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="GRAPH_REPORT-{graph_id}.md"'},
    )


# ---------------------------------------------------------------------------
# MCP helper endpoints
# ---------------------------------------------------------------------------

@api.get("/mcp/graphs")
def mcp_list_graphs():
    """MCP: List every ingested repository with summary metadata.

    Returns graph_id, repo_path, function/module counts, and the
    LLM-inferred project_type + purpose so an agent can pick the right
    graph without loading the full payload.
    """
    result = []
    # Always filter in server mode, even to an empty-string session (no
    # X-Flowify-Session header) — that matches nothing rather than falling
    # through to "no filter, show every visitor's graphs".
    scope_owner = _session_ctx.get() if IS_SERVER_MODE else None
    for graph_id in storage.list_graphs(owner=scope_owner):
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


@api.get("/mcp/impact")
def mcp_impact_by_name(graph_id: str, function_name: str):
    """MCP: Change-impact analysis addressed by *function name* rather than node_id.

    Resolves the human-readable name to a node_id internally (first
    exact match wins; falls back to case-insensitive prefix match) and
    then delegates to the standard impact analysis logic.
    """
    payload = _require_payload(graph_id)

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


@api.get("/mcp/find_node")
def mcp_find_node(graph_id: str, name: str, limit: int = 10):
    """MCP: Find code nodes by (partial) name. Returns matches with summaries."""
    payload = _require_payload(graph_id)
    lname = name.lower()
    exact, prefix, sub = [], [], []
    for n in payload.function_nodes:
        if n.kind in ("external",) or n.kind == "file":
            continue
        nl = n.name.lower()
        entry = {
            "id": n.id, "name": n.name, "file_path": n.file_path,
            "type": n.type, "summary": (n.summary or "")[:160],
            "lineno": n.lineno,
        }
        if nl == lname:
            exact.append(entry)
        elif nl.startswith(lname):
            prefix.append(entry)
        elif lname in nl:
            sub.append(entry)
    matches = (exact + prefix + sub)[:limit]
    return {"graph_id": graph_id, "query": name, "matches": matches, "count": len(matches)}


@api.get("/mcp/neighbors")
def mcp_neighbors(graph_id: str, node_id: str):
    """MCP: Direct callers and callees of a node (1-hop neighborhood)."""
    impact = get_impact(graph_id=graph_id, node_id=node_id)
    return {
        "graph_id": graph_id,
        "node_id": node_id,
        "node_name": impact.node_name,
        "callers": impact.callers,
        "callees": impact.callees,
    }


@api.get("/mcp/shortest_path")
def mcp_shortest_path(graph_id: str, source: str, target: str):
    """MCP: Shortest call path between two functions, addressed by name."""
    payload = _require_payload(graph_id)
    path = graph_analytics.shortest_path(payload, source, target)
    if path is None:
        return {"graph_id": graph_id, "source": source, "target": target,
                "found": False, "path": []}
    return {"graph_id": graph_id, "source": source, "target": target,
            "found": True, "path": path, "hops": len(path) - 1}


@api.get("/mcp/search_rationale")
def mcp_search_rationale(graph_id: str, query: str = "", marker: str = "", limit: int = 20):
    """MCP: Search extracted developer rationale (TODO/FIXME/HACK/WHY/NOTE).

    Filter by free-text `query` and/or `marker` (e.g. HACK). Empty filters
    return the most recent notes.
    """
    payload = _require_payload(graph_id)
    data = _get_or_build_knowledge(graph_id, payload=payload)
    notes = data.get("rationale_notes", [])
    if marker:
        notes = [r for r in notes if r["marker"] == marker.upper()]
    if query:
        q = query.lower()
        notes = [r for r in notes if q in r["text"].lower() or q in r["file_path"].lower()]
    return {"graph_id": graph_id, "count": len(notes), "notes": notes[:limit]}


@api.get("/mcp/dead_code")
def mcp_dead_code(graph_id: str):
    """MCP: Functions never invoked anywhere in the graph (candidates only —
    dynamic dispatch and framework hooks can produce false positives)."""
    analytics = get_graph_analytics(graph_id)
    return {"graph_id": graph_id, "dead_code": analytics.get("dead_code", [])}


@api.get("/mcp/hotspots")
def mcp_hotspots(graph_id: str):
    """MCP: God nodes, critical bridges, and high-risk components."""
    analytics = get_graph_analytics(graph_id)
    return {
        "graph_id": graph_id,
        "god_nodes": analytics.get("god_nodes", []),
        "bridges": analytics.get("bridges", []),
        "high_risk": analytics.get("high_risk", []),
    }


@api.get("/mcp/cycles")
def mcp_cycles(graph_id: str):
    """MCP: Circular dependencies in the call graph."""
    analytics = get_graph_analytics(graph_id)
    return {"graph_id": graph_id, "cycles": analytics.get("cycles", []),
            "surprising_couplings": analytics.get("surprising_couplings", [])}


@api.get("/mcp/architectural_summary")
def mcp_architectural_summary(graph_id: str):
    """MCP: The full architecture report as Markdown, for agent consumption."""
    payload = _require_payload(graph_id)
    return {"graph_id": graph_id, "markdown": report_mod.build_report(graph_id, payload)}


# ---------------------------------------------------------------------------
# Server-mode hardening: rate limiting + TTL janitor
# ---------------------------------------------------------------------------

_RATE_LIMITED_PREFIXES = ("/ingest_repo", "/mcp/ingest", "/bob/graph", "/query", "/mcp/query")
_RATE_LIMIT_WINDOW_S = 60.0
_RATE_LIMIT_MAX = int(os.environ.get("FLOWIFY_RATE_LIMIT_PER_MIN", "6"))
_rate_buckets: dict[str, list[float]] = {}
_rate_lock = None  # set lazily; threading is stdlib but avoid the import cost when unused

# Endpoints that spend real LLM budget: ingest is forced onto the heuristic
# provider (see pipeline.ingest()'s provider_override), so its only LLM cost
# is one flow-summary call baked into ingestion itself — no separate guard
# needed there. Query (2 calls/question) and an explicit flow-summary
# regenerate are the surfaces a visitor can trigger repeatedly on demand.
_LLM_ENDPOINT_SUFFIXES = ("/query", "/mcp/query")
_LLM_ENDPOINT_INFIXES = ("/flow_summary/",)  # POST only; path has a {graph_id} segment

# A per-IP-per-minute cap (above) stops one abusive visitor; it does nothing
# against many different visitors collectively draining a *shared, global*
# budget — which is exactly what Ollama Cloud's free tier is (a 5-hour
# session / 7-day rolling allowance on the account, not per-caller). This
# is a coarse, conservative backstop on top of the per-IP limit: once the
# whole server has made too many LLM-backed requests in a day, every visitor
# gets a clear 429 instead of the shared key silently running out mid-answer.
_LLM_BUDGET_WINDOW_S = 86400.0
_LLM_BUDGET_MAX = int(os.environ.get("FLOWIFY_LLM_CALLS_PER_DAY", "300"))
_llm_budget_calls: list[float] = []


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_llm_endpoint(request: Request) -> bool:
    if request.method != "POST":
        return False
    path = request.url.path.rstrip("/")
    return path.endswith(_LLM_ENDPOINT_SUFFIXES) or any(inf in path for inf in _LLM_ENDPOINT_INFIXES)


@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next):
    """A per-IP, per-minute cap on the endpoints that actually cost money or
    CPU (ingest, query), plus a global per-day cap on the subset that spend
    real LLM budget. Deliberately not applied to read-only GETs. Only active
    in server mode — local/desktop use has one user and no shared budget to
    protect."""
    if not IS_SERVER_MODE or request.method != "POST" or not any(
        request.url.path.rstrip("/").endswith(p) for p in _RATE_LIMITED_PREFIXES
    ):
        return await call_next(request)

    import threading
    import time as _time

    global _rate_lock
    if _rate_lock is None:
        _rate_lock = threading.Lock()

    ip = _client_ip(request)
    now = _time.time()
    with _rate_lock:
        bucket = [t for t in _rate_buckets.get(ip, []) if now - t < _RATE_LIMIT_WINDOW_S]
        if len(bucket) >= _RATE_LIMIT_MAX:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit: max {_RATE_LIMIT_MAX} requests/min to this endpoint"},
            )
        bucket.append(now)
        _rate_buckets[ip] = bucket

        if _is_llm_endpoint(request):
            global _llm_budget_calls
            _llm_budget_calls = [t for t in _llm_budget_calls if now - t < _LLM_BUDGET_WINDOW_S]
            if len(_llm_budget_calls) >= _LLM_BUDGET_MAX:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=429,
                    content={"detail": "AI mode has hit its shared daily budget — try again later. "
                                        "Ingestion and graph browsing are unaffected."},
                )
            _llm_budget_calls.append(now)

    return await call_next(request)


def _start_graph_janitor() -> None:
    """Server-mode only: delete graphs older than FLOWIFY_GRAPH_TTL_H so an
    ephemeral hosted instance (no persistent storage) doesn't accumulate
    other people's ingested code indefinitely between restarts."""
    ttl_h = float(os.environ.get("FLOWIFY_GRAPH_TTL_H", "24"))
    if not IS_SERVER_MODE or ttl_h <= 0:
        return

    import threading
    import time as _time

    def _loop() -> None:
        while True:
            _time.sleep(min(3600.0, ttl_h * 3600 / 4))
            try:
                cutoff = _time.time() - ttl_h * 3600
                deleted = storage.delete_older_than(cutoff)
                if deleted:
                    logger.info(f"[janitor] deleted {len(deleted)} graph(s) older than {ttl_h}h")
            except Exception as e:
                logger.error(f"[janitor] failed: {e}")

    threading.Thread(target=_loop, daemon=True).start()


_start_graph_janitor()


# ---------------------------------------------------------------------------
# Router mounting + static frontend
# ---------------------------------------------------------------------------
# /api/*  — the path the production frontend and any reverse proxy use.
# /*      — the same routes with no prefix, for backward compatibility with
#           the MCP server and tests that call e.g. /mcp/ingest directly.
# Both must be registered before the StaticFiles mount below, or the '/'
# catch-all would shadow them.
app.include_router(api, prefix="/api")
app.include_router(api, include_in_schema=False)

_frontend_dist = os.environ.get("FLOWIFY_FRONTEND_DIST", "").strip()
if _frontend_dist and _Path(_frontend_dist).is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="ui")
else:
    # Dev mode (`npm run dev` serves the frontend separately) or no build
    # was baked into the image — keep '/' answering with a small JSON status
    # blob rather than a bare 404, same shape /api/status returns.
    @app.get("/", include_in_schema=False)
    def _root_fallback():
        scope_owner = _session_ctx.get() if IS_SERVER_MODE else None
        return {"service": "flowify", "mode": FLOWIFY_MODE, "graphs": storage.list_graphs(owner=scope_owner)}
