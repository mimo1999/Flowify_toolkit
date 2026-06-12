"""
Flowify MCP Server — exposes the full Flowify toolkit to AI coding assistants.

Tools
-----
ingest_repo         Ingest a repository and build its call graph.
query_repo          Natural-language query against an ingested graph.
list_graphs         List all ingested repositories with metadata.
get_repo_overview   Full repo context: purpose, architecture, entry points.
impact_analysis     Change-impact analysis by function name.
delete_graph        Delete a stored graph and free its storage.

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
FASTAPI_BASE_URL = "http://localhost:8000"
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
        "delete_graph":     _handle_delete_graph,
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


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        logger.info("Flowify MCP server starting (6 tools)")
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
