# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Critical Non-Obvious Patterns

**Storage System**: Graph data stored as `{graph_id}.json`, metadata as `{graph_id}.{key}.json` in `_store/` (or `FLOWIFY_STORE` env var). Graph IDs are 12-char hex UUIDs, NOT full UUIDs.

**Bob Client Caching**: All Bob API calls cached in `_store/bob_cache/` by SHA256 hash of prompt. Cache is permanent - delete files to invalidate. Falls back to stubs if `BOB_API_KEY` unset.

**Module Abstraction**: `module_abstractor.py` creates hierarchical views at 3 depths - depth 1 (modules), depth 2 (files), depth 3 (functions). Entry points detected via heuristics in `find_entry_files()`, NOT from explicit declarations.

**MCP Server**: Must run from `mcp_server/` directory with `PYTHONPATH` set. Uses resilience patterns (circuit breaker, retry, deduplication) in `resilience.py` - NOT optional decorators.

**Test Execution**: Backend tests run from project root with `pytest tests/`, NOT from `tests/` directory. Integration tests require FastAPI server running on port 8000.

**Dual Endpoints**: `/ingest_repo` and `/query` are legacy. Use `/mcp/ingest` and `/mcp/query` for MCP clients - they have normalized responses and idempotent behavior.

**Graph ID Generation**: `storage.new_graph_id()` creates 12-char IDs. MCP endpoints also generate `repo_id` from SHA256 of repo_path (also 12 chars). These are different identifiers.

**Import Order**: Models must be imported before other app modules due to Pydantic model dependencies. `from .models import` always comes first in app files.

## Build/Test Commands

```bash
# Backend (from project root)
cd backend && python -m uvicorn app.main:app --port 8000

# Frontend
cd frontend && npm run dev

# Both together (idempotent, creates venv/node_modules)
./run.sh                    # Start servers
./run.sh --test             # Start + smoke test + exit

# Tests (from project root, NOT tests/ dir)
pytest tests/test_mcp_endpoints.py -v
pytest tests/test_integration.py -v  # Requires FastAPI running

# MCP Server setup
cd mcp_server && .\setup.ps1  # Windows only
```

## Environment Variables

- `FLOWIFY_STORE` - Storage directory (default: `_store/`)
- `BOB_API_KEY` - Bob API key (optional, falls back to stubs)
- `BOB_API_URL` - Bob endpoint (optional)
- `FASTAPI_BASE_URL` - For MCP server (default: `http://localhost:8000`)