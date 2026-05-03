# Enabling Bob to Access Flowify UI

This guide explains how to make the Flowify frontend accessible to Bob so it can interact with the visual graph interface.

## Quick Start

### Option 1: Use the Run Script (Recommended)

The easiest way to start both backend and frontend together:

```bash
# On Linux/Mac/Git Bash
./run.sh

# The script will:
# - Create/reuse Python venv
# - Install dependencies
# - Start backend on http://127.0.0.1:8000
# - Start frontend on http://127.0.0.1:5173
```

### Option 2: Start Servers Separately

**Backend (Terminal 1):**
```powershell
# PowerShell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Frontend (Terminal 2):**
```powershell
# PowerShell
cd frontend
npm install
npm run dev
```

## Making UI Accessible to Bob

Once the servers are running, Bob can access the UI in several ways:

### 1. Direct Browser Access

Tell Bob to open the frontend URL:
```
"Open http://localhost:5173 in a browser"
```

Bob can then:
- Take screenshots of the UI
- Analyze the visual graph
- Provide feedback on the interface

### 2. API-Based Interaction

Bob can interact with the backend API directly to:
- Ingest repositories
- Query the graph
- Retrieve function details

Example commands for Bob:
```
"Use the MCP tools to ingest this repository and show me the graph"
"Query the graph for 'how does authentication work'"
```

### 3. MCP Server Integration

The MCP server provides Bob with direct access to Flowify's capabilities:

**Available Tools:**
- `ingest_repo` - Analyze a repository
- `query_repo` - Query the code graph

**Example Usage:**
```
"Use the ingest_repo tool to analyze D:/Projects/my-project"
"Use the query_repo tool to find all authentication-related functions"
```

## Verifying Access

### Check Backend is Running
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/" -Method Get
```

Expected output: `{"message": "Flowify API"}`

### Check Frontend is Running
Open http://localhost:5173 in your browser. You should see the Flowify interface.

### Check MCP Server
```powershell
cd mcp_server
python flowify_mcp.py
```

The MCP server should start and be ready to accept tool calls from Bob.

## Port Configuration

Default ports:
- **Backend:** 8000
- **Frontend:** 5173
- **MCP Server:** Uses HTTP client to connect to backend

To change ports:
```bash
# Backend
BACKEND_PORT=9000 ./run.sh

# Frontend
FRONTEND_PORT=3000 ./run.sh

# Both
BACKEND_PORT=9000 FRONTEND_PORT=3000 ./run.sh
```

## Troubleshooting

### Frontend Not Loading
1. Check if Node.js is installed: `node --version`
2. Check if npm is installed: `npm --version`
3. Verify frontend dependencies: `cd frontend && npm install`
4. Check logs: `.run-logs/frontend.log`

### Backend Not Responding
1. Check if Python is installed: `python --version`
2. Verify backend dependencies: `cd backend && pip install -r requirements.txt`
3. Check logs: `.run-logs/backend.log`

### MCP Server Connection Issues
1. Verify backend is running on port 8000
2. Check MCP server configuration: `mcp_server/config.json`
3. Verify environment variables in `.claude/mcp.json`

## Bob-Specific Instructions

When Bob needs to interact with the UI:

1. **Start the servers** using `./run.sh` or the PowerShell commands above
2. **Verify access** by checking http://localhost:8000 and http://localhost:5173
3. **Use MCP tools** for programmatic access to Flowify's features
4. **Take screenshots** if visual analysis is needed
5. **Query the API** directly for specific data

## Example Bob Workflow

```
User: "Show me the Flowify UI"

Bob Actions:
1. Check if servers are running
2. If not, suggest: "Let me start the servers for you"
3. Execute: ./run.sh (or PowerShell equivalent)
4. Wait for servers to be ready
5. Open http://localhost:5173 in browser
6. Take screenshot and show to user
7. Offer to ingest a repository or run queries
```

## Security Notes

- Backend binds to `0.0.0.0` (all interfaces) by default
- Frontend binds to `127.0.0.1` (localhost only) by default
- For production, use proper authentication and HTTPS
- MCP server communicates with backend via HTTP (localhost only)

## Additional Resources

- [MCP Integration Guide](MCP_INTEGRATION.md)
- [Architecture Documentation](architecture.md)
- [API Documentation](http://localhost:8000/docs) (when backend is running)