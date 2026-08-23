# Flowify AI

> **Interactive GraphRAG code explorer** — ingest any repository into a dual-layer call graph, navigate it visually, and answer natural-language questions grounded in the actual graph structure.

Flowify ingests a codebase through three phases (repo context → AST/semantic enrichment → continuous learning), renders it with ReactFlow + Dagre auto-layout, and lets you explore and query it through a dark-themed UI. The LLM backend is fully pluggable — Ollama (local, free) is recommended for development; IBM watsonx, Claude, OpenAI, and others are supported.

**Live demo**: [flowify-7vcn.onrender.com](https://flowify-7vcn.onrender.com/) — paste a public git URL and explore. Hosted on Render's free tier, so it sleeps after 15 minutes idle (~30-60s cold start on the first request).

---

## Features

- **Interactive call graph** — dual-layer graph (function-level + module-level) with Dagre auto-layout. Drill down from module clusters → source files → individual functions/classes.
- **Per-node descriptions** — every node shows a one-line description of what that function/class/module does, derived from its docstring or code via AST analysis (or an LLM when one is configured).
- **Semantic edge types** — edges are colour-coded: blue = CALLS, green = EXPOSES_API, purple = USES_DB, yellow = EMITS_EVENT, red = CONSUMES_EVENT.
- **Change Impact analysis** — click any function to see its risk level (low/medium/high/critical), caller list, DB operations it touches, and affected modules.
- **Graph-grounded NLP queries** — ask questions in plain English; answers are anchored to real graph traversal with a "Grounded in N nodes" badge, numbered call-chain steps, and 👍/😐/👎 feedback.
- **Continuous learning** — query patterns and feedback adjust relevance scores and build a terminology map over time.

---

## Screenshots

### Graph exploration — drill-down from files to functions

After ingestion the graph loads instantly. The **Drill-down View** selector (Explore · Modules · Files · Functions) switches abstraction levels. Each node card shows the file/function name, path, symbol count, and a code-based one-line description.

![Flowify graph — Files depth view showing 103 source files](docs/screenshots/04_files_depth.png)

### Natural-language queries with graph-grounded answers

Ask any question about the codebase. The answer is grounded in real graph traversal — every response shows which nodes were consulted, numbered execution steps, and a **Copy context for LLM** export button.

![Query results — grounded in 35 graph nodes](docs/screenshots/06_query_results.png)

---

## Quick start

### One-command launch (recommended)

```bash
bash run.sh          # starts backend + frontend → http://localhost:5173
bash run.sh --test   # same, plus smoke-tests every API endpoint
```

### Manual

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

Frontend → http://localhost:5173 · Backend → http://localhost:8000 · Swagger → http://localhost:8000/docs

---

## LLM providers

### Ollama — recommended for local development

Install [Ollama](https://ollama.com), pull a code model, start the server, and Flowify auto-detects it:

```bash
ollama pull qwen2.5-coder:1.5b   # ~1 GB, fast on CPU
# or larger models:
# ollama pull codellama           # 4 GB
# ollama pull deepseek-coder      # 7 GB

# Ollama starts automatically after install, or run manually:
ollama serve
```

Flowify tries models in this priority order (picks the first installed):
`codellama → deepseek-coder → qwen2.5-coder → llama3.x → mistral → phi3 → gemma2`

Override with env vars:
```bash
OLLAMA_HOST=http://localhost:11434   # default
OLLAMA_MODEL=codellama               # force a specific model
```

### All providers

| `LLM_PROVIDER` value | Provider | Required env var(s) |
|---|---|---|
| *(unset)* | **Auto-detect** — Ollama first, then cloud keys | — |
| `ollama` | Ollama (local) | *(none — just needs `ollama serve`)* |
| `bob` | IBM watsonx / Bob | `BOB_API_KEY`, `BOB_API_URL` |
| `claude` | Anthropic Claude | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI | `OPENAI_API_KEY` |
| `copilot` | GitHub Copilot | `GITHUB_TOKEN` |
| `openclaw` | OpenClaw | `OPENCLAW_API_KEY`, `OPENCLAW_API_URL` |
| `heuristic` | No LLM — stubs only (fast, offline) | *(none)* |

### LLM cache

All provider responses are cached to `_store/llm_cache/` keyed by SHA256 of the prompt. The first ingestion of a large repo may take minutes (Ollama makes one call per function); subsequent runs are instant. Delete cache files manually to force re-summarisation.

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | *(auto-detect)* | Provider selection (see table above) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | *(auto-pick best installed)* | Force a specific Ollama model |
| `BOB_API_KEY` | *(unset)* | IBM Bob / watsonx API key |
| `BOB_API_URL` | IBM endpoint | IBM Bob API URL |
| `ANTHROPIC_API_KEY` | *(unset)* | Anthropic Claude API key |
| `ANTHROPIC_MODEL` | `claude-3-5-haiku-20241022` | Claude model |
| `OPENAI_API_KEY` | *(unset)* | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model |
| `GITHUB_TOKEN` | *(unset)* | GitHub token for Copilot |
| `OPENCLAW_API_KEY` | *(unset)* | OpenClaw API key |
| `OPENCLAW_API_URL` | *(unset)* | OpenClaw endpoint |
| `FLOWIFY_STORE` | `./_store` | Graph + cache storage directory |
| `FLOWIFY_MODE` | `local` | `local` (desktop use) or `server` (hosted deploy) — see [Deployment](#deployment) |
| `FLOWIFY_FRONTEND_DIST` | *(unset)* | Path to a built `frontend/dist`; if set, the backend serves it at `/` |
| `FLOWIFY_ALLOWED_ROOTS` | *(unset)* | `server` mode only: `os.pathsep`-separated local paths ingest is allowed to read |
| `FLOWIFY_WORKDIR` | system temp dir | Where git-URL clones are made before ingest, then deleted |
| `FLOWIFY_MAX_CLONE_MB` | `100` | Reject a git-URL clone larger than this |
| `FLOWIFY_CORS_ORIGINS` | `*` | Comma-separated allowed origins |
| `FLOWIFY_GRAPH_TTL_H` | `24` | `server` mode only: delete graphs older than this many hours |
| `FLOWIFY_RATE_LIMIT_PER_MIN` | `6` | `server` mode only: per-IP cap on ingest/query calls |

---

## Deployment

The same codebase runs four ways — see [Dockerfile](Dockerfile) and
[`.github/workflows/`](.github/workflows) for the full setup:

- **Hosted (public site), genuinely free — [Render](https://render.com)** —
  a single Docker image serves the built frontend and the API from one
  origin (`FLOWIFY_MODE=server`). Paste a public git URL; it's cloned to a
  temp dir, ingested, and deleted. No server-side LLM key — graphs are
  built with the deterministic AST-based provider, and the **Copy for
  LLM** export gives a full architecture report to paste into any chatbot
  for free. Graphs are scoped to an anonymous per-browser session and
  expire after `FLOWIFY_GRAPH_TTL_H` hours.

  In the Render dashboard: **New +** → **Blueprint**, point it at this
  repo — [`render.yaml`](render.yaml) configures the rest. No credit card
  required on Render's free plan. (Hugging Face Spaces was the original
  plan here, but as of July 2026 HF requires a PRO subscription to create
  a Docker Space — the free tier is capped at 2 ZeroGPU Spaces, which
  don't fit an always-on server. If you do have HF PRO,
  `.github/workflows/deploy-space.yml` + [`deploy/space_readme.md`](deploy/space_readme.md)
  still work.) Render's free tier sleeps after 15 min idle (~30-60s cold
  start on the next request) and has no persistent disk — both already
  accounted for by the TTL janitor and ephemeral-storage design.
  ```bash
  docker build -t flowify .
  docker run -p 7860:7860 -e FLOWIFY_MODE=server flowify
  ```
- **Local, one command** — the same image, pointed at your own folder,
  with `/shutdown` enabled and no session scoping:
  ```bash
  docker run -p 7860:7860 -e FLOWIFY_MODE=local -v "$PWD:/repos:ro" ghcr.io/mimo1999/flowify_toolkit
  ```
- **Local, from source** — unchanged: `bash run.sh` (see Quick start above).
- **Frontend only, on Vercel** — connect this repo with Root Directory
  `frontend`, then set `VITE_API_BASE=https://<your-render-service>.onrender.com/api`
  so a statically-hosted frontend talks to a backend running elsewhere.

`.github/workflows/ci.yml` runs the no-server-needed tests, a frontend
build, and a Docker build on every push. `docker-publish.yml` pushes
`ghcr.io/<owner>/<repo>` on version tags — free for public repos, no extra
secret required. `deploy-render.yml` is only needed if you've turned off
Render's default GitHub auto-deploy.

---

## Architecture

Flowify runs in three phases:

```
repo_path
  → ingestion.py          (language detection, file walk)
  → llm_provider.py       (analyze_repository, summarize_function, analyze_semantics)
  → graph_builder.py      (AST → CIR nodes/edges, multi-language)
  → module_abstractor.py  (community detection → module nodes)
  → storage.py            (SQLite at $FLOWIFY_STORE/flowify.db)
  → llm_ingestion.py      (LLM validation/enrichment of AST-derived node JSON)
  → retrieval.py          (query → graph BFS traversal)
  → learning.py           (feedback loop, terminology map)
```

**Phase 1 — Repo context** (`llm_provider.py`'s `analyze_repository`): analyses README, manifests, directory tree; detects project type, tech stack, entry points, architecture pattern.

**Phase 2 — Semantic enrichment** (`graph_builder.py` + `llm_provider.py` + `llm_ingestion.py`): multi-language AST parsing (Python, JS/TS, Java, C/C++) → Canonical Intermediate Representation (CIR) → per-function LLM summaries (intent, complexity, criticality) → `llm_ingestion.py` validates/enriches the AST-derived node JSON. Module clustering via greedy modularity community detection (NetworkX).

**Phase 3 — Continuous learning** (`learning.py`): tracks query patterns and feedback; adjusts relevance scores; builds a terminology map.

### Semantic edge types

| Edge | Colour | Meaning |
|---|---|---|
| `CALLS` | Blue | Direct function call |
| `EXPOSES_API` | Green | HTTP / RPC endpoint |
| `USES_DB` | Purple | Database read/write |
| `EMITS_EVENT` | Yellow | Event / message publish |
| `CONSUMES_EVENT` | Red | Event / message subscribe |

### Graph storage

- Graphs: stored in a SQLite database at `$FLOWIFY_STORE/flowify.db` — `{graph_id}` is a 12-char hex from `uuid4().hex[:12]`
- MCP repo IDs: 12-char hex from SHA256 of `repo_path` — **different from graph IDs**
- Override store location with `FLOWIFY_STORE` env var

---

## API reference

Every route below is available both at its path as written (kept for
backward compatibility with the MCP server and existing tests) and under an
`/api` prefix (e.g. `/api/ingest_repo`) — the path production deployments
and the built frontend use. See the router-mounting comment at the top of
[`backend/app/main.py`](backend/app/main.py).

### UI-facing endpoints

| Method | Path | Body / Params | Returns |
|---|---|---|---|
| `POST` | `/ingest_repo` | `{repo_path}` **or** `{repo_url}` | `{graph_id, function_count, module_count}` |
| `GET` | `/entry_points` | `?graph_id=&max_count=4` | Root file nodes for the initial view |
| `GET` | `/expand` | `?graph_id=&node_id=&action=callees\|functions` | `{children, edges}` |
| `GET` | `/graph` | `?graph_id=&depth=1..3` | Full graph at the requested depth |
| `GET` | `/impact` | `?graph_id=&node_id=` | Change impact analysis for a function |
| `POST` | `/query` | `{graph_id, query}` | `{explanation, execution_steps, graph_nodes_consulted}` |
| `POST` | `/feedback` | `{graph_id, query_id, rating}` | Feedback (feeds continuous learning) |
| `POST` | `/update` | `{graph_id}` | Re-ingests changed files via git diff |
| `GET` | `/provider_info` | — | Active LLM provider name + model |

### Analysis endpoints

| Method | Path | Returns |
|---|---|---|
| `GET` | `/repo_context?graph_id=` | Phase 1 repository analysis |
| `GET` | `/semantic_analysis?graph_id=` | Per-function semantic metadata |
| `GET` | `/analytics?graph_id=` | Query usage statistics |
| `GET` | `/hot_nodes?graph_id=&limit=10` | Most-queried functions |
| `GET` | `/module_details?graph_id=&module_id=` | Module with control-flow groups |

### MCP endpoints (for LLM assistants)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/mcp/ingest` | Idempotent; returns stable `repo_id` from SHA256 of path |
| `POST` | `/mcp/query` | Structured function list + execution path |
| `POST` | `/bob/graph` | Full Bob-ready graph payload (CLI / agent use) |

---

## MCP server (VS Code integration)

```powershell
cd mcp_server
.\setup.ps1        # registers the MCP server into VS Code
# restart VS Code, then ask your AI assistant: "What tools do you have available?"
```

Bob CLI (from `backend/`):

```bash
python -m app.bob_graph_cli --repo-path /path/to/repo --depth 3
```

See [`mcp_server/README.md`](mcp_server/README.md) for full setup details.

---

## Running tests

```bash
# Run from project root (NOT from tests/ — relative imports will break)
pytest tests/ -v

# Individual suites
pytest tests/test_mcp_endpoints.py -v       # no server needed
pytest tests/test_integration.py -v         # requires FastAPI on :8000
pytest tests/test_self_graph.py -v          # ingests this repo itself
```

---

## Windows notes

- **B:\ drive error dialog**: `uvicorn --reload` uses watchfiles which enumerates all drive letters on Windows. Scope it to the app directory to prevent the error:
  ```bash
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app
  ```
  `run.sh` and `start_server.ps1` already include this flag.

- **Long first-ingestion time**: Ollama makes one LLM call per function during ingestion. A 600-function repo can take 10–20 minutes on first run. All responses are cached — subsequent runs are instant. Use `LLM_PROVIDER=heuristic` for a no-LLM fast ingest.
