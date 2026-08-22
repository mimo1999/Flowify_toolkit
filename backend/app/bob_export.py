"""Bob-facing graph export helpers.

This module provides one JSON shape shared by the HTTP endpoint and CLI/MCP
wrappers. Bob can pass a repository root and receive a generated CIR graph in
the same request.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from . import module_abstractor, pipeline, storage
from .models import GraphPayload


BOB_GRAPH_SCHEMA_VERSION = "bob.graph.v1"


def _collapsed_view(payload: GraphPayload, depth: int) -> dict:
    depth = max(1, min(3, depth))
    function_nodes_by_id = {node.id: node.model_dump() for node in payload.function_nodes}
    function_edges = [edge.model_dump() for edge in payload.function_edges]
    view = module_abstractor.collapse_for_depth(
        payload.module_nodes,
        payload.module_edges,
        payload.module_to_functions,
        function_nodes_by_id,
        depth,
        function_edges,
    )
    view["graph_id"] = payload.graph_id
    view["repo_path"] = payload.repo_path
    return view


def build_bob_graph_response(
    repo_path: str,
    *,
    depth: int = 3,
    include_full_graph: bool = True,
    include_view: bool = True,
    include_llm_ingestion: bool = True,
    max_nodes: int | None = None,
) -> Dict[str, Any]:
    """Ingest `repo_path` and return a Bob-ready graph export.

    max_nodes: forwarded to pipeline.ingest() — see its docstring and
    pipeline.RepoTooLargeError. Unset for local/MCP use; main.py passes it
    in server mode.
    """
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"repo_path must be an existing directory: {repo_path}")

    payload = pipeline.ingest(str(root), max_nodes=max_nodes)
    repo_context = storage.load_meta(payload.graph_id, "repo_context") or {}
    llm_ingestion = storage.load_meta(payload.graph_id, "llm_ingestion") if include_llm_ingestion else None

    response: Dict[str, Any] = {
        "schema_version": BOB_GRAPH_SCHEMA_VERSION,
        "graph_id": payload.graph_id,
        "repo_path": payload.repo_path,
        "cir_version": payload.cir_version,
        "stats": {
            "node_count": len(payload.function_nodes),
            "edge_count": len(payload.function_edges),
            "module_count": len(payload.module_nodes),
            "semantic_edge_count": len(payload.semantic_edges),
        },
        "repo_context": repo_context,
    }

    if include_view:
        response["view"] = _collapsed_view(payload, depth)
    if include_full_graph:
        response["graph"] = payload.model_dump()
    if llm_ingestion is not None:
        response["llm_ingestion"] = llm_ingestion

    return response
