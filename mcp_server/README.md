# Flowify MCP Server

Model Context Protocol (MCP) server for Flowify code graph analysis.

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Start FastAPI Backend

```bash
cd ../backend
uvicorn app.main:app --reload --port 8000
```

### 3. Configure VS Code

Add to your MCP configuration:

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

### 4. Test in VS Code

Ask your LLM assistant:

```
"What tools do you have available?"
```

You should see `ingest_repo` and `query_repo` tools.

## Usage Examples

### Ingest a Repository

```
User: "Please ingest the repository at /path/to/my/project"

LLM: [Calls ingest_repo]
✓ Repository ingested successfully
Graph ID: abc123def456
Functions: 45
Modules: 8
```

### Query the Code

```
User: "How does authentication work in this codebase?"

LLM: [Calls query_repo]
Based on the code analysis:

1. authenticate_user (/src/auth.py)
   Authenticates user credentials
   Intent: validation
   Criticality: high

2. check_password (/src/auth.py)
   Verifies password hash
   ...
```

## Tools

### ingest_repo

Ingest and analyze a code repository.

**Parameters:**
- `repo_path` (required): Path to repository
- `repo_id` (optional): Stable identifier

**Returns:** Graph ID and statistics

### query_repo

Query code graph with natural language.

**Parameters:**
- `graph_id` (required): From ingest_repo
- `query` (required): Natural language query
- `depth` (optional): Traversal depth (1-5)

**Returns:** Relevant functions and explanation

## Features

- ✅ Circuit breaker for fault tolerance
- ✅ Automatic retry with exponential backoff
- ✅ Request deduplication
- ✅ Health checking
- ✅ Comprehensive logging
- ✅ Idempotent ingestion

## Testing

```bash
# Unit tests
cd ../tests
pytest test_mcp_endpoints.py -v

# Integration tests (requires running FastAPI server)
pytest test_integration.py -v
```

## Configuration

Edit `flowify_mcp.py` to customize:

```python
# Timeout
REQUEST_TIMEOUT = 120.0

# Circuit breaker
circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=60.0
)
```

## Troubleshooting

### Connection Refused

**Problem:** Can't connect to FastAPI backend

**Solution:**
1. Start backend: `uvicorn app.main:app --port 8000`
2. Verify: `curl http://localhost:8000`

### Circuit Breaker Open

**Problem:** Service unavailable

**Solution:**
1. Check backend logs
2. Wait 60 seconds for recovery
3. Restart if needed

### Module Not Found

**Problem:** `ModuleNotFoundError: No module named 'mcp'`

**Solution:**
```bash
pip install -r requirements.txt
```

## Documentation

- [Full Integration Guide](../docs/MCP_INTEGRATION.md)
- [LLM Testing Guide](../tests/test_llm_integration.md)
- [Architecture](../docs/architecture.md)

## License

See [LICENSE](../LICENSE)