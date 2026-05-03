# LLM-Level Integration Tests for VS Code

This document describes how to test the Flowify MCP server integration with VS Code and LLM assistants (like Claude or GPT-4).

## Prerequisites

1. **FastAPI Server Running**
   ```bash
   cd backend
   uvicorn app.main:app --reload --port 8000
   ```

2. **MCP Server Installed**
   ```bash
   cd mcp_server
   pip install -r requirements.txt
   ```

3. **VS Code with MCP Support**
   - Install Claude Dev or similar MCP-compatible extension
   - Configure MCP server in VS Code settings

## VS Code MCP Configuration

Add to your VS Code `settings.json` or MCP configuration file:

```json
{
  "mcpServers": {
    "flowify": {
      "command": "python",
      "args": ["-m", "flowify_mcp"],
      "cwd": "/path/to/Flowify/mcp_server",
      "env": {
        "FASTAPI_BASE_URL": "http://localhost:8000"
      }
    }
  }
}
```

## Test Scenarios

### Test 1: Tool Discovery

**Objective**: Verify LLM can discover and understand available tools.

**Steps**:
1. Open VS Code with MCP extension
2. Ask the LLM: "What tools do you have available?"
3. Verify response mentions `ingest_repo` and `query_repo`

**Expected Behavior**:
- LLM lists both tools
- Descriptions are clear and actionable
- Parameter requirements are understood

**Success Criteria**:
```
✓ LLM identifies ingest_repo tool
✓ LLM identifies query_repo tool
✓ LLM understands tool purposes
✓ LLM knows required parameters
```

---

### Test 2: Repository Ingestion

**Objective**: Verify LLM can correctly use ingest_repo tool.

**Steps**:
1. Ask: "Please ingest the repository at /path/to/test/repo"
2. Observe tool call and response
3. Verify graph_id is captured

**Expected Behavior**:
- LLM calls `ingest_repo` with correct repo_path
- Response includes graph_id
- LLM acknowledges successful ingestion
- LLM stores graph_id for subsequent queries

**Success Criteria**:
```
✓ Tool called with correct parameters
✓ graph_id returned and noted
✓ Function/module counts displayed
✓ Repository context shown (if available)
```

**Example Conversation**:
```
User: "Ingest the repository at ./backend"

LLM: I'll ingest the repository for you.
[Calls ingest_repo with repo_path="./backend"]

✓ Repository ingested successfully

Graph ID: abc123def456
Repository: ./backend
Functions: 45
Modules: 8

Repository Type: web_api
Domain: code_analysis
Architecture: modular_pipeline

Use graph_id 'abc123def456' to query this repository.
```

---

### Test 3: Code Query

**Objective**: Verify LLM can query ingested repository.

**Steps**:
1. After ingestion, ask: "How does the authentication work in this codebase?"
2. Observe query_repo tool call
3. Verify meaningful response

**Expected Behavior**:
- LLM uses graph_id from previous ingestion
- Calls `query_repo` with natural language query
- Receives and interprets function list
- Provides coherent explanation

**Success Criteria**:
```
✓ Correct graph_id used
✓ Query properly formatted
✓ Relevant functions returned
✓ LLM synthesizes explanation
✓ Function details included (file paths, summaries)
```

**Example Conversation**:
```
User: "How does authentication work?"

LLM: Let me query the code graph to find authentication-related functions.
[Calls query_repo with graph_id="abc123", query="how does authentication work?"]

Based on the code analysis:

The authentication flow involves these key functions:

1. authenticate_user (/src/auth.py)
   Authenticates user credentials
   Intent: validation
   Criticality: high

2. check_password (/src/auth.py)
   Verifies password hash

3. generate_token (/src/auth.py)
   Creates JWT authentication token

The authentication process starts with authenticate_user, which validates
credentials by calling check_password, and then generates a session token
using generate_token.
```

---

### Test 4: Multi-Step Workflow

**Objective**: Verify LLM can handle complex multi-step workflows.

**Steps**:
1. Ask: "Analyze the backend repository and tell me about the main entry points"
2. Observe: Should ingest, then query
3. Verify: Coherent multi-step execution

**Expected Behavior**:
- LLM recognizes need to ingest first
- Automatically calls ingest_repo
- Then calls query_repo for entry points
- Synthesizes comprehensive answer

**Success Criteria**:
```
✓ Autonomous tool selection
✓ Correct tool ordering (ingest → query)
✓ Context maintained between calls
✓ Comprehensive final answer
```

---

### Test 5: Error Handling

**Objective**: Verify LLM handles errors gracefully.

**Test 5a: Invalid Repository Path**
```
User: "Ingest /nonexistent/path"

Expected:
✓ LLM calls tool
✓ Receives error response
✓ Communicates error to user
✓ Suggests corrective action
```

**Test 5b: Query Before Ingestion**
```
User: "Query graph_id xyz123"

Expected:
✓ LLM attempts query
✓ Receives "graph not found" error
✓ Explains issue to user
✓ Suggests ingesting repository first
```

**Test 5c: Malformed Query**
```
User: "Query the repository" (without specifying graph_id)

Expected:
✓ LLM recognizes missing information
✓ Asks user for graph_id or repo path
✓ Does not make invalid tool call
```

---

### Test 6: Tool Parameter Validation

**Objective**: Verify LLM correctly handles tool parameters.

**Test Cases**:

1. **Optional Parameters**
   ```
   User: "Ingest ./backend with custom repo_id 'my-backend'"
   Expected: ✓ Both repo_path and repo_id passed
   ```

2. **Depth Parameter**
   ```
   User: "Query with depth 3"
   Expected: ✓ Depth parameter included in query_repo call
   ```

3. **Parameter Inference**
   ```
   User: "Query the backend about authentication"
   Expected: ✓ LLM infers need to ingest first or use existing graph_id
   ```

---

### Test 7: Idempotency

**Objective**: Verify idempotent ingestion behavior.

**Steps**:
1. Ingest repository: "Ingest ./backend"
2. Note graph_id (e.g., "abc123")
3. Ingest again: "Ingest ./backend again"
4. Verify same graph_id returned

**Expected Behavior**:
```
✓ First ingestion creates new graph
✓ Second ingestion returns existing graph
✓ Same graph_id both times
✓ LLM recognizes repository already ingested
```

---

## Manual Testing Checklist

Use this checklist when manually testing in VS Code:

- [ ] MCP server starts without errors
- [ ] FastAPI backend is accessible
- [ ] LLM discovers both tools
- [ ] Tool descriptions are clear
- [ ] ingest_repo works with valid path
- [ ] ingest_repo handles invalid path
- [ ] query_repo works with valid graph_id
- [ ] query_repo handles invalid graph_id
- [ ] Depth parameter works (1-5)
- [ ] Idempotent ingestion verified
- [ ] Multi-step workflows execute correctly
- [ ] Error messages are informative
- [ ] LLM provides helpful responses
- [ ] Function details are displayed
- [ ] Execution paths are shown
- [ ] Repository context included

---

## Automated LLM Testing (Future)

For automated LLM-level testing, consider:

1. **LLM Test Harness**
   - Script that sends prompts to LLM
   - Captures tool calls
   - Validates responses

2. **Test Scenarios as Code**
   ```python
   async def test_llm_ingest_and_query():
       llm = LLMTestClient()
       
       # Send prompt
       response = await llm.send("Ingest ./backend and find auth functions")
       
       # Verify tool calls
       assert response.tool_calls[0].name == "ingest_repo"
       assert response.tool_calls[1].name == "query_repo"
       
       # Verify final response
       assert "authenticate" in response.text.lower()
   ```

3. **Regression Testing**
   - Save successful conversations
   - Replay to verify consistency
   - Detect regressions in tool usage

---

## Troubleshooting

### MCP Server Not Found
```
Error: MCP server 'flowify' not found

Solution:
1. Check VS Code MCP configuration
2. Verify Python path in config
3. Ensure mcp_server directory in PYTHONPATH
4. Restart VS Code
```

### Connection Refused
```
Error: Connection refused to http://localhost:8000

Solution:
1. Start FastAPI server: uvicorn app.main:app --port 8000
2. Verify server is running: curl http://localhost:8000
3. Check FASTAPI_BASE_URL in MCP config
```

### Tool Calls Timeout
```
Error: Tool call timed out

Solution:
1. Check FastAPI server logs
2. Verify repository path is valid
3. Increase timeout in flowify_mcp.py (REQUEST_TIMEOUT)
4. For large repos, consider ingesting in background
```

---

## Success Metrics

A successful LLM integration should demonstrate:

1. **Discoverability**: LLM finds and understands tools
2. **Correctness**: Tool calls use correct parameters
3. **Robustness**: Errors handled gracefully
4. **Usability**: Natural language queries work
5. **Efficiency**: Multi-step workflows execute smoothly
6. **Reliability**: Consistent behavior across sessions

---

## Next Steps

After successful LLM-level testing:

1. Document common usage patterns
2. Create example prompts library
3. Build LLM-specific optimizations
4. Add streaming support for large results
5. Implement caching for repeated queries
6. Add progress indicators for long operations