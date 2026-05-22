# Flowify AI

Interactive code-graph explorer with GraphRAG querying. Ingests a repository into a dual-layer graph (function-level + module-level), lets you expand nodes on-click, and answers natural-language questions about your codebase via IBM Bob (or heuristic stubs when no API key is set).

## Quick start

### One-command launch (recommended)

```bash
bash run.sh          # starts backend + frontend, opens http://localhost:5173
bash run.sh --test   # same, plus smoke-tests every API endpoint
```

### Manual

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

Frontend → http://localhost:5173 · Backend → http://localhost:8000 · API docs → http://localhost:8000/docs

## Usage

1. Paste an absolute path to a local repository in the sidebar and click **Ingest**.
2. Up to 4 entry-point files appear as root nodes on the left.
3. **Click a node** to expand it — files expand to their callees, then to their functions.
4. Use the **"Drill into functions"** tooltip button on a file node to see its symbols directly.
5. Use the **Query** bar to ask natural-language questions; matching nodes are highlighted.
6. **← Back** restores the previous view; **⟲ Reset** returns to the entry-point roots.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BOB_API_KEY` | *(unset)* | IBM Bob API key; falls back to heuristic stubs if unset |
| `BOB_API_URL` | *(placeholder)* | Bob endpoint URL |
| `FLOWIFY_STORE` | `./_store` | Directory for persisted graph JSON |

## API reference

### UI-facing endpoints

| Method | Path | Body / Params | Returns |
|---|---|---|---|
| `POST` | `/ingest_repo` | `{repo_path}` | `{graph_id, function_count, module_count}` |
| `GET` | `/entry_points` | `?graph_id=&max_count=4` | Root file nodes for the initial view |
| `GET` | `/expand` | `?graph_id=&node_id=&action=callees\|functions` | `{children, edges}` |
| `GET` | `/graph` | `?graph_id=&depth=1..3` | Full graph at the requested depth |
| `POST` | `/query` | `{graph_id, query}` | `{explanation, path, query_id}` |
| `POST` | `/feedback` | `{graph_id, query_id, rating}` | Feedback acknowledgement |
| `POST` | `/update` | `{graph_id}` | Re-ingests changed files via git diff |

### Analysis endpoints

| Method | Path | Returns |
|---|---|---|
| `GET` | `/repo_context?graph_id=` | Bob Phase 1 repository analysis |
| `GET` | `/semantic_analysis?graph_id=` | Per-function semantic metadata (Phase 2) |
| `GET` | `/analytics?graph_id=` | Query usage statistics (Phase 3) |
| `GET` | `/hot_nodes?graph_id=&limit=10` | Most-queried functions |
| `GET` | `/module_details?graph_id=&module_id=` | Module with control-flow groups |

### MCP endpoints (for LLM assistants)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/mcp/ingest` | Idempotent; returns stable `repo_id` |
| `POST` | `/mcp/query` | Structured function list + execution path |
| `POST` | `/bob/graph` | Full Bob-ready graph payload (CLI / agent use) |

## MCP server (Bob / Claude integration)

```powershell
cd mcp_server
.\setup.ps1        # installs the MCP server into VS Code
# restart VS Code, then ask Bob: "What tools do you have available?"
```

Bob CLI (from `backend/`):

```bash
python -m app.bob_graph_cli --repo-path /path/to/repo --depth 3
```

See [`mcp_server/README.md`](mcp_server/README.md) for full setup details.

## Architecture

The pipeline runs in three phases powered by IBM Bob:

1. **Repo context** — Bob analyses the repository structure and identifies entry points, tech stack, and architecture pattern.
2. **Semantic enrichment** — per-function intent, complexity, and criticality scores.
3. **Continuous learning** — query feedback adjusts relevance scores and builds a terminology map.

The graph itself is language-neutral (Python, JS/TS, Java, C/C++ supported) via a Canonical Intermediate Representation (CIR). Module clustering uses greedy modularity community detection (NetworkX).

See [docs/architecture.md](docs/architecture.md) for the full breakdown.
