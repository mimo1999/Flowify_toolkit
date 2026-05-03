# Enhanced Drill-Down and Module Organization

## Overview

This document describes the enhanced drill-down functionality that addresses the issues with ambiguous function listings and improves the graph visualization hierarchy.

## Key Improvements

### 1. Structured Module/Submodule Hierarchy with Entry Point Tagging

**Problem Solved:** The previous implementation showed all functions in a flat list after the initial entry call, making it difficult to understand the system's structure.

**Solution:** 
- Modules now have explicit entry point tagging via `is_entry_point` flag
- Entry functions are tracked in `entry_functions` list
- Modules are classified by type: `entry`, `core`, `utility`, or `control_flow`

**API Changes:**
```python
# ModuleNode now includes:
class ModuleNode(BaseModel):
    is_entry_point: bool  # True if module contains entry points
    entry_functions: List[str]  # IDs of entry point functions
    submodule_type: Optional[Literal["entry", "core", "utility", "control_flow"]]
    control_flow_groups: Dict[str, List[str]]  # Control flow pattern groupings
```

### 2. Control Flow Abstraction

**Problem Solved:** Multiple calls inside if-else clauses were shown in sequence, creating confusion about execution flow.

**Solution:**
- New `control_flow_analyzer` module analyzes AST to identify control flow patterns
- Functions are grouped by pattern type:
  - `conditional_branch`: Functions in if-else branches
  - `error_handling`: Exception handling functions
  - `try_block`: Functions in try blocks
  - `loop`: Functions in loops

**Usage:**
```python
from backend.app import control_flow_analyzer

# Analyze control flow for a module
cf_groups = control_flow_analyzer.group_control_flow_functions(
    function_nodes, function_edges, module_function_ids
)

# Create submodule representations
submodules = control_flow_analyzer.create_control_flow_submodules(
    cf_groups, function_nodes_by_id
)
```

### 3. Default Graph Shows High-Level Structure Only

**Problem Solved:** The default view showed all functions, overwhelming users.

**Solution:**
- Depth 1 (default): Shows only modules with metadata
  - Entry point indicators
  - Control flow pattern summaries
  - Function counts
- Depth 2: Shows file-level submodules
- Depth 3: Shows individual functions

**API Endpoint:**
```
GET /graph?graph_id={id}&depth=1
```

Response includes:
```json
{
  "nodes": [
    {
      "id": "mod_abc123",
      "label": "Authentication Module",
      "kind": "module",
      "is_entry_point": true,
      "submodule_type": "entry",
      "control_flow_patterns": ["conditional_branch", "error_handling"],
      "control_flow_summary": {
        "conditional_branch": 5,
        "error_handling": 3
      }
    }
  ],
  "metadata": {
    "total_modules": 8,
    "entry_point_modules": 2,
    "modules_with_control_flow": 5
  }
}
```

### 4. Enhanced Module Details Endpoint

**New Endpoint:** `GET /module_details?graph_id={id}&module_id={module_id}`

Returns comprehensive module information:
```json
{
  "module": {
    "id": "mod_abc123",
    "name": "Authentication Module",
    "is_entry_point": true,
    "submodule_type": "entry",
    "function_count": 15,
    "entry_function_count": 2
  },
  "functions": [
    {
      "id": "auth.py::login",
      "name": "login",
      "type": "function",
      "is_entry_point": true,
      "summary": "Handles user authentication"
    }
  ],
  "control_flow_groups": [
    {
      "pattern_type": "conditional_branch",
      "description": "Functions called in if-else conditional branches",
      "function_count": 5,
      "functions": [...]
    }
  ]
}
```

## Implementation Details

### Module Building Process

The enhanced `build_modules()` function now:

1. **Identifies Entry Points:**
   - Uses declared entry points from repository analysis
   - Matches entry point files to functions
   - Tags functions with names like `main`, `run`, `start`, `train`

2. **Analyzes Control Flow:**
   - Parses Python AST for each function
   - Identifies if-else, try-except, and loop structures
   - Maps called functions to control flow patterns

3. **Classifies Modules:**
   - `entry`: Contains entry point functions
   - `core`: Contains orchestration logic
   - `control_flow`: Has significant control flow patterns
   - `utility`: Helper/utility functions

### Control Flow Analysis

The `control_flow_analyzer` module provides:

```python
def _analyze_function_control_flow(node: FunctionNode) -> Dict[str, List[str]]:
    """Analyze a function's code to identify control flow patterns."""
    # Returns: {"conditional_branch": ["func1", "func2"], ...}

def group_control_flow_functions(
    function_nodes: List[FunctionNode],
    function_edges: List[FunctionEdge],
    module_functions: List[str]
) -> Dict[str, Dict[str, List[str]]]:
    """Group functions within a module by control flow patterns."""
    # Returns: {func_id: {pattern_type: [called_func_ids]}}

def create_control_flow_submodules(
    control_flow_groups: Dict[str, Dict[str, List[str]]],
    function_nodes_by_id: Dict[str, FunctionNode]
) -> List[Dict[str, Any]]:
    """Create virtual submodule representations for control flow groups."""
    # Returns list of submodule descriptors with function tables
```

### Expand Node Enhancements

The `expand_node()` function now returns control flow groups:

```python
# When expanding a module, returns:
{
  "children": [
    # File-level submodules
    {"id": "mod_abc::file.py", "kind": "submodule", ...},
    # Control flow groups
    {
      "id": "mod_abc::cf::conditional_branch",
      "kind": "control_flow_group",
      "pattern_type": "conditional_branch",
      "function_ids": ["func1", "func2"],
      ...
    }
  ],
  "edges": [...]
}
```

## Migration Guide

### For Existing Code

1. **Update pipeline calls:**
```python
# Old:
module_nodes, module_edges, mod_to_funcs = module_abstractor.build_modules(g)

# New:
module_nodes, module_edges, mod_to_funcs = module_abstractor.build_modules(
    g,
    function_nodes=function_nodes,
    function_edges=function_edges,
    declared_entry_points=repo_context.key_entry_points,
)
```

2. **Handle new module fields:**
```python
for module in module_nodes:
    if module.is_entry_point:
        print(f"Entry module: {module.name}")
        print(f"Entry functions: {module.entry_functions}")
    
    if module.control_flow_groups:
        print(f"Control flow patterns: {list(module.control_flow_groups.keys())}")
```

### For Frontend/UI

1. **Display entry point indicators:**
   - Show badge/icon for modules with `is_entry_point=true`
   - Highlight entry functions in function lists

2. **Show control flow summaries:**
   - Display `control_flow_patterns` as tags
   - Show `control_flow_summary` counts

3. **Implement drill-down:**
   - Start at depth=1 (modules only)
   - Allow expansion to depth=2 (files) or depth=3 (functions)
   - Show control flow groups as expandable sections

## Benefits

1. **Clearer Structure:** Entry points are explicitly marked
2. **Better Organization:** Functions grouped by control flow patterns
3. **Reduced Clutter:** Default view shows high-level structure only
4. **Improved Understanding:** Control flow abstraction shows logical groupings
5. **Flexible Exploration:** Progressive drill-down from modules → files → functions

## Examples

### Example 1: Entry Point Module

```json
{
  "id": "mod_entry_001",
  "name": "Main Entry Point",
  "is_entry_point": true,
  "submodule_type": "entry",
  "entry_functions": ["main.py::main", "main.py::run"],
  "control_flow_groups": {
    "conditional_branch": ["validate_args", "setup_logging", "run_pipeline"]
  }
}
```

### Example 2: Control Flow Group

```json
{
  "pattern_type": "error_handling",
  "description": "Functions involved in error handling and exception management",
  "function_count": 4,
  "functions": [
    {"id": "errors.py::handle_error", "name": "handle_error"},
    {"id": "errors.py::log_exception", "name": "log_exception"},
    {"id": "errors.py::send_alert", "name": "send_alert"},
    {"id": "errors.py::cleanup", "name": "cleanup"}
  ]
}
```

## Future Enhancements

1. **Pattern Detection:** Identify more complex patterns (factory, strategy, etc.)
2. **Semantic Grouping:** Use LLM to suggest logical groupings beyond control flow
3. **Interactive Refinement:** Allow users to manually adjust groupings
4. **Performance Optimization:** Cache control flow analysis results
5. **Multi-language Support:** Extend control flow analysis to JavaScript, Java, etc.