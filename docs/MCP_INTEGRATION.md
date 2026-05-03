# Flowify MCP Server Integration Guide

Complete guide for the Flowify MCP (Model Context Protocol) server integration with FastAPI backend.

## Overview

The Flowify MCP server provides a thin, resilient wrapper around FastAPI endpoints, exposing two primary tools for LLM assistants:

1. **`ingest_repo`** - Ingest and analyze code repositories
2. **`query_repo`** - Query code graphs with natural language

## Architecture

```
┌─────────────────┐
│   LLM/Claude    │
│   (VS Code)     │
└────────┬────────┘
         │ MCP Protocol
         ↓
┌─────────────────┐
│  MCP Server     │
│  (flowify_mcp)  │
│                 │
│  - Circuit      │
│    Breaker      │
│  - Retry Logic  │
│  - Dedup        │
│  - Health Check │
└────────┬────────┘
         │ HTTP/REST
         ↓
┌─────────────────┐
│  FastAPI        │
│  Backend        │
│                 │
│  /mcp/ingest    │
│  /mcp/query     │
└─────────────────┘
```

## Installation

### 1. Backend Setup

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### 2. MCP Server Setup

```bash
cd mcp_server
pip install -r requirements.txt
```

### 3. VS Code Configuration

Add to your MCP configuration (`.vscode/mcp.json` or similar):

```json
{
  "mcpServers": {
    "flowify": {
      "command": "python",
      "args": ["-m", "flowify_mcp"],
      "cwd": "/absolute/path/to/Flowify/mcp_server",
      "env": {
        "FASTAPI_BASE_URL": "http://localhost:8000"
      }
    }
  }
}
```

## API Contracts

### FastAPI Endpoints

#### POST /mcp/ingest

**Request:**
```json
{
  "repo_path": "/path/to/repository",
  "repo_id": "optional-stable-id"
}
```

**Response:**
```json
{
  "success": true,
  "graph_id": "abc123def456",
  "repo_id": "generated-or-custom-id",
  "repo_path": "/path/to/repository",
  "function_count": 45,
  "module_count": 8,
  "repo_context": {
    "project_type": "web_api",
    "domain": "code_analysis",
    "architecture": "modular_pipeline",
    "purpose": "Code graph analysis tool",
    "tech_stack": ["python", "fastapi"],
    "confidence": 0.95
  }
}
```

**Error Response:**
```json
{
  "success": false,
  "graph_id": "",
  "repo_id": "...",
  "repo_path": "/path/to/repository",
  "function_count": 0,
  "module_count": 0,
  "error": "Error message here"
}
```

#### POST /mcp/query

**Request:**
```json
{
  "graph_id": "abc123def456",
  "query": "how does authentication work?",
  "depth": 2
}
```

**Response:**
```json
{
  "success": true,
  "graph_id": "abc123def456",
  "query": "how does authentication work?",
  "explanation": "The authentication flow involves...",
  "relevant_functions": [
    {
      "id": "func_123",
      "name": "authenticate_user",
      "file_path": "/src/auth.py",
      "type": "function",
      "summary": "Authenticates user credentials",
      "intent": "validation",
      "complexity": "medium",
      "criticality": "high"
    }
  ],
  "execution_path": ["func_123", "func_456", "func_789"],
  "query_id": "query-uuid-here"
}
```

## MCP Tools

### ingest_repo

**Description:** Ingest a code repository and build its function-level call graph.

**Parameters:**
- `repo_path` (required): Absolute path to repository root
- `repo_id` (optional): Stable identifier for the repository

**Returns:** Graph ID and repository statistics

**Example Usage:**
```
User: "Please ingest the repository at /home/user/projects/myapp"

LLM: [Calls ingest_repo]
✓ Repository ingested successfully
Graph ID: abc123def456
Functions: 45
Modules: 8
```

### query_repo

**Description:** Query a code graph using natural language.

**Parameters:**
- `graph_id` (required): Graph ID from ingest_repo
- `query` (required): Natural language query
- `depth` (optional): Max hops for traversal (1-5, default: 2)

**Returns:** Relevant functions, execution flow, and explanation

**Example Usage:**
```
User: "How does authentication work in this codebase?"

LLM: [Calls query_repo with graph_id and query]
Based on the code analysis:

1. authenticate_user (/src/auth.py)
   Authenticates user credentials
   Intent: validation, Criticality: high

2. check_password (/src/auth.py)
   Verifies password hash
   ...
```

## Resilience Features

### 1. Circuit Breaker

Prevents cascading failures by temporarily blocking requests to a failing service.

**States:**
- **CLOSED**: Normal operation
- **OPEN**: Service failing, requests rejected
- **HALF_OPEN**: Testing recovery

**Configuration:**
```python
circuit_breaker = CircuitBreaker(
    failure_threshold=5,      # Open after 5 failures
    recovery_timeout=60.0,    # Try recovery after 60s
    half_open_max_calls=3     # Test with 3 calls
)
```

### 2. Request Deduplication

Prevents duplicate concurrent requests for the same operation.

**Benefits:**
- Reduces server load
- Improves response time
- Ensures idempotent ingestion

**Example:**
```python
# Multiple concurrent ingest requests for same repo
# Only one actual ingestion occurs, others wait and share result
result = await request_deduplicator.execute(
    "ingest",
    _ingest_func,
    repo_path="/path/to/repo"
)
```

### 3. Retry with Exponential Backoff

Automatically retries failed requests with increasing delays.

**Configuration:**
```python
await retry_with_backoff(
    func,
    max_retries=2,
    initial_delay=1.0,
    max_delay=30.0,
    backoff_factor=2.0
)
```

### 4. Health Checking

Periodically checks FastAPI backend health.

**Features:**
- Automatic health checks every 30s
- Tracks consecutive failures
- Provides health status to circuit breaker

## Error Handling

### Client-Side Errors

**Invalid Parameters:**
```json
{
  "success": false,
  "error": "repo_path is required"
}
```

**Graph Not Found:**
```json
{
  "success": false,
  "error": "Graph not found"
}
```

### Server-Side Errors

**Circuit Breaker Open:**
```
RuntimeError: Circuit breaker is OPEN. Service unavailable.
State: {'state': 'open', 'failure_count': 5, ...}
```

**Timeout:**
```
✗ Query timed out. Try reducing the depth parameter or simplifying the query.
```

**Connection Error:**
```
✗ HTTP error: 503 - Service Unavailable
```

## Testing

### Unit Tests

```bash
cd tests
pytest test_mcp_endpoints.py -v
```

Tests include:
- Successful ingestion
- Idempotent behavior
- Query execution
- Error handling
- Parameter validation

### Integration Tests

```bash
# Start FastAPI server first
cd backend
uvicorn app.main:app --port 8000

# Run integration tests
cd tests
pytest test_integration.py -v
```

Tests include:
- Complete ingest → query flow
- Multiple queries on same graph
- Error scenarios
- Performance benchmarks

### LLM-Level Tests

See [`tests/test_llm_integration.md`](../tests/test_llm_integration.md) for manual testing procedures in VS Code.

## Performance Considerations

### Ingestion

- **Small repos (<100 files)**: ~5-10 seconds
- **Medium repos (100-1000 files)**: ~30-60 seconds
- **Large repos (>1000 files)**: ~2-5 minutes

**Optimization:**
- Idempotent ingestion (cached results)
- Request deduplication
- Background processing (future)

### Queries

- **Simple queries**: <1 second
- **Complex queries (depth 3-5)**: 1-3 seconds

**Optimization:**
- Function limit (top 20 returned)
- Depth parameter tuning
- Result caching (future)

## Configuration

### Environment Variables

```bash
# FastAPI backend URL
export FASTAPI_BASE_URL="http://localhost:8000"

# Request timeout (seconds)
export REQUEST_TIMEOUT="120"

# Circuit breaker settings
export CIRCUIT_FAILURE_THRESHOLD="5"
export CIRCUIT_RECOVERY_TIMEOUT="60"

# Logging level
export LOG_LEVEL="INFO"
```

### MCP Server Configuration

Edit `mcp_server/flowify_mcp.py`:

```python
# Timeout configuration
REQUEST_TIMEOUT = 120.0  # 2 minutes

# Circuit breaker
circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=60.0,
    half_open_max_calls=3
)

# Request deduplication TTL
request_deduplicator = RequestDeduplicator(ttl=300.0)
```

## Monitoring and Logging

### Log Levels

```python
# Set in flowify_mcp.py
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

### Key Log Messages

**Ingestion:**
```
INFO - Ingesting repository: /path/to/repo
INFO - Ingestion complete: graph_id=abc123, functions=45
```

**Query:**
```
INFO - Querying graph abc123: how does auth work?
INFO - Query complete: 5 functions retrieved
```

**Resilience:**
```
WARNING - Attempt 1 failed: Connection timeout. Retrying in 1.0s...
ERROR - Circuit breaker: Threshold reached (5 failures), opening circuit
INFO - Circuit breaker: Recovery timeout elapsed, entering half-open state
```

## Troubleshooting

### MCP Server Won't Start

**Problem:** `ModuleNotFoundError: No module named 'mcp'`

**Solution:**
```bash
cd mcp_server
pip install -r requirements.txt
```

### Connection Refused

**Problem:** `Connection refused to http://localhost:8000`

**Solution:**
1. Start FastAPI server: `uvicorn app.main:app --port 8000`
2. Verify: `curl http://localhost:8000`
3. Check firewall settings

### Circuit Breaker Open

**Problem:** `Circuit breaker is OPEN. Service unavailable.`

**Solution:**
1. Check FastAPI server logs for errors
2. Wait for recovery timeout (60s)
3. Fix underlying issue
4. Restart MCP server if needed

### Slow Ingestion

**Problem:** Ingestion takes too long

**Solution:**
1. Check repository size
2. Verify no network issues
3. Increase `REQUEST_TIMEOUT`
4. Consider background processing

## Future Enhancements

### Streaming Support

Stream large query results incrementally:

```python
async def stream_query_results(graph_id, query):
    async for chunk in query_stream(graph_id, query):
        yield chunk
```

### Advanced Caching

Multi-level caching strategy:

```python
# L1: In-memory cache (hot data)
# L2: Redis cache (shared across instances)
# L3: Disk cache (persistent)
```

### Finer-Grained Tools

Additional MCP tools:

- `get_function_details` - Deep dive into specific function
- `trace_execution_path` - Follow call chain
- `find_similar_code` - Semantic code search
- `analyze_dependencies` - Dependency analysis

### Batch Operations

Process multiple repositories:

```python
await ingest_repos([
    "/path/to/repo1",
    "/path/to/repo2",
    "/path/to/repo3"
])
```

## Best Practices

### 1. Use Stable repo_id

```python
# Good: Consistent identifier
ingest_repo(repo_path="/path/to/repo", repo_id="my-project")

# Avoid: Auto-generated IDs change
ingest_repo(repo_path="/path/to/repo")
```

### 2. Tune Depth Parameter

```python
# Quick overview
query_repo(graph_id, query, depth=1)

# Detailed analysis
query_repo(graph_id, query, depth=3)

# Avoid: Too deep (slow)
query_repo(graph_id, query, depth=5)
```

### 3. Handle Errors Gracefully

```python
try:
    result = await ingest_repo(repo_path)
except RuntimeError as e:
    if "Circuit breaker" in str(e):
        # Wait and retry
        await asyncio.sleep(60)
        result = await ingest_repo(repo_path)
```

### 4. Monitor Circuit Breaker

```python
state = circuit_breaker.get_state()
if state["state"] == "open":
    logger.warning("Service degraded, using fallback")
```

## Support

For issues, questions, or contributions:

- **GitHub Issues**: [Project Issues](https://github.com/your-org/flowify/issues)
- **Documentation**: [docs/](../docs/)
- **Tests**: [tests/](../tests/)

## License

See [LICENSE](../LICENSE) file for details.