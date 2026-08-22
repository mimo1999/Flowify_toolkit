# Flowify — Technical Handoff

This document is a from-first-principles technical account of what is actually implemented in this repository, how it works, and why it was built this way. It is derived from reading the source directly (not from README/marketing copy), and it calls out rough edges and known limitations wherever they exist in the code, not just the happy path.

Scope: backend pipeline (`backend/app/`), frontend (`frontend/src/`), MCP server (`mcp_server/`).

---

## 1. System shape

Flowify ingests a repository once into a durable, queryable graph (SQLite-backed), then serves that graph through three consumer surfaces that all sit on top of the *same* stored payload:

- **FastAPI HTTP API** (`backend/app/main.py`) — consumed by the React UI and by anything else that wants raw HTTP.
- **MCP stdio server** (`mcp_server/flowify_mcp.py`) — wraps a subset of the same FastAPI endpoints as 14 tools for AI coding assistants, with its own resilience layer (circuit breaker, retry, dedup/cache) bolted on top.
- **Bob export / CLI** (`bob_export.py`, `bob_graph_cli.py`) — one-shot JSON dump of the full graph for an external IBM "Bob" agent, via HTTP (`/bob/graph`) or a standalone CLI process.

Everything funnels through one core object, a `GraphPayload` (defined in `models.py`): `function_nodes`, `function_edges`, `module_nodes`, `module_edges`, `module_to_functions`, `semantic_edges`. This is built once at ingest time by `pipeline.ingest(repo_path)`, persisted to SQLite (`storage.py`), and reloaded per request — nothing downstream re-parses source code; every endpoint operates on the cached graph.

### 1.1 Two parallel API surfaces

Almost every capability exists twice: a "legacy"/UI-facing endpoint (`/ingest_repo`, `/query`) and an `/mcp/...` counterpart (`/mcp/ingest`, `/mcp/query`). The `/mcp/` versions add: a stable `repo_id` (SHA-256 of `repo_path`, truncated to 12 hex chars) independent of the graph's own random `graph_id`; idempotent ingest (scans existing graphs for a matching `repo_path` before re-ingesting); and normalized, always-200, `{success, error}`-shaped responses instead of raising HTTP errors — because MCP tool-calling clients generally want a structured failure they can hand back to the LLM, not an exception to catch.

---

## 2. Ingestion pipeline (`pipeline.ingest`)

`pipeline.ingest(repo_path)` is the top-level orchestrator. Exact sequence:

1. **Phase 1 — repository context.** `llm_provider.analyze_repository(repo_path)` → `RepositoryContext` (project_type, domain, architecture, tech_stack, purpose, key_entry_points, critical_modules, confidence, fallback_used). Always computes a heuristic baseline first (directory/manifest sniffing), then overlays LLM output on top if available — so this step never "fails," it only degrades.
2. **CIR construction.** `graph_builder.build_function_graph(repo_path)` → `(networkx.DiGraph, function_nodes, function_edges)`. This is the language-agnostic Canonical Intermediate Representation build (§3).
3. **Phase 2 — semantic enrichment.** `pipeline._enrich_functions(nodes, g, repo_context)` — batched LLM calls that mutate each node's `.summary`/`.semantics` in place and produce `SemanticEdge` objects (§4).
4. Node-level semantics (summary, intent, complexity, criticality) are pushed onto the `networkx` graph's node attributes — this is how module clustering later gets access to Phase-2 signals like `intent == "orchestration"` without re-threading them through function arguments.
5. **Phase 4/5 — module abstraction.** `module_abstractor.build_modules(...)` → `(module_nodes, module_edges, module_to_functions)`, internally calling `control_flow_analyzer.group_control_flow_functions` (§5).
6. Assemble `GraphPayload`, `storage.new_graph_id()`, `storage.save(payload)`.
7. Persist `repo_context` as graph metadata.
8. **Whole-graph LLM normalization pass.** `llm_ingestion.ingest_ast_results(...)` — a *second*, separate LLM call over the entire graph (not per-function) that asks for a structured `role/responsibilities/inputs/outputs/dependencies/side_effects` per node, treating the already-built modules as ground truth the LLM must respect. Stored as metadata, along with the prompt itself (for debugging/audit).
9. `git_updater.changed_files_since(repo_path, None)` → captures current HEAD as the baseline for future incremental updates.
10. `learning.seed_terminology_from_graph(graph_id)` — cold-start bootstrap for the continuous-learning terminology map (§8), best-effort (swallowed on exception).
11. `pipeline._build_flow_summary(...)` — LLM prose (`what`/`usecase`/`techniques`/per-module flow descriptions) + a **deterministically generated** Mermaid architecture diagram (never LLM-authored, so it's always syntactically valid and always references real module_edges).
12. `pipeline._build_knowledge_layer(...)` — calls `knowledge.build_knowledge` (docs + rationale, §6) and `graph_analytics.compute_analytics` (§7) independently, each in its own try/except, and stores both as metadata.

**Reliability design decision, explicit in the code:** only steps 1–6 are load-bearing. Everything after (steps 8–12) is wrapped in try/except with a print-and-continue on failure — ingestion cannot be broken by an LLM outage or an analytics bug. This is why `main.py` has on-demand rebuild endpoints (`POST /knowledge/{id}`, `GET /graph_analytics?refresh=true`, `POST /flow_summary/{id}`) for graphs that predate a feature or whose best-effort step failed silently.

### 2.1 Incremental update (`pipeline.update`)

Triggered via `POST /update`. Uses `git_updater.changed_files_since(repo_path, last_head)` (GitPython, soft dependency — degrades to a no-op if unavailable or not a git repo) to get the diff since the last ingest's HEAD.

- No changes → returns the existing payload untouched.
- Otherwise: drops nodes/edges belonging to changed files, re-parses only the changed files via `graph_builder.parse_file`, **re-resolves symbol edges** over the merged node set (this duplicates — with slight divergence — the `<symbol>::` resolution logic in `graph_builder.build_function_graph`; a maintenance smell flagged below), re-runs Phase 2 enrichment **only on the new/changed nodes** (old semantics are preserved untouched), then **rebuilds modules from scratch over the entire merged graph** — module clustering is not incremental, and module IDs (`mod_<8-hex>`) are **not stable across updates**, so any external reference to a module ID can go stale after a sync.

**Known limitation, worth flagging explicitly:** `changed_files_since` filters the git diff to files whose extension resolves to a supported source language (`ingestion.detect_language`). A commit that only touches Markdown docs produces an empty changed-file set, so `update()` short-circuits *before* the knowledge-layer rebuild runs — meaning **doc-only edits do not trigger a knowledge-layer refresh** automatically; you have to call `POST /knowledge/{id}` manually after doc changes.

---

## 3. Canonical Intermediate Representation (CIR) — `graph_builder.py`, `ingestion.py`, `models.py`

### 3.1 File discovery (`ingestion.py`)

Pure filesystem walk, no parsing. `SUPPORTED_EXTENSIONS_BY_LANGUAGE` covers `python, javascript, typescript, java, c, cpp`. `iter_repo_files` uses `os.walk` with in-place `dirnames[:]` pruning against `IGNORED_DIRS` (`.git, .venv, venv, __pycache__, node_modules, .tox, dist, build, .mypy_cache`) and any dotdir. Language detection is suffix-only — no shebang/content sniffing, so a `.py` file with no extension would be invisible.

### 3.2 Per-language parsing (`graph_builder.py`)

**Python gets a real parser; every other language is regex/brace-counting.** This asymmetry is deliberate and load-bearing for the rest of the system's accuracy claims — Python analysis (call resolution, control-flow grouping, semantic classification) is meaningfully more reliable than JS/TS/Java/C/C++.

- **Python** (`parse_python_file` → `ast.NodeVisitor` subclass): walks `ClassDef`/`FunctionDef`/`AsyncFunctionDef` with a `qual_stack` for dotted qualified names (`Class.method`). Extracts a `CIRSignature` (params with type annotations; `type_system` is `"explicit"` if any annotation exists, else `"dynamic"`), a `CIRSourceSpan` (line/col), the exact `code_snippet` (source slice by line range), and decorators. Call edges come from `ast.walk`-ing every function body for `ast.Call` nodes and extracting the callee name — but **only** through `Name` or `Attribute.attr` (so calls through a variable holding a function reference, `getattr`, or higher-order results are invisible to the graph). Import edges (static `import`/`from...import`, and dynamic `__import__`/`importlib.import_module` with a literal string argument) become placeholder edges targeting `<module>::<fullname>`.
- **JS/TS, Java, C/C++**: line-oriented regex matching. A `class_stack` with a hand-rolled brace-depth counter (`_brace_end_line`, strips `//` comments but **not** `/* */` block comments or multi-line strings) determines where a class/function body ends. Call names are extracted via one shared regex with a keyword-exclusion list (`if/for/while/switch/catch/return/new/throw/typeof/sizeof/delete/await/function/class/super`). This is explicitly fragile against multi-line generics (`Map<String, List<Foo>>`), braces inside strings/block comments, and any construct the regex wasn't tuned for. The module docstring itself says: *"The call graph is approximate... good enough for a hackathon visualization."*

### 3.3 Semantic-kind heuristic

Every function/method gets classified by name/code/decorator pattern-matching into `EXPOSES_API | USES_DB | EMITS_EVENT | CONSUMES_EVENT | CALLS` (checked in that priority order — e.g. route decorators win over DB-substring matches). Stored in `adapter_metadata["semantic_kind"]`. This single field is reused pervasively downstream: `ExecutionStep.semantic_kind` in query responses, `ImpactAnalysis.semantic_kind`/risk scoring, GraphView's node coloring, and the dead-code filter in `graph_analytics.py` (decorated/API/event-consuming functions are assumed framework-invoked and excluded from dead-code candidates).

### 3.4 Symbol resolution — tiered and scoped, not a flat name fan-out

`build_function_graph` builds several indexes (`name_index`, `file_functions_index`, `class_attr_types_by_file`, `file_import_maps`, `file_stem_index`) and resolves each placeholder `<symbol>::X` edge through `_resolve_call_target`, a tiered resolver that takes the **first matching tier and only that tier** — it never unions across tiers and never fans out to every same-named node. Call sites now carry real context captured at emission time (`_visit_func`/`_Visitor`): the receiver expression (`self._preprocessor` in `self._preprocessor.fit()`), the enclosing class, and a per-class `self.x = Foo()` attribute-type map built from scanning `__init__`.

Tiers, checked in order, first match wins:

1. `self.foo()` inside a known enclosing class → that class's `foo`
2. `self.attr.foo()` where `__init__` recorded `attr`'s type → `TypeName.foo`
3. `Receiver.foo()` where `Receiver` is a class name in scope → `Receiver.foo`
4. `module_alias.foo()` where `module_alias` is an import → `foo` in the file that alias points at (via `file_stem_index`)
5. bare `foo()` → a function defined in the same file
6. bare `foo()` → a function uniquely named repo-wide (still gated on true uniqueness even if a receiver was present but unresolved, at reduced confidence)
7. still ambiguous → **the edge is dropped**, not guessed

Each resolved edge's tier and confidence are stored in `adapter_metadata["resolution_confidence"]`/`resolution_tier"`, and `knowledge.edge_provenance` reads that instead of the old flat `0.9` for every python_ast call edge — so provenance confidence now reflects how the edge was actually resolved. Self-loops are explicitly guarded against even when a call is genuinely recursive (matching the pre-existing `test_no_self_loops` invariant), and a name with zero/ambiguous matches is silently dropped from `function_edges`, same as before.

- `<module>::X` → becomes a synthetic external node `external::module::X` (deduplicated across multiple imports of the same module).
- Edges are deduplicated by `(source_id, target_id, type)` — multiple call-sites of the same callee within the same function collapse to one edge, but different edge *types* between the same pair (e.g. both a CALLS and an IMPORTS edge) survive independently.
- **Optional pyan augmentation**: if `FLOWIFY_PYTHON_EDGE_BACKEND=pyan` is set, `pyan.analyzer.CallGraphVisitor` runs a proper cross-module static call graph for Python and adds any edges not already present. Off by default; fails soft on ImportError or any exception.
- **Non-Python** (`_append_call_edges`, regex-based — no receiver/enclosing-class context available) keeps the same-file / imported / drop-on-ambiguous shape but at lower confidence (0.5–0.7), since it can't do the receiver/attribute-type tiers Python gets.

Measured impact on a real external repo (`google-research/tabfm`, 500 sampled edges): edges pointing at an ambiguous (multi-node) name dropped from 99.2% to a small minority, `source.name == target.name` same-name-fan-out edges dropped from 47% to ~0%, and the top-15 functions by out-degree went from being 13× `__init__` (a pure fan-out artifact — every `__init__` "calling" every other `__init__`) to real orchestration functions. This mattered beyond cosmetics: out-degree drives god-node ranking, PageRank, and what `/export` selects, so the fan-out was actively corrupting the "most important functions in this codebase" answer.

This is still a recurring theme worth knowing, just a narrower one now — `graph_analytics.py`'s cycle detector still filters out cycles where every node shares the same name (a defensive check against any resolver mistake, no longer the primary defense it once was), and `flow_verifier.py` still cross-checks FLOW edges against real imports for the same reason. `tests/test_self_graph.py::TestPayloadInvariants::test_no_same_name_call_fanout` is the regression guard: it fails if any group of ≥3 same-named nodes is ever found completely fanned-out to each other again.

### 3.5 CIR data model (`models.py`)

`CIR_VERSION = "cir.v1"`. The schema is mid-migration from a Python-flavored vocabulary to a language-agnostic one, bridged automatically:

- Legacy: `NodeType = function|class|method|file`, `EdgeType = CALLS|DEFINES|IMPORTS|INHERITS|OVERRIDES|FLOW`.
- Canonical: `CIRNodeKind = file|namespace|type|function|callable|data|external`, `CIRRelationship = CONTAINS|INVOKES|DEPENDS_ON|EXTENDS|IMPLEMENTS|OVERRIDES|FLOWS_TO`.
- `CIRNode.model_post_init` auto-derives `kind` from `type` (and vice versa is not attempted) via a fixed mapping table if `kind` wasn't explicitly set; same pattern for `CIREdge.relationship` from `type`. This means any code path that only sets the legacy field still gets a valid canonical field for free.
- `FunctionNode`/`FunctionEdge` are literally empty subclasses of `CIRNode`/`CIREdge` — pure aliases kept for naming continuity with older call sites; this is what `graph_builder`, `pipeline`, and `storage` actually construct.
- Node IDs: `"<repo-relative-file-path>::<dotted-qualified-name>"`. Every file gets a synthetic root node with qualname `<file>`. External module nodes: `"external::module::<modname>"`.

---

## 4. Semantic enrichment — Phase 2 (`pipeline._enrich_functions`, `llm_provider.py`)

### 4.1 Batching (the primary cost-control mechanism)

Non-trivial callable nodes are accumulated into batches of `FLOWIFY_LLM_BATCH` (default 10) and sent to `provider.analyze_functions_batch(batch, repo_context)` as **one prompt per batch** rather than one call for summary + one call for semantics per function — explicitly documented as collapsing what used to be ~2N LLM calls into ~N/10. The response is a JSON array with an `id` field per object matching the `[N]` label used in the prompt; if the LLM drops an item or returns malformed JSON, that specific item falls back individually to the heuristic path — so batch output always covers every input 1:1 even under partial LLM failure.

`_is_trivial(n)` routes dunders (except `__init__`), functions ≤6 non-blank lines, or bodies <160 chars to the free heuristic provider regardless of which real LLM is configured — cost-saving for boilerplate.

### 4.2 LLM provider abstraction (`llm_provider.py`)

An `LLMProvider` ABC — every subclass implements only `_call(prompt) -> str`; all prompt construction, JSON-parsing/fallback, and caching live in the base class. Providers: `Ollama`, `Bob` (IBM watsonx), `Anthropic`, `OpenAI`, `Copilot(OpenAI)`, `OpenClaw(OpenAI)`, `Heuristic` (always available, no LLM).

**Selection**: explicit `LLM_PROVIDER` env var (with aliases) overrides auto-detection. Auto-detect order, strictly: Ollama reachable locally (`GET /api/tags`, 2s timeout) → `BOB_API_KEY` → `ANTHROPIC_API_KEY` → `OPENAI_API_KEY` → `GITHUB_TOKEN` (Copilot) → `OPENCLAW_API_KEY` → `HeuristicProvider`. A module-level singleton is created lazily and logged once.

**Disk cache**: `_store/llm_cache/{sha256(prompt)[:32]}.json`. The cache key is the *entire raw prompt string* — any change to a prompt template (wording, whitespace, injected context) silently invalidates all prior entries for that call type; there is no cache versioning. Responses starting with `"(stub)"` or containing an `[... error:` marker are never cached, and — notably — if a *previously cached* stub/error response is read back, the cache file is deleted on the spot so the next call retries live. This means the cache self-heals once a provider comes online, at the cost of re-paying for one live call. Cache writes are best-effort/non-fatal.

**Failure handling**: `ask()` catches *any* exception from `_call` uniformly and degrades to `HeuristicProvider()._call(prompt) + "\n[llm error: ...]"`, uncached. No retry/backoff, no streaming, no token/cost accounting anywhere in this layer — a transient network blip and a permanent auth failure look identical to the caller.

### 4.3 Heuristic (no-LLM) fallback is real static analysis, not a stub

When the heuristic provider is active — or when any structured call falls back after an LLM/JSON failure — the system still does meaningful analysis, entirely via keyword/AST heuristics: project-type/tech-stack sniffing from `requirements.txt`/`package.json` keywords and directory-name patterns; entry-point scoring from filename conventions, `if __name__ == "__main__"`, `argparse` usage; per-function `intent`/`complexity`/`criticality`/`side_effects`/`patterns` from name+code substring buckets and line-count thresholds; and a multi-tier one-liner generator (AST docstring → AST return/call-shape heuristic → name-decomposition-to-verb-dictionary heuristic). This is why the system is usable end-to-end with zero API keys configured, at reduced quality.

### 4.4 Downstream LLM touchpoints (for completeness)

- `explain_flow_with_graph` — the RAG answer generator (§8), explicitly instructed to stay "grounded... not source file text," fed the last 2 conversation turns for follow-up continuity.
- `interpret_query` — semantic re-ranking of retrieval candidates (§8).
- `summarize_repository_flow` — prose sections of the architecture report; the Mermaid diagram itself is always generated deterministically, never by the LLM.
- `analyze_repository` — merges LLM-derived `key_entry_points` with the heuristic set (union + dedup) rather than replacing it outright.

---

## 5. Module abstraction — Phases 4/5 (`module_abstractor.py`, `control_flow_analyzer.py`)

### 5.1 Clustering algorithm

1. Restrict the graph to code-symbol nodes (`function|callable|type`, dropping `file`/`external` nodes) and convert to undirected.
2. Per **connected component** independently: components with ≤4 nodes are kept as one cluster verbatim; larger components run `networkx.algorithms.community.greedy_modularity_communities` (Clauset-Newman-Moore greedy modularity maximization); any exception falls back to treating the whole component as one cluster.
3. **Directory-based re-merge**: pure call-graph modularity ignores directory structure and, per the code's own comment, fragments low-internal-coupling directories (test suites are the named example — each test mostly calls code-under-test, not other tests, so they'd otherwise scatter into many singleton-like same-named clusters). Every cluster's majority source directory (`_name_hint`, via `PurePosixPath.parts`) is computed, and clusters sharing a hint are merged back together.

This means final module boundaries are a deliberate blend of call-graph topology *and* directory structure, not a pure graph-theoretic result — worth knowing when debugging "why are these two clearly-different areas one module."

### 5.2 Per-module metadata

For each final cluster: an LLM (or heuristic) call over up to 30 member summaries produces `{name, description}`. Entry-point tagging checks whether any cluster function lives in a `RepositoryContext.key_entry_points` file and is named `main/run/start/train` or ends in `_main`. `submodule_type` is assigned in priority order: `entry` (has entry functions) → `core` (any member has Phase-2 `intent == "orchestration"`, read off the `networkx` node attributes set earlier in the pipeline) → `control_flow` (any control-flow groups found) → `utility`. Control-flow grouping comes from `control_flow_analyzer.group_control_flow_functions`.

**Module FLOW edges are unweighted despite being built from a weighted aggregation**: cross-cluster call counts are tallied per `(source_module, target_module)` pair, but only used to decide *whether* an edge exists (≥1 call) — the count itself is discarded; `ModuleEdge` has no weight field.

### 5.3 Control-flow grouping (`control_flow_analyzer.py`)

Python-only (guarded on `source_language == "python"` and a non-empty `code_snippet`) — a **second, independent** `ast.parse` of the already-extracted snippet (decoupled from `graph_builder`'s visitor at the cost of redundant CPU). Walks `If`/`Try`/`For`/`While` nodes to bucket calls into `conditional_branch`, `try_block`, `error_handling`, `loop`. Crucially, a call name only counts if there's a **real edge** between the two functions already present in `function_edges` restricted to same-module pairs — this double-check (name match *and* resolved-edge match) avoids false positives from name collisions that exist outside the actual resolved call graph.

### 5.4 Entry-point *file* heuristics (`find_entry_files`) — separate from entry-point *function* tagging above

Used to pick the initial graph view, not part of ingestion. A hardcoded filename-score table (`main.py:45, __main__.py:45, train.py:20, cli.py:15, ...`) plus a penalty list (`__init__.py, constants.py, config.py, utils.py, ...` −40) plus source-level signals (`if __name__=="__main__"` +35, `argparse` +25, bare `main()` call +20). Declared entries from `RepositoryContext.key_entry_points` take priority *if* at least one matches a known boost pattern — otherwise pure structural scoring is used, guarding against an uninformed LLM guess dominating the result.

### 5.5 Depth-based collapse/expand API (query-time, reused by `/graph` and `/expand`)

`collapse_for_depth(depth)`: depth 1 = modules + FLOW edges; depth 2 = per-module file-level submodules with aggregated cross-file FLOW; depth 3 = real functions + real CALLS + CONTAINS. `expand_node`/`expand_file_node` drive interactive drill-down, capping fan-out via `_rank_and_cap` (`_MAX_CHILDREN=12` file-level, `_MAX_SYMBOL_CHILDREN=40` symbol-level) to keep the canvas readable.

### 5.6 Flow-edge verification (`flow_verifier.py`)

A separate deterministic sanity layer for FLOW edges: `verify_flow_edge` requires ≥1 real cross-file call *and*, if `require_import=True`, a matching import edge — flagging calls-without-imports as likely false positives from the name-based resolver. The batch entry point `filter_verified_flow_edges` deliberately defaults `require_import=False`, "since not all languages track imports" reliably (Python AST does; the regex adapters for other languages may not) — an explicit, documented inconsistency from `verify_flow_edge`'s own default, worth knowing if you call the single-pair function directly expecting the batch function's laxer behavior.

---

## 6. Knowledge layer — documents & rationale (`knowledge.py`)

Fully deterministic, zero LLM calls. Two independent extraction passes, both persisted under graph metadata key `"knowledge"`.

### 6.1 Document graph

Scans `.md/.markdown/.rst/.adoc` files (capped at 200 docs, 512KB each), classifies each by filename/path regex into `readme|changelog|contributing|adr|rfc|wiki|guide|doc`. A fence-aware line scanner extracts markdown headings into `DocumentSection`s. Code-mention linking (`_extract_doc_edges`) produces `KnowledgeEdge`s at three distinct confidence tiers: markdown links resolved relative to the doc (0.95), backticked code spans matched against known file paths or a name index (0.9/0.85), and bare prose identifiers matched via regex with no code formatting (0.6, deliberately the weakest signal, capped at 2 matches per token). The name index requires names ≥4 chars, so short real function names (`run`, `get`) are never doc-linked.

### 6.2 Rationale extraction

Regex `(?:#|//|/\*|\*|<!--)\s*(TODO|FIXME|HACK|NOTE|WHY|XXX|BUG|WARNING|OPTIMIZE)\b[:\s-]*(.+)` across all source files (cap 500 notes). Attachment to an enclosing function: builds `(start_line, end_line, node)` spans per file, **sorts by span size ascending**, so the innermost/smallest enclosing function wins when a comment sits inside nested spans; unenclosed comments stay file-level.

### 6.3 Provenance

Every knowledge/code edge carries a `Provenance(source, confidence, evidence, reasoning)`. `edge_provenance` maps CIR adapter name → tier: `python_ast`→(ast, 1.0), `pyan`→(static_analysis, 0.9), the four regex structural adapters→(regex, 0.7); Python call edges resolved by name-matching get a special-cased (ast-derived but resolution-uncertain) 0.9. `semantic_edge_provenance` maps Phase-2 `inferred_by` (`bob`/heuristic/other) to `llm`/`heuristic`/`ast` respectively. This is what powers the "70% confidence — regex" style badges in the frontend's `ImpactPanel`.

---

## 7. Deterministic graph analytics (`graph_analytics.py`)

Pure `networkx` computation, no LLM. Shares its "what counts as a callable node/call edge" predicate definitions with `retrieval.py` by explicit design decision (to avoid the two diverging) — though `flow_verifier.py` unfortunately re-implements the same predicates independently rather than importing them, a maintenance inconsistency.

- **Centrality**: `pagerank` (alpha=0.85), in/out-degree, `betweenness_centrality` — exact if ≤800 nodes, else sampled (k=min(200, n)) for cost. **God-node score** is a hand-tuned composite: `pagerank*100 + in_degree*0.5 + out_degree*0.25 + betweenness*50` — hardcoded weights, not derived from any normalization scheme, top 20 kept.
- **Cycles**: `nx.simple_cycles(length_bound=8)`, capped at 25 results. Cycles where every member shares the same name are discarded as name-resolution artifacts (see §3.4) rather than genuine circular dependencies.
- **Bridges**: articulation points of the undirected call graph — nodes whose removal disconnects the graph, framed as architectural bottlenecks, top 15 by degree.
- **Dead code**: callable nodes with zero fan-in, name not in a fixed entry-name set (`main, __init__, __main__, setup, run, app, handler`), not a test, no decorators (decorated = assumed framework-invoked), and `semantic_kind` not `EXPOSES_API`/`CONSUMES_EVENT` — capped 50. No awareness of reflection/`getattr`-based dispatch, so those false-positive as dead.
- **High-risk**: ≥2 of {fan_out≥10, fan_in≥15, complexity high/very_high}, capped 20.
- **Surprising couplings**: cross-directory call-edge pairs with ≤2 total edges — sparse, rare links between otherwise-unrelated subsystems, capped 15.
- **Per-file rollups** for the UI heatmap: pagerank *summed* across a file's nodes (valid since PageRank is a probability mass), degree summed, complexity/criticality taken as the file's single worst offender.

`shortest_path(payload, source_name, target_name)`: exact → case-insensitive → prefix name-resolution cascade, then plain unweighted BFS shortest path.

---

## 8. Retrieval / GraphRAG (`retrieval.py`) and continuous learning (`learning.py`)

### 8.1 Retrieval is hybrid keyword + LLM re-rank — **not** embedding-based

There is no vector store or embedding model anywhere in the retrieval path. The pipeline:

1. Tokenize the query (regex splits camelCase/snake_case/digits), then expand tokens through a hand-authored ~40-group synonym table covering common backend concepts (auth, db, api, cache, events, payments...), plus a prefix/substring fuzzy match for tokens ≥5 chars.
2. Compute smooth IDF over `"{name} {summary} {file_path}"` strings for every code-symbol node.
3. Score every node: name hits ×3.0, summary hits ×1.5, path hits ×0.5, intent hits ×1.0, each × that token's IDF.
4. Take the **top-30** scorers as an LLM shortlist, with a `{name: summary[:80]}` context dict so the LLM judges semantically rather than lexically.
5. `bob_client.interpret_query(query, names, context)` — the LLM selects up to 10 names from the shortlist; results are merged with `learning.get_terminology_suggestions` (names the learning system has previously mapped to similar terms).
6. **Guaranteed non-empty fallback**: after LLM selection, the top-5 TF-IDF scorers with score > 0 are always appended if not already present — so a garbage/empty LLM response never produces an empty result.

From there, `retrieve_subgraph` does a BFS (up to `max_hops=2`) from all selected entry nodes simultaneously, following only INVOKES/CALLS edges, and `explain()` hands the ordered node summaries + adjacent edge labels + last-2 prior conversation turns to `explain_flow_with_graph` for the final prose answer.

**Edge grounding is keyed off the real BFS discovery parent, not list position.** `retrieve_subgraph` now also returns a `parent: {child_id: discovering_id}` map built during the same traversal (first-discovery wins, matching BFS shortest-path semantics), and `explain()`/`build_execution_steps()` use `parent_map.get(nid)` to look up each node's edge, not `ordered_ids[i-1]`. This mattered because BFS visits nodes level-by-level — a node's immediate predecessor in the flat `order` list is frequently an unrelated sibling with no edge to it at all, not the node that actually called it. Before this fix, `edge_info`/`edge_label` came back sparse or empty even on a well-connected subgraph, while the prompt still told the LLM the node list was "ordered by execution flow" — so with few real edges to cite, `explain_flow_with_graph` would fill the gap with a plausible-sounding but fabricated call chain and unsupported event-emission claims. `retrieve_subgraph`'s return signature changed from a 4-tuple to a 5-tuple (`order, subgraph, query_id, g, parent`) to carry this through to both call sites in `main.py`.

### 8.2 Continuous learning (`learning.py`)

Every query is recorded (`record_query`): a `QueryPattern`, updated `UsageStatistics` per retrieved node, and a **terminology map** update — query terms (stopword-filtered) get associated with the retrieved node names, with `confidence = min(1.0, frequency/10.0)` (a term needs 10 distinct queries to reach full confidence). `seed_terminology_from_graph` cold-starts this map at ingest time from discriminative function-name substrings (excluding terms present in >20% of all names, to filter out generic words like "get"/"set"), seeded at `frequency=4` — deliberately landing just above the `>0.1` threshold `get_terminology_suggestions` uses, so the very first real query benefits without waiting for repeated use.

Feedback (`record_feedback`) nudges `avg_relevance_score` ±0.1 (thumbs up/down) or +0.2 for an explicit user correction (`corrections.better_nodes`). `update_node_importance` (a separate, presumably periodic job) ratchets a node's `criticality` up one level once it's been accessed >10 times with `avg_relevance_score > 0.7` — this is the mechanism by which usage feeds back into `graph_analytics`'s high-risk/criticality-driven views.

**No decay anywhere** — access counts and terminology frequency only grow; a term or node's learned relevance never ages out even if it stops being queried. Conversation memory (`load_conversation`/`save_conversation`) keeps a sliding window of the **last 3** turns per `conversation_id`, though `explain_flow_with_graph` only actually uses the last 2 when building its prompt (an unreconciled off-by-one, not obviously intentional).

---

## 9. Persistence (`storage.py`)

SQLite at `$FLOWIFY_STORE/flowify.db` (default `_store/`), WAL mode, thread-local connections. Seven tables: `graphs`, `nodes`, `edges`, `module_nodes`, `module_edges`, `module_to_functions` (junction, `INSERT OR IGNORE`), `semantic_edges`, `metadata` (generic key/value JSON blob keyed by `graph_id`+`key` — this is what `repo_context`, `llm_ingestion`, git head, `flow_summary`, `knowledge`, and `analytics` are all stored as).

`save()` is a full transactional replace: deletes all rows for a `graph_id` across every table, then bulk-reinserts — not a diff/merge, consistent with `pipeline.update()` always building a fresh merged payload before saving. `load()` vs `load_light()`: both route through the same loader, but `load_light` selects an explicit column list with `NULL AS code_snippet` to avoid transferring large source blobs on read-only paths (query/expand/impact/analytics/display); `load()` is reserved for paths that may re-save the graph and therefore need real snippets (`pipeline.update`, description regeneration, importance updates). Nested Pydantic fields (`signature`, `source_span`, `semantics`) round-trip as raw JSON and are re-coerced into model instances by Pydantic at construction time, not manually reconstructed.

---

## 10. Report generation (`report.py`)

Pure assembly of already-cached artifacts (`repo_context`, `flow_summary`, `knowledge`, `analytics`) into one Markdown document — no LLM calls of its own. Sections: repository summary, language distribution, entry points, the deterministic Mermaid architecture diagram, major components (from `flow_summary.flows`), top-15 most-important functions (god nodes), architectural bottlenecks (bridges), high-risk components, circular dependencies, dead-code candidates, surprising couplings, a documentation map (docs ranked by inbound reference count), developer rationale highlights (HACK/FIXME/WHY/BUG/WARNING only), and a rule-based "suggested questions" list (top god nodes, project-type-specific canned questions, a cycle-specific question if any cycle exists, a HACK/FIXME-specific question if any exist). Sections with no data are silently omitted rather than shown as empty — the one exception is High-Risk, which explicitly renders "_No components matched multiple risk signals._"

---

## 11. FastAPI surface (`main.py`)

Every endpoint follows the same skeleton: `payload = storage.load_light(graph_id); if None: 404; ... ; return`. Families:

- **Ingest**: `/ingest_repo`, `/mcp/ingest`, `/bob/graph` — all funnel to `pipeline.ingest`/`bob_export.build_bob_graph_response`.
- **Read/navigate**: `/graph` (depth-collapsed snapshot), `/expand` (lazy single-node drill-down), `/entry_points`.
- **Query**: `/query`, `/mcp/query` — both call `retrieval.retrieve_subgraph` + `retrieval.explain`; conversation continuity via `conversation_id`.
- **Analysis**: `/impact` (BFS downstream-reachability risk scoring from callers/callees/DB-touching callees), `/graph_analytics`, `/knowledge`, `/node_references`, `/architecture_report`.
- **Learning**: `/feedback`, `/analytics`, `/hot_nodes`, `/common_paths`, `/update_importance`.
- **Export**: `/export/{id}?format=json|mermaid` (top-N nodes by out-degree, capped 80 by default), `/architecture_report`.
- **MCP-prefixed duplicates**: `/mcp/graphs`, `/mcp/impact` (name-addressable instead of node_id, with exact→case-insensitive→prefix resolution), `/mcp/find_node`, `/mcp/neighbors`, `/mcp/shortest_path`, `/mcp/search_rationale`, `/mcp/dead_code`, `/mcp/hotspots`, `/mcp/cycles`, `/mcp/architectural_summary`.
- **Dev convenience**: `/shutdown` — kills the process that owns the Vite dev-server port (via `netstat`/`taskkill` on Windows, `lsof`/`kill` elsewhere) and then `os._exit(0)`s the backend itself, on a daemon thread delayed 0.4s so the HTTP response can flush first.

`_generate_repo_id` (SHA-256 of `repo_path`, first 12 hex chars) is the stable identifier used across the MCP surface, distinct from the random `graph_id` SQLite key.

---

## 12. Frontend (`frontend/src/`)

React + Zustand (`store.js`, one flat `create()`, no slicing/middleware) + ReactFlow, Vite dev server, `/api` proxied to the FastAPI backend (note: `vite.config.js` was not inspected as part of this pass — confirm the exact proxy rule there if debugging routing).

### 12.1 Store (`store.js`)

Holds `nodes`/`edges`/`expanded` maps, `rootIds`, and a 50-entry-capped `viewHistory` undo stack. Two load strategies: `loadInitial()` (entry points only, lazy expand from there) and `loadDepthView(depth)` (whole-repo snapshot at a fixed collapse depth, replacing all expand state). `toggleExpand`/`deepExpand` drive incremental lazy loading via `GET /expand`; expanding a root auto-collapses sibling roots (accordion UX). `runQuery` **merges** the query's returned subgraph into the existing visible graph rather than replacing it (so highlighted results are guaranteed visible even if not previously expanded) — but query-injected nodes get `parent: null`, meaning they don't participate in the branch-lineage computation the rest of the graph relies on for coloring/depth, and always resolve as their own branch root (a visible inconsistency, not a crash).

### 12.2 GraphView (`GraphView.jsx`)

Custom compact-grid layout (`compactLevelLayout`/`layoutBranches`) replacing a former `dagre`-based layout — the switch is explicitly commented as fixing dagre's one-column-per-rank blowup on wide levels; the new layout buckets by hop-depth and packs each depth level into a roughly square grid, so footprint grows with `sqrt(n)` instead of `n`, at the cost of not minimizing edge crossings. `@dagrejs/dagre` remains a declared (now unused) dependency in `package.json`. Branch coloring walks each node's `.parent` chain to find its originating root and hop-depth, assigning one of 8 fixed hues per root and fading saturation/lightness with depth. Node sizing/border/glow encode fan-in/out, complexity, and PageRank ratio (global, whole-repo stats, not just the visible subset) as a heatmap. Every function/class node renders an expand chevron unconditionally (`hasCallees: true` hardcoded) — the client can't know ahead of a round-trip whether a node truly has callees, so leaf functions still show a (no-op) expand affordance.

**Render performance** — the layout algorithm was never the bottleneck (it's O(N) and memoized); the lag on large graphs came from React/ReactFlow render volume:

- `onlyRenderVisibleElements` is set on `<ReactFlow>` so offscreen nodes aren't mounted at all (viewport culling).
- Hover is deliberately *not* a dependency of the `rfNodes`/`rfEdges` memos anymore — those build the graph's default (non-hover) visual state only. Two small `useEffect`s keyed on `hoveredId`/`hoverNeighbors` patch `setNodes`/`setEdges` afterward, touching only the handful of nodes/edges whose dimmed/highlighted state actually flips (returning the same object reference for everything unchanged), instead of rebuilding all N nodes on every mouse-move.
- `FileNodeComponent`/`FunctionNodeComponent`/`LaneBackgroundComponent` are wrapped in `React.memo`, and their `data` object now passes `n.id` plus the store's stable `toggleExpand`/`deepExpand` references instead of a fresh `() => toggleExpand(n.id)` closure allocated per node per render — without that, `React.memo` had nothing stable to compare against and never actually skipped a re-render.
- Basic level-of-detail: each node component reads zoom via `useStore((s) => s.transform[2])` (ReactFlow v11's store hook, aliased `useRFStore`) and skips the PageRank glow filter and description text below 0.5 zoom.
- `fitView`'s animation is skipped above `FIT_VIEW_ANIMATE_THRESHOLD` (150) rendered nodes — the 350ms animated stretch reads as lag, not polish, once there's enough on screen.
- `store.js`'s `loadDepthView` (the whole-repo-at-a-fixed-depth path — the one most likely to blow past a sane render budget, e.g. depth-3 on a 2000-function repo) is capped at `MAX_RENDERED_NODES` (600), keeping the highest-degree nodes (by in+out degree within the returned view) and setting a `nodeBudget` the UI renders as a persistent "Showing N of M — zoom in or expand a module to see more" banner rather than truncating silently. Incremental `toggleExpand`/`deepExpand` aren't budgeted the same way — they're already bounded per-click (`deepExpand`'s own `maxNewNodes=60`, and file/symbol child-capping in `module_abstractor._rank_and_cap`), so the unbounded case was specifically the whole-repo snapshot path.

### 12.3 Other components

`Sidebar.jsx` — ingest/sync controls, depth selector, export buttons, a two-step-confirm shutdown button, and a `ProviderBadge` that independently polls `/provider_info` to show whether a real LLM or the heuristic stub is active. `ImpactPanel.jsx` — risk badge, caller/DB/module counts, doc references with `ProvenanceBadge` confidence, developer rationale notes. `InsightsPanel.jsx` — tabbed modal over the single cached `graph_analytics` payload (god nodes/high-risk/cycles/dead-code/couplings); clicking a row selects the node in the store but does **not** navigate the graph to make it visible, so `ImpactPanel` can end up showing data for a node that isn't on-screen. `SummaryPanel.jsx` — renders the LLM-authored architecture summary and its deterministic Mermaid diagram via the `mermaid` library, with a raw-code fallback if rendering throws. `QueryPanel.jsx`/`QueryResults.jsx` — query input and results (execution path, relevant code, feedback buttons, and a "copy as markdown context" action for pasting Flowify's grounded output into another LLM tool).

---

## 13. MCP server (`mcp_server/`)

`flowify_mcp.py` is an MCP stdio server (`mcp.server.Server`) exposing 14 tools, each a thin wrapper over one FastAPI endpoint, hardcoded to `http://localhost:8000`. `resilience.py` supplies three hand-rolled (no external library) primitives, used only by this server:

- **`CircuitBreaker`** — single **global** instance shared across *all* 14 tools (failure_threshold=5, recovery_timeout=60s, half_open_max_calls=3). Because it's global rather than per-route, a failure storm on one slow/LLM-backed endpoint can trip the breaker and fail-fast unrelated cheap endpoints too. `record_success()` in the closed state *decrements* the failure count by one rather than resetting to zero — failures decay gradually across successes.
- **`RequestDeduplicator`** — despite the name, this is actually a **5-minute TTL cache** (not just an in-flight-request merger): a completed result for identical params is served straight from cache for up to 300s even for sequential, non-concurrent calls; concurrent identical calls additionally share one in-flight `asyncio.Future`. Only `ingest_repo` uses it. There is no cache-invalidation hook tied to `delete_graph`, so deleting and re-ingesting the same `repo_path`/`repo_id` within the TTL window can return a stale cached result.
- **`retry_with_backoff`** — used only for `httpx.TimeoutException`/`httpx.ConnectError`; HTTP error-status responses are never retried, only network-level failures.
- **`HealthChecker`** — checks `GET /` at most once per 30s; the result is logged but is **advisory-only** — a failed health check does not gate or abort the actual request, so as currently wired it has no enforcement effect.

`_request()` in `flowify_mcp.py` chains all of the above: circuit-breaker gate → health check (log-only) → retry-wrapped HTTP call → record success/failure on the breaker. Every tool handler catches `HTTPStatusError` and generic `Exception` separately and always returns a text response (prefixed `✗ ` on failure) rather than raising — so at the MCP protocol level, a tool call "succeeds" even when the underlying operation failed; callers distinguish failure only by string-sniffing the leading `✗`.

`bob_graph_cli.py` is a third, independent entrypoint (`python -m app.bob_graph_cli`) for producing the same graph payload as a one-shot CLI process — notably redirects stdout to stderr during the build (`contextlib.redirect_stdout(sys.stderr)`) so that stdout is reserved purely for the final JSON dump, but the error path prints its JSON error object to **stderr** instead of stdout — success and failure payloads live on different streams, so a caller must branch on exit code before deciding which stream to parse.

---

## 14. Cross-cutting design themes worth internalizing

1. **Everything downstream of the CIR degrades gracefully to a deterministic/heuristic fallback rather than failing.** LLM outages, JSON-parse failures, and missing optional dependencies (GitPython, pyan) are all handled by falling back to something computed, never by raising past the ingest boundary (except for the CIR build itself, which is load-bearing).
2. **Call resolution used to be the single biggest accuracy caveat in the system** — a flat repo-wide name index fanned every call out to *every* same-named node, which measurably corrupted out-degree-driven rankings (god nodes, PageRank, `/export` selection; see §3.4 for the before/after numbers). It's now a tiered, context-aware resolver for Python (self/attribute-type/receiver-class/module-alias/same-file/unique-repo-wide, first match wins, ambiguous → drop) and a lower-confidence version of the same shape for the regex-based languages. The downstream defenses this caused are still in place, now as a second line of defense rather than the primary one: `graph_analytics.py`'s cycle detector still filters same-name cycles, `flow_verifier.py` still cross-checks FLOW edges against real imports, and dead-code detection still special-cases decorated/API functions.
3. **Non-Python languages are second-class**: real AST for Python only; everything else is regex/brace-counting, with correspondingly weaker guarantees on call graph accuracy, and control-flow grouping is Python-only entirely.
4. **The system is mid-refactor in a few places** — the CIR schema's legacy/canonical field duplication (bridged by auto-derivation), the duplicated `_is_invocation`/`_is_import` predicate definitions across `retrieval.py`/`graph_analytics.py`/`flow_verifier.py` (only the first two are explicitly kept in sync by design; `flow_verifier.py` diverges), and the unused `dagre` frontend dependency are all visible seams from incremental evolution rather than a single clean design pass.
5. **Retrieval has no vector/embedding component** — it's TF-IDF-with-synonyms plus one LLM re-rank call plus a guaranteed keyword-scored fallback. Anyone evaluating retrieval quality should test against vocabulary far from both the codebase's own naming and the synonym table, since that's the actual weak point (mitigated slowly, over time, by the learned terminology map — which itself never decays).
6. **Module IDs are not stable across incremental updates** (`pipeline.update` rebuilds module clustering from scratch every time) — any external system that persists references to `mod_xxxxxxxx` IDs across a repo sync will see them invalidated.
