# Flowify AI

Dual-layer code graph (function + module) with GraphRAG querying via IBM Bob.

## 🚀 Quick Start - UI Access

**✅ Both servers are currently running!**

- **Frontend UI:** http://localhost:5173 (open in browser)
- **Backend API:** http://localhost:8000
- **API Docs:** http://localhost:8000/docs

Simply open **http://localhost:5173** in your browser to start using Flowify's visual interface.

## Quick start

### Backend
```bash
cd backend
python -m venv .venv && source .venv/Scripts/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Frontend
```bash
cd frontend
npm install
npm run dev
```

Frontend at http://localhost:5173, backend at http://localhost:8000.

## Environment

- `BOB_API_KEY` — optional; if unset, summaries fall back to heuristic stubs.
- `BOB_API_URL` — Bob endpoint (defaults to a placeholder).
- `FLOWIFY_STORE` — directory for graph JSON storage (default `./_store`).

## API

### REST Endpoints

- `POST /ingest_repo` `{repo_path}` → `{graph_id}`
- `GET  /graph?graph_id=...&depth=2` → module + function graph JSON
- `POST /query` `{graph_id, query}` → flow explanation + subgraph
- `POST /update` `{graph_id}` → re-ingests changed files via git diff

### MCP Endpoints (for LLM assistants)

- `POST /mcp/ingest` `{repo_path, repo_id?}` → normalized response with idempotent behavior
- `POST /mcp/query` `{graph_id, query, depth?}` → structured function metadata

## MCP Server (for Bob/Claude)

The Flowify MCP server provides two tools for LLM assistants:

### Quick Setup

```powershell
# 1. Install MCP server
cd mcp_server
.\setup.ps1

# 2. Start FastAPI backend
cd ..\backend
uvicorn app.main:app --port 8000

# 3. Restart VS Code
# 4. Ask Bob: "What tools do you have available?"
```

### Available Tools

**`ingest_repo`** - Ingest and analyze a code repository
```
User: "Ingest the repository at D:/Projects/myapp"
Bob: [Uses ingest_repo tool]
✓ Repository ingested successfully
Graph ID: abc123
Functions: 45, Modules: 8
```

**`query_repo`** - Query code with natural language
```
User: "How does authentication work?"
Bob: [Uses query_repo tool]
Based on the code analysis:
1. authenticate_user (/src/auth.py) - Validates credentials
2. check_password (/src/auth.py) - Verifies password hash
...
```

See [`mcp_server/README.md`](mcp_server/README.md) for details.

## Bob / MCP Direct Usage

HTTP:
```bash
curl -X POST http://localhost:8000/bob/graph \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/path/to/repo", "depth": 3}'
```

CLI, from `backend/`:
```bash
python -m app.bob_graph_cli --repo-path /path/to/repo --depth 3
```

The response includes `schema_version`, `graph_id`, `repo_context`, `stats`, a query-friendly `view`, and the full CIR `graph` payload.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full phase breakdown.
