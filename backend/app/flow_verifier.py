"""Flow edge verification using imports and direct function calls.

This module provides utilities to verify that FLOW edges at depth 2 are based on
actual code imports and direct function calls, not just transitive dependencies.
"""
from __future__ import annotations
from typing import Any, Dict, List, Set, Tuple
from collections import defaultdict


def _is_invocation(edge: dict) -> bool:
    """Check if edge represents a function invocation."""
    return edge.get("relationship") == "INVOKES" or edge.get("type") == "CALLS"


def _is_import(edge: dict) -> bool:
    """Check if edge represents an import relationship."""
    return edge.get("relationship") == "DEPENDS_ON" or edge.get("type") == "IMPORTS"


def extract_file_imports(
    function_nodes_by_id: Dict[str, dict],
    function_edges: List[dict]
) -> Dict[str, Set[str]]:
    """Extract which files import from which other files.
    
    Returns: Dict mapping source_file -> set of imported files
    """
    file_imports: Dict[str, Set[str]] = defaultdict(set)
    
    # Build function to file mapping
    func_to_file = {
        fid: node.get("file_path", "")
        for fid, node in function_nodes_by_id.items()
    }
    
    # Extract import relationships
    for edge in function_edges:
        if _is_import(edge):
            source_id = edge.get("source_id")
            target_id = edge.get("target_id")
            if source_id and target_id:
                source_file = func_to_file.get(source_id)
                target_file = func_to_file.get(target_id)
                if source_file and target_file and source_file != target_file:
                    file_imports[source_file].add(target_file)
    
    return file_imports


def extract_direct_calls(
    function_nodes_by_id: Dict[str, dict],
    function_edges: List[dict]
) -> Dict[Tuple[str, str], List[Tuple[str, str]]]:
    """Extract direct function calls between files.
    
    Returns: Dict mapping (source_file, target_file) -> list of (source_func, target_func) pairs
    """
    file_calls: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)
    
    # Build function to file mapping
    func_to_file = {
        fid: node.get("file_path", "")
        for fid, node in function_nodes_by_id.items()
    }
    
    # Extract call relationships
    for edge in function_edges:
        if _is_invocation(edge):
            source_id = edge.get("source_id")
            target_id = edge.get("target_id")
            
            if source_id and target_id:
                source_file = func_to_file.get(source_id)
                target_file = func_to_file.get(target_id)
                
                if source_file and target_file and source_file != target_file:
                    file_calls[(source_file, target_file)].append((source_id, target_id))
    
    return file_calls


def verify_flow_edge(
    source_file: str,
    target_file: str,
    file_imports: Dict[str, Set[str]],
    file_calls: Dict[Tuple[str, str], List[Tuple[str, str]]],
    require_import: bool = True
) -> Tuple[bool, str, List[Tuple[str, str]]]:
    """Verify if a FLOW edge between two files is valid.
    
    Args:
        source_file: Source file path
        target_file: Target file path
        file_imports: Import relationships between files
        file_calls: Direct call relationships between files
        require_import: If True, require an import relationship
    
    Returns:
        Tuple of (is_valid, reason, list of (source_func, target_func) calls)
    """
    # Check if there are direct calls
    calls = file_calls.get((source_file, target_file), [])
    if not calls:
        return False, "No direct function calls found", []
    
    # Check if there's an import relationship (if required)
    if require_import:
        has_import = target_file in file_imports.get(source_file, set())
        if not has_import:
            return False, f"No import relationship found (has {len(calls)} calls but no import)", calls
    
    return True, f"Valid: {len(calls)} direct calls" + (" with import" if require_import else ""), calls


def filter_verified_flow_edges(
    submodule_pairs: List[Tuple[str, str]],
    function_nodes_by_id: Dict[str, dict],
    function_edges: List[dict],
    require_import: bool = False  # Set to False by default since not all languages track imports
) -> Dict[Tuple[str, str], dict]:
    """Filter submodule FLOW edges to only include verified relationships.
    
    Args:
        submodule_pairs: List of (source_submodule_id, target_submodule_id) pairs
        function_nodes_by_id: Function node lookup
        function_edges: All function edges
        require_import: Whether to require import relationships
    
    Returns:
        Dict mapping verified pairs to metadata about the relationship
    """
    # Extract file paths from submodule IDs (format: "mod_xxx::path/to/file.py")
    def get_file_path(submodule_id: str) -> str:
        if "::" in submodule_id:
            return submodule_id.split("::", 1)[1]
        return ""
    
    # Build verification data
    file_imports = extract_file_imports(function_nodes_by_id, function_edges)
    file_calls = extract_direct_calls(function_nodes_by_id, function_edges)
    
    # Verify each pair
    verified_edges = {}
    for source_sub, target_sub in submodule_pairs:
        source_file = get_file_path(source_sub)
        target_file = get_file_path(target_sub)
        
        if not source_file or not target_file:
            continue
        
        is_valid, reason, calls = verify_flow_edge(
            source_file, target_file, file_imports, file_calls, require_import
        )
        
        if is_valid:
            verified_edges[(source_sub, target_sub)] = {
                "call_count": len(calls),
                "reason": reason,
                "function_pairs": calls[:10],  # Limit to first 10 for metadata
            }
    
    return verified_edges


def get_flow_edge_details(
    source_submodule: str,
    target_submodule: str,
    function_nodes_by_id: Dict[str, dict],
    function_edges: List[dict]
) -> Dict[str, Any]:
    """Get detailed information about a FLOW edge for debugging.
    
    Returns dict with:
        - has_import: bool
        - direct_calls: list of (source_func, target_func) tuples
        - call_details: list of dicts with function names and summaries
    """
    def get_file_path(submodule_id: str) -> str:
        if "::" in submodule_id:
            return submodule_id.split("::", 1)[1]
        return ""
    
    source_file = get_file_path(source_submodule)
    target_file = get_file_path(target_submodule)
    
    file_imports = extract_file_imports(function_nodes_by_id, function_edges)
    file_calls = extract_direct_calls(function_nodes_by_id, function_edges)
    
    has_import = target_file in file_imports.get(source_file, set())
    direct_calls = file_calls.get((source_file, target_file), [])
    
    call_details = []
    for source_id, target_id in direct_calls:
        source_node = function_nodes_by_id.get(source_id, {})
        target_node = function_nodes_by_id.get(target_id, {})
        call_details.append({
            "source_function": source_node.get("name", source_id),
            "target_function": target_node.get("name", target_id),
            "source_summary": source_node.get("summary", ""),
            "target_summary": target_node.get("summary", ""),
        })
    
    return {
        "source_file": source_file,
        "target_file": target_file,
        "has_import": has_import,
        "direct_calls": direct_calls,
        "call_count": len(direct_calls),
        "call_details": call_details,
    }

# Made with Bob
