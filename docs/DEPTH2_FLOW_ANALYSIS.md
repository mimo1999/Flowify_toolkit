# Depth 2 Flow Edge Analysis and Verification

## Problem Statement

The depth 2 drill-down flow currently includes unrelated function calls in the FLOW edges between file-level submodules. This happens because the aggregation logic in [`module_abstractor.py:collapse_for_depth()`](../backend/app/module_abstractor.py:597-636) creates FLOW edges for ALL function calls between files, without verifying:

1. Whether there's an actual import relationship
2. Whether the calls are direct or transitive
3. Whether the calls are part of identified control flow patterns

## Current Implementation Issues

### Location: `module_abstractor.py` lines 622-635

```python
# Aggregate CALLS into submodule→submodule FLOW edges.
sub_pair_counts: Dict[Tuple[str, str], int] = defaultdict(int)
for e in function_edges:
    if not _is_invocation(e):
        continue
    s = sub_id_for.get(e["source_id"])
    t = sub_id_for.get(e["target_id"])
    if s and t and s != t:
        sub_pair_counts[(s, t)] += 1
for (s, t), _ in sub_pair_counts.items():
    edges_out.append({
        "id": f"flow:{s}->{t}",
        "source": s, "target": t, "kind": "FLOW",
    })
```

**Problems:**
- No verification that source file imports target file
- No check if calls are direct vs transitive dependencies
- No filtering based on control flow patterns
- Counts all invocations equally, regardless of context

## Verification Strategy

### New Module: `flow_verifier.py`

The new [`flow_verifier.py`](../backend/app/flow_verifier.py) module provides utilities to verify FLOW edges:

#### 1. Import Relationship Verification

```python
def extract_file_imports(
    function_nodes_by_id: Dict[str, dict],
    function_edges: List[dict]
) -> Dict[str, Set[str]]:
```

Extracts which files import from which other files by looking for `DEPENDS_ON` or `IMPORTS` edge types.

#### 2. Direct Call Extraction

```python
def extract_direct_calls(
    function_nodes_by_id: Dict[str, dict],
    function_edges: List[dict]
) -> Dict[Tuple[str, str], List[Tuple[str, str]]]:
```

Extracts direct function calls between files, returning the specific function pairs involved.

#### 3. Flow Edge Verification

```python
def verify_flow_edge(
    source_file: str,
    target_file: str,
    file_imports: Dict[str, Set[str]],
    file_calls: Dict[Tuple[str, str], List[Tuple[str, str]]],
    require_import: bool = True
) -> Tuple[bool, str, List[Tuple[str, str]]]:
```

Verifies if a FLOW edge is valid by checking:
- Direct function calls exist
- Import relationship exists (optional, since not all languages track imports)
- Returns the specific function pairs for transparency

#### 4. Batch Filtering

```python
def filter_verified_flow_edges(
    submodule_pairs: List[Tuple[str, str]],
    function_nodes_by_id: Dict[str, dict],
    function_edges: List[dict],
    require_import: bool = False
) -> Dict[Tuple[str, str], dict]:
```

Filters a list of submodule pairs to only include verified relationships with metadata.

## Recommended Fix

### Option 1: Strict Verification (Recommended for Python projects)

Modify `collapse_for_depth()` at depth 2 to use verification:

```python
if depth == 2:
    # ... existing submodule creation code ...
    
    # Import the verifier
    from . import flow_verifier
    
    # Collect candidate pairs
    candidate_pairs = []
    for e in function_edges:
        if not _is_invocation(e):
            continue
        s = sub_id_for.get(e["source_id"])
        t = sub_id_for.get(e["target_id"])
        if s and t and s != t:
            candidate_pairs.append((s, t))
    
    # Verify and filter
    verified = flow_verifier.filter_verified_flow_edges(
        list(set(candidate_pairs)),  # Deduplicate
        function_nodes_by_id,
        function_edges,
        require_import=False  # Set to True for stricter verification
    )
    
    # Create edges only for verified pairs
    for (s, t), metadata in verified.items():
        edges_out.append({
            "id": f"flow:{s}->{t}",
            "source": s,
            "target": t,
            "kind": "FLOW",
            "call_count": metadata["call_count"],
            "verified": True,
        })
```

### Option 2: Hybrid Approach (Recommended for multi-language projects)

Keep existing aggregation but add verification metadata:

```python
if depth == 2:
    # ... existing code ...
    
    from . import flow_verifier
    
    # Build verification data
    file_imports = flow_verifier.extract_file_imports(function_nodes_by_id, function_edges)
    file_calls = flow_verifier.extract_direct_calls(function_nodes_by_id, function_edges)
    
    # Aggregate with verification
    for (s, t), count in sub_pair_counts.items():
        # Extract file paths
        source_file = s.split("::", 1)[1] if "::" in s else ""
        target_file = t.split("::", 1)[1] if "::" in t else ""
        
        # Verify
        is_valid, reason, calls = flow_verifier.verify_flow_edge(
            source_file, target_file, file_imports, file_calls, require_import=False
        )
        
        edges_out.append({
            "id": f"flow:{s}->{t}",
            "source": s,
            "target": t,
            "kind": "FLOW",
            "call_count": count,
            "verified": is_valid,
            "verification_reason": reason,
            "direct_call_count": len(calls),
        })
```

## Testing the Fix

### 1. Inspect Current Edges

Use the `get_flow_edge_details()` function to inspect specific edges:

```python
from backend.app import flow_verifier

details = flow_verifier.get_flow_edge_details(
    "mod_abc123::src/main.py",
    "mod_abc123::src/utils.py",
    function_nodes_by_id,
    function_edges
)

print(f"Has import: {details['has_import']}")
print(f"Direct calls: {details['call_count']}")
for call in details['call_details']:
    print(f"  {call['source_function']} -> {call['target_function']}")
```

### 2. Compare Before/After

Run the graph generation with and without verification to see the difference:

```python
# Before: Count all edges
unverified_count = len([e for e in edges_out if e['kind'] == 'FLOW'])

# After: Count verified edges
verified_edges = flow_verifier.filter_verified_flow_edges(...)
verified_count = len(verified_edges)

print(f"Unverified edges: {unverified_count}")
print(f"Verified edges: {verified_count}")
print(f"Filtered out: {unverified_count - verified_count}")
```

## Benefits

1. **Accuracy**: Only show FLOW edges that represent actual code dependencies
2. **Clarity**: Users see the real structure, not transitive noise
3. **Debugging**: Metadata shows why edges were included/excluded
4. **Flexibility**: Can adjust `require_import` based on language/project needs
5. **Transparency**: Function pairs are available for detailed inspection

## Implementation Priority

**High Priority** - This directly impacts the usability of depth 2 views, which are critical for understanding module structure without overwhelming detail.

## Related Files

- [`backend/app/module_abstractor.py`](../backend/app/module_abstractor.py) - Main abstraction logic
- [`backend/app/flow_verifier.py`](../backend/app/flow_verifier.py) - New verification utilities
- [`backend/app/control_flow_analyzer.py`](../backend/app/control_flow_analyzer.py) - Control flow pattern detection