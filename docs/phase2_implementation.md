# Phase 2: Semantic Graph Enrichment - Implementation Guide

## Overview

Phase 2 adds **semantic understanding** to the Flowify graph by analyzing functions with full context and adding semantic relationship types beyond the syntactic AST-based edges.

## What's New

### 1. Semantic Metadata for Functions

**Before Phase 2**: Functions only had name, summary, and code snippet.

**After Phase 2**: Each function now has rich semantic metadata:
- **Intent**: What the function does conceptually (orchestration, transformation, validation, etc.)
- **Complexity**: Cognitive complexity assessment (low, medium, high, very_high)
- **Criticality**: System importance (low, medium, high, critical)
- **Patterns**: Design patterns detected (factory, singleton, decorator, etc.)
- **Side Effects**: External interactions (file_io, network, database, state_mutation, logging)
- **Data Flow**: Input/output/transformation description
- **Confidence**: Bob's confidence in the analysis (0.0 to 1.0)

### 2. Semantic Edge Types

**Before Phase 2**: Only syntactic edges (CALLS, DEFINES, IMPORTS, INHERITS).

**After Phase 2**: New semantic relationship types:
- **TRANSFORMS**: Data transformation (parser → AST, JSON → object)
- **VALIDATES**: Input/state validation
- **ORCHESTRATES**: Coordinates multiple operations
- **PERSISTS**: Saves to storage/database
- **RETRIEVES**: Loads from storage/database
- **CONFIGURES**: System configuration/setup
- **HANDLES**: Error/exception handling
- **DEPENDS_ON**: Logical dependency
- **PRODUCES**: Creates/generates output
- **CONSUMES**: Uses/processes input

### 3. Context-Aware Analysis

Functions are analyzed with full context:
- Repository context from Phase 1 (project type, domain, architecture)
- Call graph neighbors (what it calls, what calls it)
- File location and module structure
- Tech stack and patterns

## Files Modified

### 1. `backend/app/models.py`

Added semantic models:

```python
# Semantic edge types
SemanticEdgeType = Literal[
    "TRANSFORMS", "VALIDATES", "ORCHESTRATES", "PERSISTS",
    "RETRIEVES", "CONFIGURES", "HANDLES", "DEPENDS_ON",
    "PRODUCES", "CONSUMES"
]

# Semantic metadata
class SemanticMetadata(BaseModel):
    intent: Literal["orchestration", "transformation", ...]
    complexity: Literal["low", "medium", "high", "very_high"]
    criticality: Literal["low", "medium", "high", "critical"]
    patterns: List[str]
    side_effects: List[Literal["file_io", "network", ...]]
    data_flow: Optional[Dict[str, Any]]
    confidence: float

# Semantic edge
class SemanticEdge(BaseModel):
    type: SemanticEdgeType
    source_id: str
    target_id: str
    confidence: float
    description: Optional[str]
    inferred_by: Literal["bob", "ast", "heuristic"]

# Enhanced function node
class FunctionNode(BaseModel):
    # ... existing fields ...
    semantics: Optional[SemanticMetadata] = None  # NEW

# Enhanced graph payload
class GraphPayload(BaseModel):
    # ... existing fields ...
    semantic_edges: List[SemanticEdge] = []  # NEW
```

### 2. `backend/app/bob_client.py`

Added two new functions:

**`_heuristic_semantic_analysis(name, code, repo_context) -> dict`**
- Fallback analysis without LLM
- Infers intent from function name patterns
- Estimates complexity from code length
- Detects side effects from code patterns
- Returns medium-confidence (0.4) metadata

**`analyze_function_semantics(name, code, repo_context, neighbors) -> dict`**
- Main entry point for semantic analysis
- Uses heuristic baseline
- Enhances with Bob API if available
- Returns high-confidence (0.8) metadata with Bob
- Gracefully falls back on errors

### 3. `backend/app/pipeline.py`

Added semantic analysis to pipeline:

```python
def _analyze_semantics(
    nodes: list[FunctionNode],
    g: nx.DiGraph,
    repo_context: RepositoryContext
) -> list[SemanticEdge]:
    """Phase 2: Perform semantic analysis on function nodes."""
    # Analyze each function with context
    # Extract semantic edges
    # Return discovered relationships

def ingest(repo_path: str) -> GraphPayload:
    # Phase 1: Repository analysis
    repo_context = bob_client.analyze_repository(repo_path)
    
    # Build function graph
    g, function_nodes, function_edges = graph_builder.build_function_graph(repo_path)
    
    # Phase 2: Semantic analysis (NEW)
    semantic_edges = _analyze_semantics(function_nodes, g, repo_context)
    
    # Push semantics to graph for module clustering
    for n in function_nodes:
        if n.semantics:
            g.nodes[n.id]["intent"] = n.semantics.intent
            g.nodes[n.id]["complexity"] = n.semantics.complexity
            g.nodes[n.id]["criticality"] = n.semantics.criticality
    
    # Create payload with semantic data
    payload = GraphPayload(
        # ... existing fields ...
        semantic_edges=semantic_edges,  # NEW
    )
```

### 4. `backend/app/main.py`

Added new API endpoint:

```python
@app.get("/semantic_analysis")
def get_semantic_analysis(graph_id: str):
    """Get semantic analysis results for a graph."""
    # Returns:
    # - functions_analyzed: count
    # - semantic_edges_count: count
    # - functions: list with semantic metadata
    # - semantic_edges: list of semantic relationships
```

## Usage Examples

### Example 1: Ingest with Semantic Analysis

```bash
# Ingest a repository
curl -X POST http://localhost:8000/ingest_repo \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/path/to/repo"}'

# Console output shows both phases:
[Phase 1] Analyzing repository: /path/to/repo
  - Project type: web_api
  - Domain: code_analysis
  ...
[Phase 2] Performing semantic analysis on 42 functions...
  - Analyzed 10/42 functions
  - Analyzed 20/42 functions
  ...
  - Semantic analysis complete: 42 functions, 15 semantic edges
```

### Example 2: Retrieve Semantic Analysis

```bash
# Get semantic analysis for a graph
curl http://localhost:8000/semantic_analysis?graph_id=abc123

# Response:
{
  "graph_id": "abc123",
  "functions_analyzed": 42,
  "semantic_edges_count": 15,
  "functions": [
    {
      "id": "pipeline.py::ingest",
      "name": "ingest",
      "file_path": "backend/app/pipeline.py",
      "intent": "orchestration",
      "complexity": "medium",
      "criticality": "high",
      "patterns": ["pipeline", "facade"],
      "side_effects": ["file_io"],
      "confidence": 0.8
    },
    {
      "id": "storage.py::save",
      "name": "save",
      "file_path": "backend/app/storage.py",
      "intent": "persistence",
      "complexity": "low",
      "criticality": "high",
      "patterns": [],
      "side_effects": ["file_io"],
      "confidence": 0.8
    }
  ],
  "semantic_edges": [
    {
      "type": "ORCHESTRATES",
      "source_id": "pipeline.py::ingest",
      "target_id": "graph_builder.py::build_function_graph",
      "confidence": 0.8,
      "description": "Coordinates graph building process",
      "inferred_by": "bob"
    },
    {
      "type": "PERSISTS",
      "source_id": "pipeline.py::ingest",
      "target_id": "storage.py::save",
      "confidence": 0.8,
      "description": "Saves graph payload to storage",
      "inferred_by": "bob"
    }
  ]
}
```

### Example 3: Heuristic Mode (No Bob API)

```bash
# Without BOB_API_KEY, heuristic analysis still works
unset BOB_API_KEY

curl -X POST http://localhost:8000/ingest_repo \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/path/to/repo"}'

# Functions get semantic metadata with lower confidence:
{
  "intent": "persistence",      # Inferred from "save" in name
  "complexity": "low",           # Inferred from code length
  "criticality": "medium",       # Default
  "patterns": [],                # Limited pattern detection
  "side_effects": ["file_io"],   # Detected from code patterns
  "confidence": 0.4              # Lower confidence
}
```

## Semantic Intent Classification

### Intent Types and Detection

| Intent | Detected From | Example Functions |
|--------|---------------|-------------------|
| **orchestration** | "orchestrate", "coordinate", "manage", "run" | `ingest()`, `update()`, `execute_pipeline()` |
| **transformation** | "transform", "convert", "parse", "build" | `parse_file()`, `build_graph()`, `convert_to_json()` |
| **validation** | "validate", "check", "verify", "ensure" | `validate_input()`, `check_permissions()` |
| **persistence** | "save", "store", "write", "persist" | `save()`, `store_meta()`, `write_to_file()` |
| **retrieval** | "load", "get", "fetch", "read", "retrieve" | `load()`, `get_graph()`, `fetch_data()` |
| **configuration** | "config", "setup", "init", "configure" | `setup_logging()`, `init_database()` |
| **error_handling** | "handle", "catch", "error", "exception" | `handle_error()`, `catch_exception()` |
| **computation** | "compute", "calculate", "sum", "count" | `calculate_total()`, `compute_score()` |
| **presentation** | "render", "display", "show", "format" | `render_template()`, `format_output()` |

## Complexity Assessment

| Level | Criteria | Typical LOC |
|-------|----------|-------------|
| **low** | Simple, single responsibility | < 20 lines |
| **medium** | Moderate logic, few branches | 20-50 lines |
| **high** | Complex logic, many branches | 50-100 lines |
| **very_high** | Very complex, nested logic | > 100 lines |

## Criticality Assessment

| Level | Criteria | Examples |
|-------|----------|----------|
| **low** | Helper/utility functions | `format_string()`, `temp_file()` |
| **medium** | Standard business logic | `process_data()`, `validate_input()` |
| **high** | Core functionality, entry points | `ingest()`, `main()`, `api_handler()` |
| **critical** | System-critical, security-sensitive | `authenticate()`, `authorize()`, `encrypt()` |

## Benefits

### Immediate Benefits

1. **Better Understanding**: Know what each function does conceptually
2. **Complexity Insights**: Identify complex functions that need refactoring
3. **Criticality Mapping**: Focus on high-criticality functions
4. **Pattern Recognition**: Discover design patterns in use
5. **Side Effect Tracking**: Understand external dependencies

### Future Benefits (Phase 3+)

1. **Smart Query Routing**: Route queries based on intent
2. **Complexity-Based Explanations**: Tailor explanations to complexity
3. **Criticality-Weighted Retrieval**: Prioritize critical functions
4. **Pattern-Based Recommendations**: Suggest similar patterns
5. **Side Effect Analysis**: Track data flow and dependencies

## Performance

- **Heuristic analysis**: ~10-50ms per function (pattern matching)
- **Bob API analysis**: ~500-1500ms per function (LLM call)
- **Cached Bob response**: ~10ms per function (disk read)
- **Overall impact**: Adds ~20-30% to total ingestion time (with caching)

## Storage

Semantic data is stored in the main graph payload:

```
_store/
├── abc123.json                    # Includes semantic_edges
│   ├── function_nodes[]           # Each has semantics field
│   └── semantic_edges[]           # NEW: Semantic relationships
├── abc123.repo_context.json       # Phase 1 data
└── abc123.git.json                # Git metadata
```

## Troubleshooting

### Issue: No semantic metadata on functions
**Cause**: Graph created before Phase 2 implementation.
**Solution**: Re-ingest the repository.

### Issue: Low confidence scores (0.4)
**Cause**: Using heuristic fallback without Bob API.
**Solution**: Set `BOB_API_KEY` for higher confidence analysis.

### Issue: Missing semantic edges
**Cause**: Bob couldn't infer relationships or target functions not found.
**Solution**: Normal - not all functions have semantic relationships.

### Issue: Slow ingestion
**Cause**: Bob API calls for each function.
**Solution**: 
1. Responses are cached - subsequent runs are fast
2. Consider batching (future optimization)
3. Use heuristic mode for quick analysis

## Next Steps

Phase 2 is complete! Ready for:

**Phase 3: Continuous Learning**
- Track which functions are queried most
- Learn from user feedback
- Improve semantic analysis over time
- Build domain-specific terminology maps

## Testing

### Test Heuristic Semantic Analysis

```python
from backend.app import bob_client

# Test on a simple function
code = """
def save_data(data, filename):
    with open(filename, 'w') as f:
        json.dump(data, f)
"""

result = bob_client._heuristic_semantic_analysis(
    "save_data",
    code,
    {"project_type": "web_api", "domain": "general"}
)

print(result)
# Expected:
# {
#   'intent': 'persistence',      # From "save" in name
#   'complexity': 'low',           # Short code
#   'criticality': 'medium',       # Default
#   'patterns': [],
#   'side_effects': ['file_io'],   # Detected from 'open('
#   'confidence': 0.4
# }
```

### Test Full Semantic Analysis

```python
from backend.app import pipeline

# Ingest with semantic analysis
payload = pipeline.ingest('.')

# Check semantic metadata
for node in payload.function_nodes[:5]:
    if node.semantics:
        print(f"{node.name}:")
        print(f"  Intent: {node.semantics.intent}")
        print(f"  Complexity: {node.semantics.complexity}")
        print(f"  Criticality: {node.semantics.criticality}")
        print(f"  Confidence: {node.semantics.confidence}")

# Check semantic edges
print(f"\nSemantic edges: {len(payload.semantic_edges)}")
for edge in payload.semantic_edges[:5]:
    print(f"  {edge.type}: {edge.source_id} -> {edge.target_id}")
```

## Conclusion

Phase 2 successfully adds semantic understanding to Flowify! Functions now have rich metadata about their intent, complexity, and relationships, enabling more intelligent analysis and visualization.