"""Control flow analysis and abstraction for better graph organization.

This module analyzes function call patterns to identify control flow structures
like if-else branches, error handling paths, and validation chains, then groups
them into logical submodules for cleaner visualization.
"""
from __future__ import annotations
import ast
from typing import Any, Dict, List, Set, Tuple
from collections import defaultdict

from .models import FunctionNode, FunctionEdge


def _is_invocation(edge: dict) -> bool:
    return edge.get("relationship") == "INVOKES" or edge.get("type") == "CALLS"


def _analyze_function_control_flow(node: FunctionNode) -> Dict[str, List[str]]:
    """Analyze a function's code to identify control flow patterns.
    
    Returns dict mapping pattern type to list of called function names.
    """
    if not node.code_snippet or node.source_language != "python":
        return {}
    
    try:
        tree = ast.parse(node.code_snippet)
    except (SyntaxError, ValueError):
        return {}
    
    patterns = defaultdict(list)
    
    # Walk the AST to find control flow structures
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.If):
            # Extract function calls in if/else branches
            if_calls = _extract_calls_from_body(stmt.body)
            else_calls = _extract_calls_from_body(stmt.orelse)
            
            if if_calls or else_calls:
                patterns["conditional_branch"].extend(if_calls + else_calls)
        
        elif isinstance(stmt, ast.Try):
            # Error handling pattern
            try_calls = _extract_calls_from_body(stmt.body)
            except_calls = []
            for handler in stmt.handlers:
                except_calls.extend(_extract_calls_from_body(handler.body))
            
            if try_calls:
                patterns["try_block"].extend(try_calls)
            if except_calls:
                patterns["error_handling"].extend(except_calls)
        
        elif isinstance(stmt, (ast.For, ast.While)):
            # Loop pattern
            loop_calls = _extract_calls_from_body(stmt.body)
            if loop_calls:
                patterns["loop"].extend(loop_calls)
    
    return dict(patterns)


def _extract_calls_from_body(body: List[ast.stmt]) -> List[str]:
    """Extract function call names from a list of statements."""
    calls = []
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                name = _get_call_name(node.func)
                if name and name not in calls:
                    calls.append(name)
    return calls


def _get_call_name(func: ast.AST) -> str | None:
    """Extract the name from a function call node."""
    if isinstance(func, ast.Name):
        return func.id
    elif isinstance(func, ast.Attribute):
        return func.attr
    return None


def group_control_flow_functions(
    function_nodes: List[FunctionNode],
    function_edges: List[FunctionEdge],
    module_functions: List[str]
) -> Dict[str, Dict[str, List[str]]]:
    """Group functions within a module by control flow patterns.
    
    Returns dict mapping function_id to its control flow groups.
    Each group maps pattern type to list of called function IDs.
    """
    # Build lookup structures
    func_by_id = {n.id: n for n in function_nodes if n.id in module_functions}
    func_by_name = defaultdict(list)
    for fid in module_functions:
        node = func_by_id.get(fid)
        if node:
            func_by_name[node.name].append(fid)
    
    # Build call graph within module
    module_calls: Dict[str, Set[str]] = defaultdict(set)
    for edge in function_edges:
        if _is_invocation(edge.model_dump()) and edge.source_id in module_functions and edge.target_id in module_functions:
            module_calls[edge.source_id].add(edge.target_id)
    
    # Analyze each function's control flow
    result: Dict[str, Dict[str, List[str]]] = {}
    
    for fid, node in func_by_id.items():
        patterns = _analyze_function_control_flow(node)
        if not patterns:
            continue
        
        # Map pattern call names to actual function IDs in this module
        resolved_patterns: Dict[str, List[str]] = {}
        for pattern_type, call_names in patterns.items():
            resolved_ids = []
            for call_name in call_names:
                # Find matching function IDs
                for candidate_id in func_by_name.get(call_name, []):
                    if candidate_id in module_calls.get(fid, set()):
                        resolved_ids.append(candidate_id)
            
            if resolved_ids:
                resolved_patterns[pattern_type] = resolved_ids
        
        if resolved_patterns:
            result[fid] = resolved_patterns
    
    return result


def create_control_flow_submodules(
    control_flow_groups: Dict[str, Dict[str, List[str]]],
    function_nodes_by_id: Dict[str, FunctionNode]
) -> List[Dict[str, Any]]:
    """Create virtual submodule representations for control flow groups.
    
    Returns list of submodule descriptors for visualization.
    """
    submodules = []
    
    # Group by pattern type across all functions
    pattern_functions: Dict[str, Set[str]] = defaultdict(set)
    
    for func_id, patterns in control_flow_groups.items():
        for pattern_type, called_ids in patterns.items():
            pattern_functions[pattern_type].add(func_id)
            pattern_functions[pattern_type].update(called_ids)
    
    # Create submodule for each significant pattern
    for pattern_type, func_ids in pattern_functions.items():
        if len(func_ids) < 2:  # Skip trivial groups
            continue
        
        # Get function details for the table
        functions_table = []
        for fid in sorted(func_ids):
            node = function_nodes_by_id.get(fid)
            if node:
                functions_table.append({
                    "id": fid,
                    "name": node.name,
                    "type": node.type,
                    "file_path": node.file_path,
                    "summary": node.summary or "",
                })
        
        submodules.append({
            "pattern_type": pattern_type,
            "function_count": len(func_ids),
            "functions": functions_table,
            "description": _pattern_description(pattern_type),
        })
    
    return submodules


def _pattern_description(pattern_type: str) -> str:
    """Get human-readable description for a control flow pattern."""
    descriptions = {
        "conditional_branch": "Functions called in if-else conditional branches",
        "error_handling": "Functions involved in error handling and exception management",
        "try_block": "Functions called within try blocks",
        "loop": "Functions called within loops (for/while)",
    }
    return descriptions.get(pattern_type, f"Functions in {pattern_type} pattern")

# Made with Bob
