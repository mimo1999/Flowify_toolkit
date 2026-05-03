"""
Flowify MCP Server - Thin wrapper around FastAPI endpoints.

This MCP server exposes two tools:
1. ingest_repo - Ingest a repository and build its code graph
2. query_repo - Query the code graph with natural language

The server acts as a client to the Flowify FastAPI backend.

Features:
- Circuit breaker for fault tolerance
- Request deduplication for efficiency
- Retry logic with exponential backoff
- Health checking
- Comprehensive logging
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
    HealthChecker
)

# Configure logging with more detail
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("flowify-mcp")

# Configuration
FASTAPI_BASE_URL = "http://localhost:8000"
REQUEST_TIMEOUT = 120.0  # 2 minutes for ingestion operations

# Initialize MCP server and health checker
app = Server("flowify-mcp")
health_checker = HealthChecker(FASTAPI_BASE_URL)


def create_http_client() -> httpx.AsyncClient:
    """Create HTTP client with timeout configuration."""
    return httpx.AsyncClient(
        base_url=FASTAPI_BASE_URL,
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        follow_redirects=True
    )


async def make_resilient_request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """Make HTTP request with circuit breaker and retry logic."""
    
    # Check circuit breaker
    if not circuit_breaker.can_attempt():
        raise RuntimeError(
            f"Circuit breaker is OPEN. Service unavailable. "
            f"State: {circuit_breaker.get_state()}"
        )
    
    # Check health
    if not await health_checker.ensure_healthy(client):
        logger.warning("Backend health check failed, but attempting request anyway")
    
    # Make request with retry
    async def _request():
        if method.lower() == "get":
            return await client.get(url, **kwargs)
        elif method.lower() == "post":
            return await client.post(url, **kwargs)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
    
    try:
        response = await retry_with_backoff(
            _request,
            max_retries=2,
            initial_delay=1.0,
            exceptions=(httpx.TimeoutException, httpx.ConnectError)
        )
        circuit_breaker.record_success()
        return response
    except Exception as e:
        circuit_breaker.record_failure()
        logger.error(f"Request failed after retries: {e}")
        raise


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available MCP tools."""
    return [
        Tool(
            name="ingest_repo",
            description=(
                "Ingest a code repository and build its function-level call graph. "
                "This analyzes the repository structure, extracts functions, classes, "
                "and their relationships. Returns a graph_id for subsequent queries. "
                "Idempotent - will return existing graph if already ingested."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the repository root directory"
                    },
                    "repo_id": {
                        "type": "string",
                        "description": "Optional stable identifier for the repository"
                    }
                },
                "required": ["repo_path"]
            }
        ),
        Tool(
            name="query_repo",
            description=(
                "Query a code graph using natural language. Returns relevant functions, "
                "their execution flow, and an explanation of how they relate to the query. "
                "Use this to understand code flow, find implementations, or explore "
                "function relationships."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_id": {
                        "type": "string",
                        "description": "Graph ID returned from ingest_repo"
                    },
                    "query": {
                        "type": "string",
                        "description": "Natural language query about the code (e.g., 'how does authentication work?')"
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Maximum hops for graph traversal (1-5, default: 2)",
                        "minimum": 1,
                        "maximum": 5,
                        "default": 2
                    }
                },
                "required": ["graph_id", "query"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Handle tool calls by forwarding to FastAPI backend."""
    
    if name == "ingest_repo":
        return await handle_ingest_repo(arguments)
    elif name == "query_repo":
        return await handle_query_repo(arguments)
    else:
        raise ValueError(f"Unknown tool: {name}")


async def handle_ingest_repo(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle ingest_repo tool call with deduplication and resilience."""
    try:
        repo_path = arguments.get("repo_path")
        repo_id = arguments.get("repo_id")
        
        if not repo_path:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "repo_path is required"
                }, indent=2)
            )]
        
        logger.info(f"Ingesting repository: {repo_path}")
        
        # Use request deduplication for idempotent ingestion
        async def _ingest():
            async with create_http_client() as client:
                response = await make_resilient_request(
                    client,
                    "post",
                    "/mcp/ingest",
                    json={"repo_path": repo_path, "repo_id": repo_id}
                )
                response.raise_for_status()
                return response.json()
        
        result = await request_deduplicator.execute(
            "ingest",
            _ingest,
            repo_path=repo_path,
            repo_id=repo_id or ""
        )
        
        logger.info(f"Ingestion complete: graph_id={result.get('graph_id')}")
        
        # Format response for LLM
        if result.get("success"):
            summary = (
                f"✓ Repository ingested successfully\n\n"
                f"Graph ID: {result['graph_id']}\n"
                f"Repository: {result['repo_path']}\n"
                f"Functions: {result['function_count']}\n"
                f"Modules: {result['module_count']}\n"
            )
            
            # Add repository context if available
            if result.get("repo_context"):
                ctx = result["repo_context"]
                summary += f"\nRepository Type: {ctx.get('project_type', 'unknown')}\n"
                summary += f"Domain: {ctx.get('domain', 'general')}\n"
                summary += f"Architecture: {ctx.get('architecture', 'unknown')}\n"
                if ctx.get("purpose"):
                    summary += f"Purpose: {ctx['purpose']}\n"
            
            summary += f"\nUse graph_id '{result['graph_id']}' to query this repository."
            
            return [TextContent(type="text", text=summary)]
        else:
            return [TextContent(
                type="text",
                text=f"✗ Ingestion failed: {result.get('error', 'Unknown error')}"
            )]
            
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error during ingestion: {e}")
        return [TextContent(
            type="text",
            text=f"✗ HTTP error: {e.response.status_code} - {e.response.text}"
        )]
    except httpx.TimeoutException:
        logger.error("Ingestion timeout")
        return [TextContent(
            type="text",
            text="✗ Ingestion timed out. The repository may be too large or the server is unresponsive."
        )]
    except Exception as e:
        logger.error(f"Unexpected error during ingestion: {e}", exc_info=True)
        return [TextContent(
            type="text",
            text=f"✗ Unexpected error: {str(e)}"
        )]


async def handle_query_repo(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle query_repo tool call with resilience."""
    try:
        graph_id = arguments.get("graph_id")
        query = arguments.get("query")
        depth = arguments.get("depth", 2)
        
        if not graph_id or not query:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "graph_id and query are required"
                }, indent=2)
            )]
        
        logger.info(f"Querying graph {graph_id}: {query}")
        
        # Call FastAPI endpoint with resilience
        async with create_http_client() as client:
            response = await make_resilient_request(
                client,
                "post",
                "/mcp/query",
                json={"graph_id": graph_id, "query": query, "depth": depth}
            )
            response.raise_for_status()
            result = response.json()
        
        logger.info(f"Query complete: {len(result.get('relevant_functions', []))} functions found")
        
        # Format response for LLM
        if result.get("success"):
            summary = f"Query: {query}\n\n"
            summary += f"{result['explanation']}\n\n"
            
            # Add function details
            functions = result.get("relevant_functions", [])
            if functions:
                summary += f"Relevant Functions ({len(functions)}):\n"
                for i, func in enumerate(functions[:10], 1):  # Limit to top 10 for readability
                    summary += f"\n{i}. {func['name']} ({func['file_path']})\n"
                    if func.get('summary'):
                        summary += f"   {func['summary']}\n"
                    if func.get('intent'):
                        summary += f"   Intent: {func['intent']}\n"
                    if func.get('criticality'):
                        summary += f"   Criticality: {func['criticality']}\n"
                
                if len(functions) > 10:
                    summary += f"\n... and {len(functions) - 10} more functions\n"
            else:
                summary += "No relevant functions found for this query.\n"
            
            return [TextContent(type="text", text=summary)]
        else:
            return [TextContent(
                type="text",
                text=f"✗ Query failed: {result.get('error', 'Unknown error')}"
            )]
            
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error during query: {e}")
        return [TextContent(
            type="text",
            text=f"✗ HTTP error: {e.response.status_code} - {e.response.text}"
        )]
    except httpx.TimeoutException:
        logger.error("Query timeout")
        return [TextContent(
            type="text",
            text="✗ Query timed out. Try reducing the depth parameter or simplifying the query."
        )]
    except Exception as e:
        logger.error(f"Unexpected error during query: {e}", exc_info=True)
        return [TextContent(
            type="text",
            text=f"✗ Unexpected error: {str(e)}"
        )]


async def main():
    """Run the MCP server."""
    from mcp.server.stdio import stdio_server
    
    async with stdio_server() as (read_stream, write_stream):
        logger.info("Flowify MCP server starting...")
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())

# Made with Bob
