# Flowify AI — Complete Project Documentation

> **Code-graph explorer with GraphRAG querying.** Ingests any repository into a navigable call graph and answers natural-language questions about your codebase using any LLM provider.

---

## Table of Contents

1. [Use Case & Problem Statement](#1-use-case--problem-statement)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Ingestion Pipeline — Step by Step](#3-ingestion-pipeline--step-by-step)
4. [Module Reference](#4-module-reference)
   - 4.1 [ingestion.py — File Discovery](#41-ingestionpy--file-discovery)
   - 4.2 [graph_builder.py — AST Parsing & Call Graph](#42-graph_builderpy--ast-parsing--call-graph)
   - 4.3 [pipeline.py — Orchestration](#43-pipelinepy--orchestration)
   - 4.4 [module_abstractor.py — Hierarchy & Navigation](#44-module_abstractorpy--hierarchy--navigation)
   - 4.5 [retrieval.py — GraphRAG Query Engine](#45-retrievalpy--graphrag-query-engine)
   - 4.6 [llm_provider.py — Agent-Agnostic LLM Layer](#46-llm_providerpy--agent-agnostic-llm-layer)
   - 4.7 [llm_ingestion.py — Structured LLM Normalisation](#47-llm_ingestionpy--structured-llm-normalisation)
   - 4.8 [storage.py — Persistence](#48-storagepy--persistence)
   - 4.9 [learning.py — Continuous Learning](#49-learningpy--continuous-learning)
   - 4.10 [models.py — Data Contracts (CIR)](#410-modelspy--data-contracts-cir)
   - 4.11 [main.py — REST API (FastAPI)](#411-mainpy--rest-api-fastapi)
   - 4.12 [mcp_server/ — MCP Integration](#412-mcp_server--mcp-integration)
5. [Frontend](#5-frontend)
   - 5.1 [Component Tree](#51-component-tree)
   - 5.2 [Store (Zustand)](#52-store-zustand)
   - 5.3 [Graph Rendering](#53-graph-rendering)
6. [Graph Data Model](#6-graph-data-model)
   - 6.1 [Node Types](#61-node-types)
   - 6.2 [Edge Types](#62-edge-types)
   - 6.3 [ID Format](#63-id-format)
7. [LLM Provider System](#7-llm-provider-system)
8. [API Endpoints](#8-api-endpoints)
9. [Storage Layout](#9-storage-layout)
10. [Configuration Reference](#10-configuration-reference)
11. [Test Suite](#11-test-suite)
12. [Running the Project](#12-running-the-project)

---

## 1. Use Case & Problem Statement

### The Problem

Large codebases are hard to understand:

- A new engineer joining a team of 10 spends **2–4 weeks** just tracing how request paths connect before they can contribute.
- Senior engineers context-switching between repos must constantly re-derive "what calls what" before diagnosing a bug.
- Code review tools show you diffs, not **causation**: which downstream functions does this change affect?
- Grep and IDE "Find References" are file-level; they don't surface the *call chain*, *ownership*, or *architectural role* of a function.

### What Flowify Solves

Flowify ingests a repository once and produces an **interactive, navigable call graph** that answers:

| Question | How Flowify answers it |
|---|---|
| "Where does this repo start?" | Entry-point heuristics surface `main.py`, `app.py`, CLI scripts |
| "What does this function call?" | Expand any node to see its callees one hop at a time |
| "How does authentication work?" | Natural-language query → BFS subgraph → LLM explanation |
| "What changed and what does it affect?" | Incremental git-diff re-ingestion, re-resolved call graph |
| "Who does all the persistence?" | Query "save to database" → relevant nodes highlighted |

### Target Users

- **New engineers** onboarding to an unfamiliar codebase.
- **Tech leads** performing architectural reviews or impact analysis.
- **AI-assisted development** workflows — the "Copy context for LLM" button exports the relevant subgraph as a structured markdown block to paste into any chat interface.

---

## 2. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           USER BROWSER                                   │
│                                                                          │
│  ┌──────────┐  ┌────────────────────────┐  ┌───────────────────────┐    │
│  │ Sidebar  │  │  GraphView (React Flow)│  │  QueryResults (slide) │    │
│  │          │  │  dagre LR layout       │  │  + feedback           │    │
│  │ • ingest │  │  custom node types     │  │                       │    │
│  │ • sync   │  │  file / fn / class     │  │  QueryPanel (bottom)  │    │
│  │ • back   │  │                        │  │  natural-lang input   │    │
│  └──────────┘  └────────────────────────┘  └───────────────────────┘    │
│                         Zustand store (store.js)                         │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │  REST  (Vite proxy → :8000)
┌────────────────────────────────▼─────────────────────────────────────────┐
│                         FastAPI  backend  (:8000)                        │
│                           main.py — router                               │
│                                                                          │
│  POST /ingest_repo          GET /entry_points       POST /query          │
│  GET  /expand               GET /graph              POST /feedback       │
│  POST /update               GET /provider_info      GET  /analytics      │
│                                                                          │
│  ┌────────────┐  ┌────────────────┐  ┌────────────┐  ┌───────────────┐  │
│  │ pipeline   │  │ module_        │  │ retrieval  │  │  learning     │  │
│  │ (ingest /  │  │ abstractor     │  │ (GraphRAG) │  │  (feedback /  │  │
│  │  update)   │  │ (expand /      │  │            │  │   analytics)  │  │
│  │            │  │  find_entry)   │  │            │  │               │  │
│  └─────┬──────┘  └───────┬────────┘  └─────┬──────┘  └───────┬───────┘  │
│        │                 │                  │                  │          │
│  ┌─────▼──────┐  ┌───────▼────────┐  ┌─────▼──────┐          │          │
│  │graph_      │  │ llm_ingestion  │  │ llm_       │          │          │
│  │builder     │  │ (LLM prompt /  │  │ provider   │◄─────────┘          │
│  │(AST parse  │  │  normalise)    │  │ (ask /     │                      │
│  │ call graph)│  │                │  │  explain / │                      │
│  └─────┬──────┘  └───────┬────────┘  │  interpret)│                      │
│        │                 │           └─────┬──────┘                      │
│  ┌─────▼──────┐          │                 │                             │
│  │ ingestion  │          │           ┌─────▼──────┐                      │
│  │(file walk) │          │           │  _store/   │                      │
│  └────────────┘          └──────────►│  (JSON)    │                      │
│                                      └────────────┘                      │
└──────────────────────────────────────────────────────────────────────────┘
                                 ▲
                         MCP protocol
┌────────────────────────────────┴──────────────┐
│  mcp_server/flowify_mcp.py                    │
│  (Claude / Copilot / any MCP-capable agent)   │
└───────────────────────────────────────────────┘
```

---

## 3. Ingestion Pipeline — Step by Step

When a user clicks **Ingest** (or calls `POST /ingest_repo`), the following sequence runs:

```
repo_path
   │
   ▼
[1] ingestion.iter_source_files()
    Walk repo, skip .venv / node_modules / __pycache__
    Detect language by extension (.py, .js, .ts, .java, .c/.cpp)
   │
   ▼
[2] graph_builder.build_function_graph()
    For each file → parse_file() → language-specific adapter:
      Python   → _Visitor (ast.NodeVisitor)
      JS / TS  → structural regex adapter
      Java     → structural adapter
      C / C++  → structural adapter
    Emit FunctionNode (id, name, kind, file_path, code_snippet, lineno)
    Emit FunctionEdge with target_id = "<symbol>::{callee_name}"
    Resolve <symbol>:: references: match callee name → known node IDs
    Deduplicate edges by (source, target, type)
   │
   ▼
[3] pipeline._summarize_functions()
    For each callable node with code_snippet:
      llm_provider.summarize_function(name, code) → 1-sentence summary
      (cached by prompt hash; heuristic stub if no LLM key)
   │
   ▼
[4] pipeline._analyze_semantics()
    For each callable node:
      llm_provider.analyze_function_semantics() →
        intent, complexity, criticality, patterns, side_effects
      Store as FunctionNode.semantics (SemanticMetadata)
      Collect SemanticEdge[]
   │
   ▼
[5] module_abstractor.build_modules()
    Cluster functions via greedy modularity community detection (NetworkX)
    Name each cluster: llm_provider.summarize_module()
    Emit ModuleNode[], ModuleEdge[] (aggregated cross-cluster FLOW)
    Tag entry points within each module
   │
   ▼
[6] storage.save(GraphPayload)
    Write <graph_id>.json to _store/
   │
   ▼
[7] llm_ingestion.ingest_ast_results()
    Build structured prompt: repo context + module ground truth + AST nodes
    llm_provider.ask_json() → LLMIngestionResult (node roles, responsibilities)
    storage.store_meta(graph_id, "llm_ingestion", ...)
   │
   ▼
[8] git_updater.changed_files_since()
    Record current git HEAD for incremental updates
   │
   ▼
GraphPayload returned → API serialises entry_points view to frontend
```

### Incremental Update (`POST /update`)

```
stored git HEAD
   │
   ▼
git diff <last>..HEAD → changed file paths
   │
   ├── keep_nodes = nodes NOT in changed files
   ├── re-parse changed files → new_nodes, new_edges
   ├── re-resolve symbol edges across merged node set
   ├── _summarize_functions(new_nodes only)
   ├── _analyze_semantics(new_nodes only, merge semantic edges)
   ├── build_modules(merged graph)
   └── storage.save(merged payload)
```

---

## 4. Module Reference

### 4.1 `ingestion.py` — File Discovery

**Role:** Repository walker. Knows which files to parse and which to skip.

**Key functions:**

| Function | What it does |
|---|---|
| `detect_language(path)` | Maps file extension → language string |
| `iter_source_files(repo_path)` | Generator — yields Path objects, skipping ignored dirs |
| `list_source_files(repo_path)` | Eager list version |

**Ignored directories:** `.git`, `.venv`, `venv`, `__pycache__`, `node_modules`, `.tox`, `dist`, `build`, `.mypy_cache`

**Supported languages:**

```
Python    .py
JS/JSX    .js .jsx .mjs .cjs
TS/TSX    .ts .tsx
Java      .java
C         .c .h
C++       .cc .cpp .cxx .hpp .hh .hxx
```

---

### 4.2 `graph_builder.py` — AST Parsing & Call Graph

**Role:** The core static analyser. Parses each source file and produces language-agnostic `FunctionNode` and `FunctionEdge` objects.

#### Python adapter — `_Visitor`

An `ast.NodeVisitor` subclass that walks the Python AST:

```
_Visitor
├── visit_FunctionDef / visit_AsyncFunctionDef
│   ├── Build qualified name: qual_stack + name (e.g. "MyClass.my_method")
│   ├── Emit FunctionNode(id="{file}::{qualname}", kind="function")
│   ├── Record code_snippet (source lines from lineno..end_lineno)
│   ├── Record decorators (e.g. "@app.post", "@staticmethod")
│   └── _append_call_edges() — regex scan body for callee() names
├── visit_ClassDef
│   ├── Emit FunctionNode(kind="type")
│   └── Push to qual_stack → nested methods get "ClassName.method" qualname
└── visit_Import / visit_ImportFrom
    Emit FunctionEdge(relationship="DEPENDS_ON") to module nodes
```

#### Call edge resolution

Calls are initially symbolic: `FunctionEdge(target_id="<symbol>::foo")`.

`build_function_graph()` resolves them globally:
1. Build `name_index: {name → [node_id, ...]}` across all parsed nodes.
2. For each `<symbol>::foo` edge, fan out to all nodes named `foo` (except self-loops).
3. Deduplicate `(source, target, type)` triples — prevents duplicate arrows in UI.

#### Structural adapters (JS/TS/Java/C++)

These use regex-based extraction rather than a full AST — they capture function/class/method definitions and produce the same `FunctionNode` / `FunctionEdge` format. They are designed to be swapped with Babel, Tree-sitter, or JavaParser parsers without changing the rest of the pipeline.

**Key functions:**

| Function | Purpose |
|---|---|
| `parse_file(path, root)` | Dispatcher — routes to language-specific parser |
| `parse_python_file(path, root)` | Python AST parse → `_Visitor` |
| `parse_js_ts_file(path, root)` | Structural JS/TS adapter |
| `parse_java_file(path, root)` | Structural Java adapter |
| `parse_c_family_file(path, root)` | Structural C/C++ adapter |
| `build_function_graph(repo_path)` | Full pipeline: parse all files, resolve edges, deduplicate |
| `_call_names(body, name)` | Regex: `(?:\b|\.)([A-Za-z_]\w*)\s*\(` — extracts callee names |
| `_append_call_edges(...)` | Emits `<symbol>::` placeholder edges for each callee |

---

### 4.3 `pipeline.py` — Orchestration

**Role:** Top-level coordinator. Calls every other backend module in the right order. The only module `main.py` invokes for ingestion and update.

```python
# Full ingest flow:
ingest(repo_path)
  → analyze_repository()          # Phase 1: repo context
  → build_function_graph()        # AST + call graph
  → _summarize_functions()        # LLM summaries
  → _analyze_semantics()          # Phase 2: semantic metadata
  → build_modules()               # cluster + name modules
  → storage.save()
  → llm_ingestion.ingest_ast_results()  # structured LLM pass
  → git_updater.changed_files_since()   # record HEAD
```

**`_summarize_functions(nodes)`**
- Iterates callable nodes that have `code_snippet` but no `summary`.
- Calls `llm_provider.summarize_function(name, code)` — returns a single sentence.
- Result stored in `FunctionNode.summary`.

**`_analyze_semantics(nodes, g, repo_context)`**
- Iterates callable nodes with code.
- Calls `llm_provider.analyze_function_semantics()` with call-graph neighbours for context.
- Populates `FunctionNode.semantics` (SemanticMetadata: intent, complexity, criticality, patterns, side_effects).
- Collects SemanticEdge[] (semantic relationships like ORCHESTRATES, PERSISTS, RETRIEVES).

---

### 4.4 `module_abstractor.py` — Hierarchy & Navigation

**Role:** Translates the flat function graph into a navigable, layered hierarchy. Handles all "expand node" logic that the frontend calls.

#### `build_modules(g, function_nodes, function_edges, declared_entry_points)`

```
1. Extract function subgraph (drop file nodes)
2. Run greedy_modularity_communities (NetworkX) on undirected projection
3. For each community:
   a. Pick a name hint from function names in the cluster
   b. llm_provider.summarize_module(name_hint, summaries) → {name, description}
   c. Emit ModuleNode with linked_function_ids
4. Aggregate cross-cluster CALLS → ModuleEdge(kind="FLOW")
5. Run control_flow_analyzer on each module
```

#### `find_entry_files(fn_by_id, fn_edges, declared_entry_points, max_count=4)`

Selects up to 4 files to show on the initial canvas. Two-phase approach:

**Phase A — Declared entries (from LLM/heuristic repo analysis):**
- Filters out test files and `__init__.py`-style penalty files.
- Sorts by name-boost score: `main.py` (+45), `app.py` (+40), `run.py` (+35), etc.
- Only trusted if ≥1 entry has a positive boost score (guards against stub LLM returning random names).

**Phase B — Structural heuristic (fallback):**
- Score each file: `+boost` for known entry names, `-40` for penalty names, `+2` for zero in-degree, `+0.5×out_degree` (capped at 6).
- Files with score < 0 are excluded. Files with score ≥ 60 are filtered to only those.

#### `expand_file_node(fn_by_id, fn_edges, file_path, action)`

- `action="functions"` → emit all function/class symbols inside the file as children with CONTAINS edges + intra-file CALLS edges.
- `action="callees"` → emit files this file calls (file-level aggregation of CALLS edges) as FLOW edges.
- Caps children at `_MAX_CHILDREN = 12`.

#### `expand_node(module_nodes, fn_by_id, fn_edges, node_id)`

- Module node → submodule nodes (file groupings within the cluster) + CONTAINS.
- Function node → callee function nodes (one CALLS hop out) + CALLS edges.

#### `collapse_for_depth(module_nodes, module_edges, mod_to_funcs, fn_by_id, depth, fn_edges)`

Renders the graph at a requested depth level (used by `GET /graph`):
- `depth=1` → module nodes only + FLOW edges.
- `depth=2` → modules + per-file submodule nodes.
- `depth=3` → modules + raw function nodes.

---

### 4.5 `retrieval.py` — GraphRAG Query Engine

**Role:** Given a natural-language query, finds the relevant subgraph and produces an LLM-generated explanation.

#### `retrieve_subgraph(payload, query, max_hops=2)`

```
1. Build NetworkX DiGraph from payload
2. _entry_nodes(g, query, graph_id):
   a. Collect all function names → candidate list
   b. llm_provider.interpret_query(query, names) → ranked name list
   c. Add learned terminology suggestions (from learning module)
   d. Fallback: keyword match in summary text
3. BFS from entry nodes, following INVOKES edges, up to max_hops
4. Build subgraph payload: ordered node list + relevant edges
5. learning.record_query() → returns query_id for feedback
```

#### `explain(payload, query, ordered_ids)`

```
1. Collect summaries for top 15 nodes in ordered result
2. llm_provider.explain_flow(query, summaries) → natural-language paragraph
```

The explanation appears in the slide-in QueryResults panel in the UI.

---

### 4.6 `llm_provider.py` — Agent-Agnostic LLM Layer

**Role:** Single abstraction point for all LLM calls. The rest of the codebase calls module-level functions (`ask`, `summarize_function`, `explain_flow`, etc.) and is completely unaware of which provider is active.

#### Strategy pattern

```
LLMProvider (ABC)
├── _call(prompt) → str          [abstract]
├── summarize_function(name, code) → str
├── summarize_module(hint, summaries) → dict
├── explain_flow(query, summaries) → str
├── interpret_query(query, candidates) → List[str]
├── analyze_repository(repo_path) → dict
└── analyze_function_semantics(name, code, ctx, neighbors) → dict

Concrete providers:
├── HeuristicProvider   — deterministic stubs, no network
├── BobProvider         — IBM watsonx (BOB_API_KEY + BOB_API_URL)
├── AnthropicProvider   — Claude (ANTHROPIC_API_KEY)
├── OpenAIProvider      — OpenAI / Codex (OPENAI_API_KEY)
├── CopilotProvider     — GitHub Copilot (GITHUB_TOKEN)
└── OpenClawProvider    — OpenClaw (OPENCLAW_API_KEY + OPENCLAW_API_URL)
```

#### Shared disk cache

All providers use a SHA-256 keyed disk cache at `_store/llm_cache/`:

```python
prompt → SHA-256[:32] → _store/llm_cache/<hash>.json
```

Cache hit skips the network call entirely. This means repeated ingestion of the same repo is nearly instant after the first run.

#### `HeuristicProvider` (always available)

Produces deterministic stub responses:
- `summarize_function` → `"(stub) {name}: processes input and returns result"`
- `interpret_query` → keyword match: finds candidate names containing query words
- `analyze_repository` → reads README, `requirements.txt`, `package.json`, `pyproject.toml`; scores entry files by name; detects project type from dependencies

#### Provider resolution

```python
get_provider():
  1. Check LLM_PROVIDER env var → exact match
  2. Auto-detect: BOB_API_KEY → Bob; ANTHROPIC_API_KEY → Anthropic; OPENAI_API_KEY → OpenAI; GITHUB_TOKEN → Copilot; OPENCLAW_API_KEY → OpenClaw
  3. Fallback → HeuristicProvider
```

#### Heuristic entry-point detection

`_discover_entry_points(repo_root)` scans README / shell scripts for `python -m <module>` invocations, scores `.py` files by name and content patterns (`if __name__ == "__main__"`, `argparse`, `def main()`), and returns the top 10 candidates. Used by `HeuristicProvider.analyze_repository`.

---

### 4.7 `llm_ingestion.py` — Structured LLM Normalisation

**Role:** Post-ingestion LLM pass that produces a structured `LLMIngestionResult` — roles, responsibilities, and dependencies for each node. Used to enrich the graph beyond what pure AST analysis can infer.

#### `build_prompt(repo_context, fn_nodes, fn_edges, module_nodes, mod_to_funcs, semantic_edges)`

Produces a detailed prompt containing:
- Repository overview (type, domain, architecture, tech stack)
- Module ground truth (cluster names + member list)
- Per-node data (id, name, file, type, lineno, summary, call edges, semantic edges, code snippet capped at 600 chars)

#### `ingest_ast_results(repo_context, fn_nodes, fn_edges, module_nodes, mod_to_funcs, semantic_edges)`

```
1. build_prompt(...)
2. bob_client.ask_json(prompt, fallback) → dict
3. Validate against LLMIngestionResult schema
4. Return (LLMIngestionResult, prompt_text)
```

Result stored as `<graph_id>.llm_ingestion.json` — accessible via `GET /llm_ingestion`.

---

### 4.8 `storage.py` — Persistence

**Role:** Thin JSON file store. One file per graph, sidecar files for metadata.

```python
save(payload)            → _store/<graph_id>.json
load(graph_id)           → GraphPayload | None
list_graphs()            → [graph_id, ...]           # filters out "." in stem
store_meta(id, key, val) → _store/<id>.<key>.json
load_meta(id, key)       → dict | None
```

Storage directory is controlled by the `FLOWIFY_STORE` environment variable (default: `_store/`).

---

### 4.9 `learning.py` — Continuous Learning

**Role:** Tracks queries and feedback over time to improve retrieval quality.

#### What it stores (`<graph_id>.learning.json`)

```
LearningInsights
├── query_patterns[]      QueryPattern per request
│   ├── query_text
│   ├── normalized_query
│   ├── retrieved_nodes[]
│   ├── feedback (helpful / neutral / unhelpful)
│   └── response_time_ms
├── usage_stats{}         node_id → UsageStatistics
│   ├── access_count
│   ├── avg_relevance_score
│   └── first/last_accessed
├── terminology_map[]     TerminologyMapping
│   ├── term (e.g. "persistence")
│   └── function_names (["save", "store", "persist"])
├── total_queries
├── helpful_queries
└── helpful_rate
```

#### Key functions

| Function | What it does |
|---|---|
| `record_query(graph_id, query, nodes, ms)` | Appends QueryPattern, updates usage stats, extracts terminology, returns `query_id` |
| `record_feedback(graph_id, query_id, rating)` | Finds query pattern, updates relevance scores, adjusts criticality |
| `get_terminology_suggestions(graph_id, query)` | Returns function names that learned terminology maps match to query words |
| `update_node_importance(graph_id, payload)` | Recalculates criticality: high access + high relevance → boost |

---

### 4.10 `models.py` — Data Contracts (CIR)

**Role:** Pydantic v2 models that define the Canonical Intermediate Representation (CIR). Language-agnostic by design.

#### Core CIR types

```
CIRNodeKind:  file | namespace | type | function | callable | data | external
CIRRelationship: CONTAINS | INVOKES | DEPENDS_ON | EXTENDS | IMPLEMENTS | OVERRIDES | FLOWS_TO
```

#### Node hierarchy

```
FunctionNode (CIRNode)
├── id: str                     "{file_path}::{qualname}"  or  "file::{path}"
├── name: str                   short name
├── file_path: str              POSIX relative path from repo root
├── type: str                   legacy: "function" | "method" | "class" | "file"
├── kind: CIRNodeKind           CIR: "function" | "type" | "file" | ...
├── qualified_name: str
├── source_language: str
├── code_snippet: str           up to ~40 lines
├── summary: str                LLM one-liner
├── lineno: int
├── semantics: SemanticMetadata | None
└── adapter_metadata: dict      language-specific evidence
```

#### Edge types

```
FunctionEdge (CIREdge)
├── source_id, target_id
├── type: EdgeType              legacy: "CALLS" | "DEFINES" | "IMPORTS" | ...
└── relationship: CIRRelationship   CIR: "INVOKES" | "CONTAINS" | "DEPENDS_ON"

SemanticEdge
├── source_id, target_id
├── type: SemanticEdgeType      TRANSFORMS | VALIDATES | ORCHESTRATES | PERSISTS | RETRIEVES | ...
├── confidence: float
└── inferred_by: "bob" | "heuristic"
```

#### `GraphPayload`

The main artifact produced by `pipeline.ingest()`:

```
GraphPayload
├── graph_id: str
├── repo_path: str
├── function_nodes: List[FunctionNode]
├── function_edges: List[FunctionEdge]
├── module_nodes: List[ModuleNode]
├── module_edges: List[ModuleEdge]
├── module_to_functions: Dict[str, List[str]]
└── semantic_edges: List[SemanticEdge]
```

---

### 4.11 `main.py` — REST API (FastAPI)

**Role:** HTTP layer. All endpoints are thin — they load the payload from storage, delegate to the right module, and serialise the result.

#### Internal helpers

- `_generate_repo_id(repo_path, custom_id)` — stable hash for MCP idempotency
- `_serialize_payload(payload)` — shared CIR→dict projection used by 3 endpoints

#### Endpoint summary (see §8 for full reference)

```
GET  /                      health check
GET  /provider_info         active LLM provider name + is_stub flag
POST /ingest_repo           full ingest (Phase 1–3)
POST /mcp/ingest            idempotent ingest (skips if repo already ingested)
GET  /entry_points          initial graph view (up to 4 file nodes)
GET  /expand                one expand step: file→functions or fn→callees
GET  /graph                 depth-based view (depth 1–3)
POST /query                 GraphRAG: retrieve subgraph + LLM explanation
POST /mcp/query             MCP-formatted query response
POST /update                git-diff incremental re-ingest
GET  /repo_context          Phase 1 repository analysis result
GET  /semantic_analysis     Phase 2 semantic metadata + edges
GET  /llm_ingestion         Phase 2 LLM normalised node roles
GET  /llm_ingestion_prompt  raw prompt sent during ingestion
POST /feedback              record helpful/neutral/unhelpful rating
GET  /module_details        functions inside a specific module
GET  /analytics             learning stats (query count, helpful rate)
GET  /hot_nodes             most-accessed functions
GET  /common_paths          frequently traversed call chains
POST /update_importance     recalculate criticality from usage data
POST /bob/graph             CIR graph export for Bob/MCP consumers
```

---

### 4.12 `mcp_server/` — MCP Integration

**Role:** Exposes Flowify as an MCP (Model Context Protocol) server so Claude, GitHub Copilot, and any other MCP-capable agent can call `ingest_repo` and `query_repo` as tools.

#### `flowify_mcp.py`

Thin HTTP client wrapping the FastAPI backend:

```
MCP Tool: ingest_repo
  args: repo_path, repo_id (optional)
  → POST http://localhost:8000/mcp/ingest
  → returns: graph_id, file_count, function_count, entry_points[]

MCP Tool: query_repo
  args: graph_id, query
  → POST http://localhost:8000/mcp/query
  → returns: explanation, relevant_functions[], path[]
```

#### `resilience.py`

Production reliability features:
- **Circuit breaker** — opens after 5 consecutive failures, resets after 60 s
- **Request deduplicator** — concurrent identical calls share one in-flight request
- **Retry with exponential backoff** — up to 3 retries with jitter
- **Health checker** — polls `/` before first request in a session

---

## 5. Frontend

Stack: **React 18 + Vite + React Flow + Zustand + Tailwind CSS + @dagrejs/dagre**

### 5.1 Component Tree

```
App.jsx
├── <Sidebar />                     w-72, left panel
│   ├── Brand header + graph logo
│   ├── ProviderBadge               fetches /api/provider_info
│   │   └── green = real LLM, amber = stub
│   ├── Repository path input
│   ├── Ingest / Sync buttons
│   ├── Back / Reset navigation
│   ├── Graph stats (files, symbols, expanded count)
│   └── Legend (file / function / class node colours)
│
├── <main> flex-col flex-1
│   ├── <ReactFlowProvider>
│   │   └── <GraphView />           fills canvas
│   │       ├── dagreLayout()       LR, nodesep=50, ranksep=130
│   │       ├── FileNodeComponent   blue gradient card
│   │       ├── FunctionNodeComponent  indigo (fn) / emerald (class)
│   │       ├── EmptyState          shown before any graph loaded
│   │       └── React Flow controls + minimap
│   │
│   ├── <QueryResults />            absolute right overlay, w-340px
│   │   ├── Slide-in panel (slide-in-right animation)
│   │   ├── Explanation text
│   │   ├── Relevant functions list (up to 15)
│   │   ├── "Copy context for LLM" → clipboard markdown
│   │   └── Feedback buttons (👍 😐 👎)
│   │
│   └── <QueryPanel />              bottom bar
│       ├── Search input (Enter to submit)
│       └── Ask button
```

### 5.2 Store (Zustand)

`store.js` holds all shared state and async actions:

```javascript
State:
  graphId, repoPath
  nodes: {}           // id → node (built incrementally via expand)
  edges: {}           // id → edge
  expanded: {}        // node_id → [child_id, ...]
  rootIds: []         // entry-point node IDs
  selectedId, highlightPath
  explanation, queryId, queryNodes[]
  loading, error
  viewHistory[]       // stack of {nodes, edges, expanded} snapshots

Actions:
  ingest()            POST /ingest_repo → loadInitial()
  loadInitial()       GET  /entry_points → seed nodes/edges
  toggleExpand(id)    GET  /expand?action=functions|callees
                        collapse if already expanded
                        skip-if-exists when adding callee nodes (Bug #2 fix)
                        push to viewHistory on file expand
  goBack()            pop viewHistory → restore previous state
  resetView()         clear + loadInitial()
  runQuery(q)         POST /query → explanation + queryNodes
  clearQuery()        reset explanation/queryNodes
  sendFeedback(r)     POST /feedback
```

### 5.3 Graph Rendering

**Dagre layout fix (Bug #1 fix):**

Two separate edge sets:
- `dagreEdges` — all edges, no pruning → fed to dagre so every node gets ranked
- `rfEdges` — pruned for visual cleanliness (CONTAINS fan-out capped) → displayed by React Flow

This prevents middle-sibling nodes from appearing disconnected on the left side of the canvas.

**Custom node types:**

```javascript
nodeTypes = {
  fileNode:     FileNodeComponent,     // blue gradient, expand chevron, fn count badge
  functionNode: FunctionNodeComponent, // indigo/emerald, </> or class icon
}
```

---

## 6. Graph Data Model

### 6.1 Node Types

| `kind` | Colour | Represents |
|---|---|---|
| `file` | Blue gradient | Source file — entry point to expand |
| `function` / `callable` | Indigo gradient | Top-level function or async def |
| `type` | Emerald gradient | Class definition |
| `external` | Grey | External module import target |

### 6.2 Edge Types

| `relationship` | Direction | Meaning |
|---|---|---|
| `INVOKES` (`CALLS`) | A → B | A calls B |
| `CONTAINS` | file → fn | File owns this function |
| `FLOW` | file A → file B | File-level aggregated calls |
| `DEPENDS_ON` | A → module | Import dependency |
| `EXTENDS` | class A → B | Inheritance |
| `OVERRIDES` | method A → B | Method override |

### 6.3 ID Format

```
File node (entry / expand result):   "file::{relative/path.py}"
Function node:                       "{relative/path.py}::{qualname}"
Method node:                         "{relative/path.py}::{ClassName.method_name}"
Class node:                          "{relative/path.py}::{ClassName}"
Internal file node (AST):            "{relative/path.py}::<file>"
External module:                     "external::module::{module_name}"
```

All paths use POSIX separators (`/`) regardless of OS.

---

## 7. LLM Provider System

```
Environment variable    Provider class       API call
──────────────────────────────────────────────────────────────────
LLM_PROVIDER=bob        BobProvider          POST BOB_API_URL
LLM_PROVIDER=claude     AnthropicProvider    Anthropic messages API
LLM_PROVIDER=openai     OpenAIProvider       POST OPENAI_BASE_URL/chat/completions
LLM_PROVIDER=copilot    CopilotProvider      GitHub Copilot API (OpenAI-compat.)
LLM_PROVIDER=openclaw   OpenClawProvider     POST OPENCLAW_API_URL
LLM_PROVIDER=heuristic  HeuristicProvider    (no network)
(not set)               auto-detect by key   first key found wins
```

**Shared cache** — all providers use `_store/llm_cache/<sha256>.json`. A response cached by one provider is not reused by another (prompt is the cache key, not the function call).

**Method matrix:**

| Method | HeuristicProvider | LLM provider |
|---|---|---|
| `summarize_function` | `"(stub) {name}: …"` | Prompt → 1-sentence |
| `summarize_module` | Cluster name from member names | Prompt → name + description |
| `explain_flow` | Template string | Prompt → prose paragraph |
| `interpret_query` | Substring match on candidates | Prompt → ranked name list |
| `analyze_repository` | File scan heuristics | Prompt → RepositoryContext dict |
| `analyze_function_semantics` | Name-pattern rules | Prompt → SemanticMetadata dict |

---

## 8. API Endpoints

### Ingestion

```
POST /ingest_repo
  Body: { repo_path: str, repo_id?: str }
  Returns: { graph_id, repo_path, cir_version, function_nodes[], function_edges[], ... }

POST /mcp/ingest   (idempotent)
  Body: { repo_path: str, repo_id?: str }
  Returns: { success, graph_id, message, file_count, function_count, entry_points[] }

POST /update
  Body: { graph_id: str }
  Returns: updated GraphPayload (same shape as ingest)
```

### Graph Navigation

```
GET /entry_points?graph_id=&max_count=4
  Returns: { nodes[], edges[] }   ← up to max_count file nodes

GET /expand?graph_id=&node_id=&action=functions|callees
  Returns: { parent_id, children[], edges[] }

GET /graph?graph_id=&depth=1
  Returns: { graph_id, repo_path, nodes[], edges[] }   ← depth-collapsed view
```

### Query

```
POST /query
  Body: { graph_id, query, depth?: 2 }
  Returns: { explanation, path[], query_id, subgraph: { nodes[], edges[], entries[] } }

POST /mcp/query
  Body: { graph_id, query }
  Returns: { explanation, relevant_functions[], path[], query_id }
```

### Learning & Feedback

```
POST /feedback
  Body: { graph_id, query_id, rating: "helpful"|"neutral"|"unhelpful"|1..5 }
  Returns: { status: "recorded" }

GET /analytics?graph_id=
  Returns: LearningInsights

GET /hot_nodes?graph_id=&limit=10
  Returns: [{ node_id, name, file_path, access_count, avg_relevance }]

GET /common_paths?graph_id=&min_frequency=3
  Returns: [{ path[], frequency }]

POST /update_importance
  Body: { graph_id }
  Returns: { updated_count }
```

### Metadata

```
GET /provider_info
  Returns: { provider, is_stub, display_name }

GET /repo_context?graph_id=
  Returns: RepositoryContext

GET /semantic_analysis?graph_id=
  Returns: { function_nodes[], semantic_edges[] }   ← nodes with .semantics populated

GET /llm_ingestion?graph_id=
  Returns: LLMIngestionResult

GET /module_details?graph_id=&module_id=
  Returns: { module, functions[], edges[] }
```

---

## 9. Storage Layout

```
_store/                                   ← FLOWIFY_STORE env var
├── <12-char hex graph_id>.json           GraphPayload (function_nodes, edges, modules)
├── <graph_id>.repo_context.json          Phase 1: RepositoryContext
├── <graph_id>.llm_ingestion.json         Phase 2: LLMIngestionResult (node roles)
├── <graph_id>.llm_ingestion_prompt.json  raw prompt + version
├── <graph_id>.git.json                   { head: "<SHA>" } for incremental updates
├── <graph_id>.learning.json              Phase 3: LearningInsights
└── llm_cache/
    └── <sha256-32>.json                  { prompt: "...", response: "..." }
```

A graph with ID `923fa37b221f` produces files:
```
923fa37b221f.json
923fa37b221f.repo_context.json
923fa37b221f.git.json
923fa37b221f.llm_ingestion.json
923fa37b221f.learning.json
```

`storage.list_graphs()` only returns stems without `.` in them, so meta-files are never accidentally loaded as graph payloads.

---

## 10. Configuration Reference

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | *(auto)* | `bob` / `claude` / `openai` / `copilot` / `openclaw` / `heuristic` |
| `BOB_API_KEY` | — | IBM watsonx API key |
| `BOB_API_URL` | — | IBM watsonx endpoint URL |
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API key |
| `ANTHROPIC_MODEL` | `claude-3-5-haiku-latest` | Model override |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model override |
| `GITHUB_TOKEN` | — | GitHub Copilot token |
| `COPILOT_MODEL` | `gpt-4o` | Copilot model |
| `OPENCLAW_API_KEY` | — | OpenClaw key |
| `OPENCLAW_API_URL` | — | OpenClaw endpoint |
| `FLOWIFY_STORE` | `_store` | Storage directory path |

### Frontend proxy

Vite proxies `/api/*` → `http://localhost:8000/*` (configured in `vite.config.js`).

---

## 11. Test Suite

Three test files, all in `tests/`:

### `test_graph_correctness.py` — Unit tests for graph algorithms

Tests the core `module_abstractor` expand logic in isolation using synthetic graph data:

| Class | Focus |
|---|---|
| `TestExpandFileNode` (9 tests) | Children count, ID format, parent field, CONTAINS edges, no orphans, no duplicates |
| `TestExpandFunctionCallees` (4 tests) | Callee list, parent assignment, edge kinds, empty callee case |
| `TestCalleeOverwrite` (2 tests) | Reproduces the parent-overwrite bug; verifies skip-if-exists fix |
| `TestDagreEdgeCompleteness` (3 tests) | Proves pruning drops middle CONTAINS edges; verifies full-edge dagre input |
| `TestCrossFileCallees` (2 tests) | Cross-file and intra-file callee parent correctness |

### `test_self_graph.py` — Integration tests (113 tests)

Ingests the Flowify repo itself via `pipeline.ingest()` and verifies the resulting graph. Session-scoped fixture means ingestion runs once for all 113 tests.

| Class | Tests | What it pins |
|---|---|---|
| `TestPayloadFiles` | 12 | Every core `.py` + MCP file parsed; `.venv`/`__pycache__` excluded |
| `TestPayloadFunctions` | 35 | Named functions exist in expected files |
| `TestPayloadCallEdges` | 16 | Critical cross-file CALLS edges |
| `TestPayloadInvariants` | 8 | No duplicates, no orphans, no self-loops, ≥80 functions |
| `TestEntryPoints` | 9 | `main.py` first, `file::` prefix, no test files, ≥10 symbols |
| `TestExpandMainPy` | 10 | Children structure, CONTAINS edges, no orphans |
| `TestExpandPipelinePy` | 4 | `ingest`/`update` in children, intra-file CALLS |
| `TestExpandPipelineIngestCallees` | 5 | `graph_builder`, `storage`, `module_abstractor` reachable |
| `TestExpandGraphBuilderPy` | 4 | `_Visitor` visible, `build_function_graph` in payload |
| `TestGraphReachability` | 6 | BFS from `main.py` reaches all 6 core modules |

### Running

```bash
cd backend
.venv/Scripts/python -m pytest ../tests/ -v          # Windows
python -m pytest ../tests/ -v                        # Linux/Mac
```

---

## 12. Running the Project

### Quick start (one command)

```bash
bash run.sh           # starts backend + frontend
bash run.sh --test    # same, plus smoke-tests all API endpoints
```

### Manual

```bash
# Backend
cd backend
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend (new terminal)
cd frontend
npm install
npm run dev
```

- Frontend: http://localhost:5173
- Backend: http://localhost:8000
- Interactive API docs: http://localhost:8000/docs

### Workflow

1. Paste an absolute path to any local repo in the sidebar → **Ingest**.
2. Up to 4 entry-point file nodes appear on the canvas.
3. **Click a file node** → expands to its function/class symbols.
4. **Click a function node** → expands to its callees.
5. Use **← Back** to return to the previous view, **⟲ Reset** to return to root.
6. Type a question in the bottom bar → **Ask** → results slide in from the right.
7. Click **"Copy context for LLM"** to export the relevant subgraph as markdown.

### MCP server (Claude / Copilot integration)

```bash
cd mcp_server
pip install -r requirements.txt     # mcp[cli], httpx
python -m flowify_mcp               # or: mcp install flowify_mcp.py
```

Tools exposed: `ingest_repo(repo_path)` and `query_repo(graph_id, query)`.
