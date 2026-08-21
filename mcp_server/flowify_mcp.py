"""
Flowify MCP Server — exposes the full Flowify toolkit to AI coding assistants.

Tools
-----
ingest_repo           Ingest a repository and build its call graph.
query_repo            Natural-language query against an ingested graph.
list_graphs           List all ingested repositories with metadata.
get_repo_overview     Full repo context: purpose, architecture, entry points.
impact_analysis       Change-impact analysis by function name.
get_flow_summary      Narrative flow summary: what/usecase/flows/architecture/techniques.
delete_graph          Delete a stored graph and free its storage.
find_node             Locate code nodes by (partial) name.
shortest_path         Shortest call path between two functions.
graph_hotspots        God nodes, critical bridges, high-risk components.
find_cycles           Circular dependencies + surprising cross-module couplings.
find_dead_code        Functions never invoked anywhere.
search_rationale      Search TODO/FIXME/HACK/WHY developer intent notes.
architecture_report   Full Markdown architecture report for a repository.

Resilience features
-------------------
- Circuit breaker (fail-fast when backend is down)
- Retry with exponential back-off (transient errors)
- Request deduplication (parallel ingest calls de-duped)
- Per-tool timeout tuning (long timeout for ingest/query, short for reads)
"""
import asyncio
import json
import logging
import os
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import Tool, TextContent

from .resilience import (
    circuit_breaker,
    request_deduplicator,
    retry_with_backoff,
    HealthChecker,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("flowify-mcp")

# ── Config ──────────────────────────────────────────────────────────────────
# Both mcp_server/config.json and mcp_server/README.md have long documented
# FASTAPI_BASE_URL as an environment variable — this used to ignore it
# entirely and hardcode localhost, so pointing the MCP server at a deployed
# backend required editing this file. Root paths (e.g. "/mcp/ingest") work
# unprefixed against any Flowify backend — see the router-mounting comment
# at the top of backend/app/main.py.
FASTAPI_BASE_URL = os.environ.get("FASTAPI_BASE_URL", "http://localhost:8000").rstrip("/")
# LLM-backed operations can take several minutes (Ollama cold start)
LONG_TIMEOUT  = 420.0   # ingest / query
SHORT_TIMEOUT =  15.0   # list / overview / delete

app = Server("flowify-mcp")
health_checker = HealthChecker(FASTAPI_BASE_URL)


def _http(timeout: float = LONG_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=FASTAPI_BASE_URL,
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
    )


async def _request(
    method: str,
    url: str,
    timeout: float = LONG_TIMEOUT,
    **kwargs,
) -> dict:
    """Circuit-broken, retried HTTP call.  Returns parsed JSON."""
    if not circuit_breaker.can_attempt():
        raise RuntimeError(
            f"Circuit breaker OPEN — backend unavailable. "
            f"State: {circuit_breaker.get_state()}"
        )

    async def _do():
        async with _http(timeout) as client:
            if not await health_checker.ensure_healthy(client):
                logger.warning("Health check failed; attempting anyway")
            m = method.lower()
            if m == "get":
                r = await client.get(url, **kwargs)
            elif m == "post":
                r = await client.post(url, **kwargs)
            elif m == "delete":
                r = await client.delete(url, **kwargs)
            else:
                raise ValueError(f"Unsupported method: {method}")
            r.raise_for_status()
            return r.json()

    try:
        data = await retry_with_backoff(
            _do,
            max_retries=2,
            initial_delay=1.0,
            exceptions=(httpx.TimeoutException, httpx.ConnectError),
        )
        circuit_breaker.record_success()
        return data
    except Exception as exc:
        circuit_breaker.record_failure()
        raise exc


def _text(content: str) -> list[TextContent]:
    return [TextContent(type="text", text=content)]


def _error(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"✗ {msg}")]


# ── Tool registry ────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="ingest_repo",
            description=(
                "Analyse a code repository and build a function-level call graph with "
                "LLM-generated summaries and semantic metadata. Returns a graph_id used "
                "by all other tools. Idempotent — returns the existing graph if the "
                "repository has already been ingested."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the repository root directory.",
                    },
                    "repo_id": {
                        "type": "string",
                        "description": "Optional stable identifier. Auto-generated from the path if omitted.",
                    },
                },
                "required": ["repo_path"],
            },
        ),
        Tool(
            name="query_repo",
            description=(
                "Answer a natural-language question about an ingested codebase using the "
                "call graph and semantic metadata. Returns an explanation grounded in the "
                "actual graph, plus a ranked list of relevant functions and their execution "
                "order. Use this to understand how a feature is implemented, trace data "
                "flow, or find the functions responsible for a behaviour."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {
                        "type": "string",
                        "description": "Graph ID from ingest_repo.",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural-language question, e.g. "
                            "'how does authentication work?' or "
                            "'where is user data persisted?'"
                        ),
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Call-graph traversal depth (1–5, default 2).",
                        "minimum": 1,
                        "maximum": 5,
                        "default": 2,
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": (
                            "Conversation thread ID for multi-turn context. "
                            "Omit on the first question; on follow-up questions pass the "
                            "conversation_id returned by the previous query_repo call."
                        ),
                    },
                },
                "required": ["graph_id", "query"],
            },
        ),
        Tool(
            name="list_graphs",
            description=(
                "List every repository that has been ingested into Flowify. "
                "Returns graph_id, repo_path, function/module counts, project type, "
                "and a one-sentence purpose description. "
                "Call this first to check whether a repository is already available "
                "before calling ingest_repo."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_repo_overview",
            description=(
                "Get a structural overview of an ingested repository: project type, "
                "tech stack, architecture pattern, high-level purpose, inferred entry "
                "points, and top-level module breakdown. "
                "Use this at the start of a session to orient yourself before querying."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {
                        "type": "string",
                        "description": "Graph ID from ingest_repo or list_graphs.",
                    },
                },
                "required": ["graph_id"],
            },
        ),
        Tool(
            name="impact_analysis",
            description=(
                "Analyse the change-impact of modifying a specific function. "
                "Returns its direct callers (who calls it), direct callees (what it calls), "
                "any database operations it touches, affected modules, downstream node count, "
                "and a risk level (low / medium / high / critical). "
                "Use this before refactoring or deleting a function to understand blast radius."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {
                        "type": "string",
                        "description": "Graph ID from ingest_repo or list_graphs.",
                    },
                    "function_name": {
                        "type": "string",
                        "description": (
                            "Exact function name to analyse. "
                            "If unsure of the exact name, use query_repo first."
                        ),
                    },
                },
                "required": ["graph_id", "function_name"],
            },
        ),
        Tool(
            name="get_flow_summary",
            description=(
                "Get a narrative flow summary of an ingested repository: what the code "
                "does, the problem it solves, an ordered walkthrough of the modules data "
                "flows through, a high-level Mermaid architecture diagram, the tech stack, "
                "and techniques used (model types, API styles, algorithms). "
                "This is the best single call to understand an unfamiliar codebase end-to-end. "
                "Generated at ingest time; pass regenerate=true to rebuild it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {
                        "type": "string",
                        "description": "Graph ID from ingest_repo or list_graphs.",
                    },
                    "regenerate": {
                        "type": "boolean",
                        "description": (
                            "Rebuild the summary instead of returning the cached one. "
                            "Use for graphs ingested before the feature existed, or to "
                            "refresh after switching to a better LLM. Default false."
                        ),
                        "default": False,
                    },
                },
                "required": ["graph_id"],
            },
        ),
        Tool(
            name="find_node",
            description=(
                "Locate functions, classes, or methods by exact or partial name. "
                "Returns matching nodes with file paths, summaries, and line numbers. "
                "Use this to resolve a name before impact_analysis or shortest_path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {"type": "string", "description": "Graph ID from ingest_repo or list_graphs."},
                    "name": {"type": "string", "description": "Full or partial symbol name to find."},
                    "limit": {"type": "integer", "description": "Max matches (default 10).", "default": 10},
                },
                "required": ["graph_id", "name"],
            },
        ),
        Tool(
            name="shortest_path",
            description=(
                "Find the shortest call path between two functions, addressed by name. "
                "Answers questions like 'how does the API layer reach the database?' — "
                "returns the ordered chain of functions connecting source to target."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {"type": "string", "description": "Graph ID from ingest_repo or list_graphs."},
                    "source": {"type": "string", "description": "Name of the starting function."},
                    "target": {"type": "string", "description": "Name of the target function."},
                },
                "required": ["graph_id", "source", "target"],
            },
        ),
        Tool(
            name="graph_hotspots",
            description=(
                "Structural hotspot analysis: the most influential 'god' functions "
                "(PageRank + degree + betweenness centrality), critical bridges whose "
                "removal disconnects the graph, and high-risk components (large fan-out, "
                "many callers, high complexity). Ideal for onboarding and refactor planning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {"type": "string", "description": "Graph ID from ingest_repo or list_graphs."},
                },
                "required": ["graph_id"],
            },
        ),
        Tool(
            name="find_cycles",
            description=(
                "Detect circular dependencies in the call graph, plus surprising "
                "cross-module couplings (thin threads between otherwise-unrelated "
                "subsystems). Both often reveal architectural problems."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {"type": "string", "description": "Graph ID from ingest_repo or list_graphs."},
                },
                "required": ["graph_id"],
            },
        ),
        Tool(
            name="find_dead_code",
            description=(
                "List functions that are never invoked anywhere in the graph. "
                "These are dead-code CANDIDATES — dynamic dispatch, reflection, and "
                "framework hooks can produce false positives, so verify before deleting."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {"type": "string", "description": "Graph ID from ingest_repo or list_graphs."},
                },
                "required": ["graph_id"],
            },
        ),
        Tool(
            name="search_rationale",
            description=(
                "Search developer intent extracted from source comments — TODO, FIXME, "
                "HACK, WHY, NOTE, BUG, WARNING, OPTIMIZE markers — each attached to the "
                "function containing it. Use this to surface known workarounds and "
                "unfinished work before modifying code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {"type": "string", "description": "Graph ID from ingest_repo or list_graphs."},
                    "query": {"type": "string", "description": "Free-text filter over note text/paths (optional)."},
                    "marker": {"type": "string", "description": "Filter to one marker, e.g. HACK or FIXME (optional)."},
                },
                "required": ["graph_id"],
            },
        ),
        Tool(
            name="architecture_report",
            description=(
                "Generate the full repository architecture report (GRAPH_REPORT.md): "
                "summary, language distribution, Mermaid diagram, major components, "
                "most important functions by centrality, high-risk components, cycles, "
                "dead code, documentation map, rationale highlights, and suggested "
                "questions. The best single artifact for onboarding to a codebase."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {"type": "string", "description": "Graph ID from ingest_repo or list_graphs."},
                },
                "required": ["graph_id"],
            },
        ),
        Tool(
            name="delete_graph",
            description=(
                "Permanently delete a stored call graph and all its associated data "
                "(nodes, edges, metadata, learning history). "
                "Use this to remove stale graphs or free storage after a major refactor. "
                "The repository can be re-ingested afterwards."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {
                        "type": "string",
                        "description": "Graph ID to delete (from list_graphs).",
                    },
                },
                "required": ["graph_id"],
            },
        ),
    ]


# ── Tool dispatcher ──────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    dispatch = {
        "ingest_repo":      _handle_ingest_repo,
        "query_repo":       _handle_query_repo,
        "list_graphs":      _handle_list_graphs,
        "get_repo_overview": _handle_get_repo_overview,
        "impact_analysis":  _handle_impact_analysis,
        "get_flow_summary": _handle_get_flow_summary,
        "delete_graph":     _handle_delete_graph,
        "find_node":           _handle_find_node,
        "shortest_path":       _handle_shortest_path,
        "graph_hotspots":      _handle_graph_hotspots,
        "find_cycles":         _handle_find_cycles,
        "find_dead_code":      _handle_find_dead_code,
        "search_rationale":    _handle_search_rationale,
        "architecture_report": _handle_architecture_report,
    }
    handler = dispatch.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    return await handler(arguments or {})


# ── ingest_repo ──────────────────────────────────────────────────────────────

async def _handle_ingest_repo(args: dict) -> list[TextContent]:
    repo_path = args.get("repo_path", "").strip()
    repo_id   = args.get("repo_id")

    if not repo_path:
        return _error("repo_path is required")

    try:
        async def _do():
            return await _request(
                "post", "/mcp/ingest",
                timeout=LONG_TIMEOUT,
                json={"repo_path": repo_path, "repo_id": repo_id},
            )

        result = await request_deduplicator.execute(
            "ingest", _do,
            repo_path=repo_path,
            repo_id=repo_id or "",
        )

        if not result.get("success"):
            return _error(result.get("error", "Ingestion failed"))

        lines = [
            "✓ Repository ingested",
            f"  Graph ID : {result['graph_id']}",
            f"  Path     : {result['repo_path']}",
            f"  Functions: {result['function_count']}",
            f"  Modules  : {result['module_count']}",
        ]
        ctx = result.get("repo_context") or {}
        if ctx.get("project_type"):
            lines.append(f"  Type     : {ctx['project_type']}")
        if ctx.get("architecture"):
            lines.append(f"  Arch     : {ctx['architecture']}")
        if ctx.get("purpose"):
            lines.append(f"  Purpose  : {ctx['purpose']}")
        lines.append(
            f"\nUse graph_id '{result['graph_id']}' with query_repo, "
            "get_repo_overview, and impact_analysis."
        )
        return _text("\n".join(lines))

    except httpx.HTTPStatusError as exc:
        return _error(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except httpx.TimeoutException:
        return _error(
            "Ingestion timed out. The repository may be large; "
            "try again — the graph may have been partially built."
        )
    except Exception as exc:
        logger.exception("ingest_repo failed")
        return _error(str(exc))


# ── query_repo ───────────────────────────────────────────────────────────────

async def _handle_query_repo(args: dict) -> list[TextContent]:
    graph_id        = args.get("graph_id", "").strip()
    query           = args.get("query", "").strip()
    depth           = args.get("depth", 2)
    conversation_id = args.get("conversation_id", "").strip() or None

    if not graph_id or not query:
        return _error("graph_id and query are required")

    try:
        body: dict = {"graph_id": graph_id, "query": query, "depth": depth}
        if conversation_id:
            body["conversation_id"] = conversation_id

        result = await _request(
            "post", "/mcp/query",
            timeout=LONG_TIMEOUT,
            json=body,
        )

        if not result.get("success"):
            return _error(result.get("error", "Query failed"))

        lines = [f"Query: {query}", "", result["explanation"], ""]

        funcs = result.get("relevant_functions", [])
        if funcs:
            lines.append(f"Relevant functions ({len(funcs)}):")
            for i, f in enumerate(funcs[:15], 1):
                line = f"  {i}. {f['name']}  ({f['file_path']})"
                if f.get("summary"):
                    line += f"\n     {f['summary']}"
                meta = []
                if f.get("intent"):
                    meta.append(f"intent={f['intent']}")
                if f.get("criticality"):
                    meta.append(f"criticality={f['criticality']}")
                if meta:
                    line += f"\n     [{', '.join(meta)}]"
                lines.append(line)
            if len(funcs) > 15:
                lines.append(f"  … and {len(funcs) - 15} more")
        else:
            lines.append("No relevant functions found.")

        qid = result.get("query_id")
        cid = result.get("conversation_id")
        if qid:
            lines.append(f"\nQuery ID: {qid}  (use submit_feedback to rate this result)")
        if cid:
            lines.append(
                f"Conversation ID: {cid}  "
                "(pass as conversation_id in your next query_repo call for follow-up context)"
            )

        return _text("\n".join(lines))

    except httpx.HTTPStatusError as exc:
        return _error(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except httpx.TimeoutException:
        return _error("Query timed out. Try depth=1 or a shorter query.")
    except Exception as exc:
        logger.exception("query_repo failed")
        return _error(str(exc))


# ── list_graphs ───────────────────────────────────────────────────────────────

async def _handle_list_graphs(_args: dict) -> list[TextContent]:
    try:
        result = await _request("get", "/mcp/graphs", timeout=SHORT_TIMEOUT)
        graphs = result.get("graphs", [])

        if not graphs:
            return _text(
                "No repositories ingested yet.\n"
                "Use ingest_repo to analyse a repository first."
            )

        lines = [f"Ingested repositories ({len(graphs)}):"]
        for g in graphs:
            lines.append(f"\n  graph_id : {g['graph_id']}")
            lines.append(f"  path     : {g['repo_path']}")
            lines.append(f"  functions: {g['function_count']}  modules: {g['module_count']}")
            if g.get("project_type") and g["project_type"] != "unknown":
                lines.append(f"  type     : {g['project_type']} / {g.get('architecture','?')}")
            if g.get("purpose"):
                lines.append(f"  purpose  : {g['purpose']}")
        lines.append(
            "\nPass a graph_id to query_repo, get_repo_overview, "
            "impact_analysis, or delete_graph."
        )
        return _text("\n".join(lines))

    except Exception as exc:
        logger.exception("list_graphs failed")
        return _error(str(exc))


# ── get_repo_overview ─────────────────────────────────────────────────────────

async def _handle_get_repo_overview(args: dict) -> list[TextContent]:
    graph_id = args.get("graph_id", "").strip()
    if not graph_id:
        return _error("graph_id is required")

    try:
        ctx_resp, ep_resp = await asyncio.gather(
            _request("get", "/repo_context", timeout=SHORT_TIMEOUT, params={"graph_id": graph_id}),
            _request("get", "/entry_points",  timeout=SHORT_TIMEOUT, params={"graph_id": graph_id, "max_count": 6}),
            return_exceptions=True,
        )

        lines = [f"Repository overview — {graph_id}"]

        # Repo context
        if isinstance(ctx_resp, Exception):
            lines.append(f"\n[repo context unavailable: {ctx_resp}]")
        else:
            ctx = ctx_resp.get("context", {})
            lines += [
                f"\nPath        : {ctx_resp.get('repo_path', '')}",
                f"Project type: {ctx.get('project_type', 'unknown')}",
                f"Architecture: {ctx.get('architecture', 'unknown')}",
                f"Domain      : {ctx.get('domain', 'general')}",
            ]
            if ctx.get("tech_stack"):
                lines.append(f"Tech stack  : {', '.join(ctx['tech_stack'])}")
            if ctx.get("purpose"):
                lines.append(f"Purpose     : {ctx['purpose']}")

        # Entry points
        if isinstance(ep_resp, Exception):
            lines.append(f"\n[entry points unavailable: {ep_resp}]")
        else:
            ep_nodes = ep_resp.get("nodes", [])
            if ep_nodes:
                lines.append("\nEntry points:")
                for ep in ep_nodes[:6]:
                    name = ep.get("name") or ep.get("id", "?")
                    path = ep.get("file_path") or ep.get("module_path") or ""
                    summary = ep.get("summary") or ep.get("description") or ""
                    line = f"  • {name}"
                    if path:
                        line += f"  ({path})"
                    if summary:
                        line += f"\n    {summary}"
                    lines.append(line)

        lines.append(
            "\nUse query_repo to explore behaviour, "
            "or impact_analysis before modifying a function."
        )
        return _text("\n".join(lines))

    except httpx.HTTPStatusError as exc:
        return _error(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:
        logger.exception("get_repo_overview failed")
        return _error(str(exc))


# ── impact_analysis ───────────────────────────────────────────────────────────

async def _handle_impact_analysis(args: dict) -> list[TextContent]:
    graph_id      = args.get("graph_id", "").strip()
    function_name = args.get("function_name", "").strip()

    if not graph_id or not function_name:
        return _error("graph_id and function_name are required")

    try:
        result = await _request(
            "get", "/mcp/impact",
            timeout=SHORT_TIMEOUT,
            params={"graph_id": graph_id, "function_name": function_name},
        )

        risk = result.get("risk_level", "unknown")
        risk_icon = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(risk, "⚪")

        lines = [
            f"Impact analysis: {result.get('node_name', function_name)}",
            f"File   : {result.get('file_path', '')}",
            f"Kind   : {result.get('semantic_kind', 'CALLS')}",
            f"Risk   : {risk_icon} {risk.upper()}",
        ]

        callers = result.get("callers", [])
        if callers:
            lines.append(f"\nDirect callers ({result.get('caller_count', len(callers))}):")
            for c in callers[:10]:
                lines.append(f"  ← {c['name']}  ({c['file_path']})")
            if result.get("caller_count", 0) > 10:
                lines.append(f"  … and {result['caller_count'] - 10} more")
        else:
            lines.append("\nNo callers found (this may be an entry point).")

        callees = result.get("callees", [])
        if callees:
            lines.append(f"\nDirect callees ({len(callees)}):")
            for c in callees[:10]:
                sk = c.get("semantic_kind", "CALLS")
                badge = " [DB]" if sk == "USES_DB" else " [API]" if sk == "EXPOSES_API" else ""
                lines.append(f"  → {c['name']}{badge}  ({c['file_path']})")

        db = result.get("db_interactions", [])
        if db:
            lines.append(f"\nDatabase operations touched: {', '.join(db)}")

        mods = result.get("affected_modules", [])
        if mods:
            lines.append(f"\nAffected modules: {', '.join(mods)}")

        lines.append(
            "\nTip: if risk is HIGH or CRITICAL, use query_repo to trace all "
            "call paths before making changes."
        )
        return _text("\n".join(lines))

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            body = exc.response.json() if exc.response.headers.get("content-type", "").startswith("application/json") else {}
            detail = body.get("detail", f"Function '{function_name}' not found in graph {graph_id}")
            return _error(f"{detail}. Use query_repo to find the correct function name.")
        return _error(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:
        logger.exception("impact_analysis failed")
        return _error(str(exc))


# ── get_flow_summary ──────────────────────────────────────────────────────────

async def _handle_get_flow_summary(args: dict) -> list[TextContent]:
    graph_id   = args.get("graph_id", "").strip()
    regenerate = bool(args.get("regenerate", False))
    if not graph_id:
        return _error("graph_id is required")

    try:
        if regenerate:
            # POST regenerates (LLM-backed → may be slow), GET reads the cached one
            data = await _request("post", f"/flow_summary/{graph_id}", timeout=LONG_TIMEOUT)
        else:
            data = await _request("get", f"/flow_summary/{graph_id}", timeout=SHORT_TIMEOUT)

        lines = [f"Flow summary — {graph_id}"]
        if data.get("what"):
            lines.append(f"\nWHAT\n{data['what']}")
        if data.get("usecase"):
            lines.append(f"\nUSE CASE\n{data['usecase']}")

        flows = data.get("flows", [])
        if flows:
            lines.append("\nFLOWS")
            for f in flows:
                lines.append(f"  {f['step']}. {f['module']}")
                if f.get("description"):
                    lines.append(f"     {f['description']}")

        if data.get("tech_stack"):
            lines.append(f"\nTECH STACK\n  {', '.join(data['tech_stack'])}")
        if data.get("techniques"):
            lines.append(f"\nTECHNIQUES\n  {', '.join(data['techniques'])}")

        if data.get("architecture_mermaid"):
            lines.append("\nHIGH-LEVEL ARCHITECTURE (Mermaid)\n```mermaid")
            lines.append(data["architecture_mermaid"])
            lines.append("```")

        if data.get("fallback_used"):
            lines.append("\n[note: generated without a real LLM — prose is degraded]")

        return _text("\n".join(lines))

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _error(
                f"No flow summary for graph '{graph_id}'. "
                "It may predate the feature — call again with regenerate=true."
            )
        return _error(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:
        logger.exception("get_flow_summary failed")
        return _error(str(exc))


# ── delete_graph ──────────────────────────────────────────────────────────────

async def _handle_delete_graph(args: dict) -> list[TextContent]:
    graph_id = args.get("graph_id", "").strip()
    if not graph_id:
        return _error("graph_id is required")

    try:
        result = await _request(
            "delete", f"/graphs/{graph_id}",
            timeout=SHORT_TIMEOUT,
        )
        return _text(
            f"✓ Graph {result.get('deleted', graph_id)} deleted.\n"
            "Run ingest_repo on the same path to re-analyse."
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _error(f"Graph '{graph_id}' not found. Use list_graphs to see available graphs.")
        return _error(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:
        logger.exception("delete_graph failed")
        return _error(str(exc))


# ── Graph-intelligence tools ──────────────────────────────────────────────────

async def _handle_find_node(args: dict) -> list[TextContent]:
    graph_id = args.get("graph_id", "").strip()
    name = args.get("name", "").strip()
    if not graph_id or not name:
        return _error("graph_id and name are required")
    try:
        result = await _request(
            "get", "/mcp/find_node", timeout=SHORT_TIMEOUT,
            params={"graph_id": graph_id, "name": name, "limit": args.get("limit", 10)},
        )
        matches = result.get("matches", [])
        if not matches:
            return _text(f"No symbols matching '{name}'. Try a shorter fragment or query_repo.")
        lines = [f"Matches for '{name}' ({len(matches)}):"]
        for m in matches:
            loc = f"{m['file_path']}:{m['lineno']}" if m.get("lineno") else m["file_path"]
            lines.append(f"  • {m['name']} [{m['type']}]  ({loc})")
            if m.get("summary"):
                lines.append(f"    {m['summary']}")
        return _text("\n".join(lines))
    except Exception as exc:
        logger.exception("find_node failed")
        return _error(str(exc))


async def _handle_shortest_path(args: dict) -> list[TextContent]:
    graph_id = args.get("graph_id", "").strip()
    source = args.get("source", "").strip()
    target = args.get("target", "").strip()
    if not graph_id or not source or not target:
        return _error("graph_id, source, and target are required")
    try:
        result = await _request(
            "get", "/mcp/shortest_path", timeout=SHORT_TIMEOUT,
            params={"graph_id": graph_id, "source": source, "target": target},
        )
        if not result.get("found"):
            return _text(
                f"No call path found from '{source}' to '{target}'.\n"
                "Check the names with find_node — or the dependency may be "
                "indirect (events, DB) rather than a call chain."
            )
        lines = [f"Call path {source} → {target} ({result['hops']} hops):"]
        for i, step in enumerate(result["path"]):
            prefix = "  " + ("└→ " if i else "   ")
            lines.append(f"{prefix}{step['name']}  ({step['file_path']})")
            if step.get("summary"):
                lines.append(f"       {step['summary']}")
        return _text("\n".join(lines))
    except Exception as exc:
        logger.exception("shortest_path failed")
        return _error(str(exc))


async def _handle_graph_hotspots(args: dict) -> list[TextContent]:
    graph_id = args.get("graph_id", "").strip()
    if not graph_id:
        return _error("graph_id is required")
    try:
        result = await _request(
            "get", "/mcp/hotspots", timeout=LONG_TIMEOUT,
            params={"graph_id": graph_id},
        )
        lines = [f"Graph hotspots — {graph_id}"]
        god = result.get("god_nodes", [])
        if god:
            lines.append("\nMost important functions (centrality):")
            for i, g in enumerate(god[:10], 1):
                lines.append(
                    f"  {i}. {g['name']}  ({g['file_path']})  "
                    f"[in={g['in_degree']} out={g['out_degree']} pr={g['pagerank']}]"
                )
        bridges = result.get("bridges", [])
        if bridges:
            lines.append("\nCritical bridges (removal disconnects the graph):")
            for b in bridges[:8]:
                lines.append(f"  • {b['name']}  ({b['file_path']})  degree={b['degree']}")
        risk = result.get("high_risk", [])
        if risk:
            lines.append("\nHigh-risk components:")
            for r in risk[:8]:
                lines.append(f"  ⚠ {r['name']}  ({r['file_path']}) — {'; '.join(r['reasons'])}")
        if len(lines) == 1:
            lines.append("\nNo hotspots detected (graph may be very small).")
        return _text("\n".join(lines))
    except Exception as exc:
        logger.exception("graph_hotspots failed")
        return _error(str(exc))


async def _handle_find_cycles(args: dict) -> list[TextContent]:
    graph_id = args.get("graph_id", "").strip()
    if not graph_id:
        return _error("graph_id is required")
    try:
        result = await _request(
            "get", "/mcp/cycles", timeout=LONG_TIMEOUT,
            params={"graph_id": graph_id},
        )
        cycles = result.get("cycles", [])
        couplings = result.get("surprising_couplings", [])
        lines = [f"Dependency analysis — {graph_id}"]
        if cycles:
            lines.append(f"\nCircular dependencies ({len(cycles)}):")
            for cyc in cycles[:10]:
                chain = " → ".join(n["name"] for n in cyc)
                lines.append(f"  ↻ {chain} → {cyc[0]['name']}")
        else:
            lines.append("\nNo circular dependencies found. 🎉")
        if couplings:
            lines.append("\nSurprising cross-module couplings (thin threads):")
            for c in couplings[:10]:
                lines.append(
                    f"  • {c['from_module']} → {c['to_module']} "
                    f"({c['edge_count']} edge(s), e.g. "
                    f"{c['example']['source']} → {c['example']['target']})"
                )
        return _text("\n".join(lines))
    except Exception as exc:
        logger.exception("find_cycles failed")
        return _error(str(exc))


async def _handle_find_dead_code(args: dict) -> list[TextContent]:
    graph_id = args.get("graph_id", "").strip()
    if not graph_id:
        return _error("graph_id is required")
    try:
        result = await _request(
            "get", "/mcp/dead_code", timeout=LONG_TIMEOUT,
            params={"graph_id": graph_id},
        )
        dead = result.get("dead_code", [])
        if not dead:
            return _text("No dead-code candidates found — every function has at least one caller.")
        lines = [f"Dead-code candidates ({len(dead)}) — verify before deleting "
                 "(dynamic dispatch / framework hooks cause false positives):"]
        for d in dead[:30]:
            lines.append(f"  • {d['name']}  ({d['file_path']})")
        if len(dead) > 30:
            lines.append(f"  … and {len(dead) - 30} more")
        return _text("\n".join(lines))
    except Exception as exc:
        logger.exception("find_dead_code failed")
        return _error(str(exc))


async def _handle_search_rationale(args: dict) -> list[TextContent]:
    graph_id = args.get("graph_id", "").strip()
    if not graph_id:
        return _error("graph_id is required")
    try:
        params = {"graph_id": graph_id}
        if args.get("query"):
            params["query"] = args["query"]
        if args.get("marker"):
            params["marker"] = args["marker"]
        result = await _request(
            "get", "/mcp/search_rationale", timeout=LONG_TIMEOUT, params=params,
        )
        notes = result.get("notes", [])
        if not notes:
            return _text("No rationale notes matched. Try without filters, or the repo has no marker comments.")
        lines = [f"Developer rationale ({result.get('count', len(notes))} total, showing {len(notes)}):"]
        for r in notes:
            owner = f" in {r['attached_node_name']}" if r.get("attached_node_name") else ""
            lines.append(f"\n  [{r['marker']}]{owner}  {r['file_path']}:{r['line']}")
            lines.append(f"    {r['text']}")
        return _text("\n".join(lines))
    except Exception as exc:
        logger.exception("search_rationale failed")
        return _error(str(exc))


async def _handle_architecture_report(args: dict) -> list[TextContent]:
    graph_id = args.get("graph_id", "").strip()
    if not graph_id:
        return _error("graph_id is required")
    try:
        result = await _request(
            "get", f"/architecture_report/{graph_id}", timeout=LONG_TIMEOUT,
            params={"format": "json"},
        )
        md = result.get("markdown", "")
        if not md:
            return _error("Report generation returned no content")
        return _text(md)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _error(f"Graph '{graph_id}' not found. Use list_graphs.")
        return _error(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:
        logger.exception("architecture_report failed")
        return _error(str(exc))


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        logger.info("Flowify MCP server starting (14 tools)")
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
