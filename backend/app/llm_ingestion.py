"""LLM ingestion layer over AST-derived graph results.

This module takes deterministic AST extraction results plus module ground truth,
packages them into a prompt, and validates the returned node-level JSON.
"""
from __future__ import annotations

import json
import textwrap
from typing import Dict, List

from . import bob_client
from .models import (
    FunctionEdge,
    FunctionNode,
    LLMIngestionNode,
    LLMIngestionResult,
    ModuleNode,
    RepositoryContext,
    SemanticEdge,
)


PROMPT_VERSION = "v1"
_MAX_CODE_CHARS = 600


def _ground_truth_modules(
    module_nodes: List[ModuleNode],
    function_nodes_by_id: Dict[str, FunctionNode],
) -> List[dict]:
    modules = []
    for module in module_nodes:
        members = []
        for fid in module.linked_function_ids[:30]:
            node = function_nodes_by_id.get(fid)
            if not node:
                continue
            members.append({
                "node_id": node.id,
                "name": node.name,
                "type": node.type,
                "file_path": node.file_path,
                "summary": node.summary or "",
            })
        modules.append({
            "module_id": module.id,
            "module_name": module.name,
            "description": module.description,
            "members": members,
        })
    return modules


def _ast_nodes_payload(
    function_nodes: List[FunctionNode],
    function_edges: List[FunctionEdge],
    semantic_edges: List[SemanticEdge],
    module_lookup: Dict[str, ModuleNode],
) -> List[dict]:
    outgoing: Dict[str, List[str]] = {}
    incoming: Dict[str, List[str]] = {}
    for edge in function_edges:
        outgoing.setdefault(edge.source_id, []).append(edge.target_id)
        incoming.setdefault(edge.target_id, []).append(edge.source_id)

    semantic_by_source: Dict[str, List[dict]] = {}
    for edge in semantic_edges:
        semantic_by_source.setdefault(edge.source_id, []).append({
            "type": edge.type,
            "target_id": edge.target_id,
            "description": edge.description or "",
            "confidence": edge.confidence,
        })

    payload = []
    for node in function_nodes:
        module = module_lookup.get(node.id)
        payload.append({
            "node_id": node.id,
            "name": node.name,
            "file_path": node.file_path,
            "node_type": node.type,
            "lineno": node.lineno,
            "summary": node.summary or "",
            "module_id": module.id if module else None,
            "module_name": module.name if module else None,
            "semantics": node.semantics.model_dump() if node.semantics else None,
            "outgoing_calls": outgoing.get(node.id, [])[:10],
            "incoming_calls": incoming.get(node.id, [])[:10],
            "semantic_edges": semantic_by_source.get(node.id, []),
            "code_snippet": (node.code_snippet or "")[:_MAX_CODE_CHARS],
        })
    return payload


def build_prompt(
    repo_context: RepositoryContext,
    function_nodes: List[FunctionNode],
    function_edges: List[FunctionEdge],
    module_nodes: List[ModuleNode],
    module_to_functions: Dict[str, List[str]],
    semantic_edges: List[SemanticEdge],
) -> str:
    """Build the prompt passed to the LLM for node-level ingestion."""
    function_nodes_by_id = {n.id: n for n in function_nodes}
    module_lookup: Dict[str, ModuleNode] = {}
    module_by_id = {m.id: m for m in module_nodes}
    for module_id, fids in module_to_functions.items():
        module = module_by_id.get(module_id)
        if not module:
            continue
        for fid in fids:
            module_lookup[fid] = module

    ast_nodes = _ast_nodes_payload(function_nodes, function_edges, semantic_edges, module_lookup)
    ground_truth_modules = _ground_truth_modules(module_nodes, function_nodes_by_id)

    return textwrap.dedent(f"""
        You are enriching AST-derived code-ingestion output for a repository graph.

        Your job:
        1. Read the AST-derived node data as the structural source of truth.
        2. Use the provided ground-truth modules as authoritative module assignments.
        3. Return a node-level JSON object that adds semantic meaning without changing node identity.

        Hard constraints:
        - Do not invent node ids.
        - Do not drop nodes.
        - Preserve the given `node_id`, `name`, `file_path`, and `node_type`.
        - Use module assignments from the ground-truth modules whenever present.
        - Return JSON only. No markdown. No prose outside JSON.

        Repository context:
        {repo_context.model_dump_json(indent=2)}

        Ground-truth modules:
        {json.dumps(ground_truth_modules, indent=2)}

        AST-derived nodes:
        {json.dumps(ast_nodes, indent=2)}

        Return exactly one JSON object with this schema:
        {{
          "prompt_version": "{PROMPT_VERSION}",
          "nodes": [
            {{
              "node_id": "string",
              "name": "string",
              "file_path": "string",
              "node_type": "function|class|method|file",
              "module_id": "string or null",
              "module_name": "string or null",
              "role": "short phrase describing the node's role in the system",
              "responsibilities": ["1-4 short bullets as strings"],
              "inputs": ["logical inputs, params, state, or upstream data"],
              "outputs": ["logical outputs, return values, emitted effects"],
              "dependencies": ["important collaborators by node name, module name, or subsystem"],
              "side_effects": ["observable effects such as file_io, network, database, logging, state_mutation, none"],
              "summary": "one concise sentence",
              "confidence": 0.0
            }}
          ],
          "notes": ["optional short notes about ambiguity"]
        }}

        Quality bar:
        - Favor precise software architecture language over generic summaries.
        - Use the code snippet, summaries, call edges, and semantic hints together.
        - If a node is ambiguous, keep the fields conservative and lower confidence.
    """).strip()


def _fallback_result(
    function_nodes: List[FunctionNode],
    module_to_functions: Dict[str, List[str]],
    module_nodes: List[ModuleNode],
) -> LLMIngestionResult:
    module_by_id = {m.id: m for m in module_nodes}
    node_to_module = {}
    for module_id, fids in module_to_functions.items():
        for fid in fids:
            node_to_module[fid] = module_id

    nodes = []
    for node in function_nodes:
        module_id = node_to_module.get(node.id)
        module = module_by_id.get(module_id) if module_id else None
        nodes.append(LLMIngestionNode(
            node_id=node.id,
            name=node.name,
            file_path=node.file_path,
            node_type=node.type,
            module_id=module_id,
            module_name=module.name if module else None,
            role=node.semantics.intent if node.semantics else "",
            responsibilities=[node.summary] if node.summary else [],
            side_effects=node.semantics.side_effects if node.semantics else [],
            summary=node.summary or "",
            confidence=node.semantics.confidence if node.semantics else 0.2,
        ))
    return LLMIngestionResult(
        prompt_version=PROMPT_VERSION,
        nodes=nodes,
        notes=["Fallback result used because no structured LLM response was available."],
    )


def ingest_ast_results(
    repo_context: RepositoryContext,
    function_nodes: List[FunctionNode],
    function_edges: List[FunctionEdge],
    module_nodes: List[ModuleNode],
    module_to_functions: Dict[str, List[str]],
    semantic_edges: List[SemanticEdge],
) -> tuple[LLMIngestionResult, str]:
    """Run the LLM ingestion stage and return validated JSON plus prompt."""
    prompt = build_prompt(
        repo_context,
        function_nodes,
        function_edges,
        module_nodes,
        module_to_functions,
        semantic_edges,
    )
    fallback = _fallback_result(function_nodes, module_to_functions, module_nodes).model_dump()
    raw = bob_client.ask_json(prompt, fallback)
    try:
        result = LLMIngestionResult(**raw)
    except Exception:
        result = _fallback_result(function_nodes, module_to_functions, module_nodes)
    return result, prompt
