"""Backward-compatibility shim — all logic now lives in llm_provider.py."""
from .llm_provider import (  # noqa: F401
    ask,
    ask_json,
    summarize_function,
    summarize_module,
    explain_flow,
    interpret_query,
    analyze_repository,
    analyze_function_semantics,
    get_provider,
)
