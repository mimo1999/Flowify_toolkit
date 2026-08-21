"""Backward-compatibility shim — all logic now lives in llm_provider.py."""
from .llm_provider import (  # noqa: F401
    ask_json,
    summarize_module,
    explain_flow_with_graph,
    interpret_query,
    analyze_repository,
    analyze_function_semantics,
    analyze_functions_batch,
    heuristic_provider,
    get_provider,
)
