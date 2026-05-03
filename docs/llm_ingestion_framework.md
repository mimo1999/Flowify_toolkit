# LLM Ingestion Framework

## Purpose

This framework lets Flowify keep deterministic AST-based parsing as the source of truth, then pass those parsed results into an LLM that returns a richer node-level JSON layer.

The LLM does not replace AST parsing. It consumes AST output and module ground truth, then produces semantic node records that are easier to use in UX, retrieval, and downstream analysis.

## Inputs

The LLM ingestion stage receives:

- repository context from Phase 1
- AST-derived nodes and edges from `graph_builder`
- semantic hints from Phase 2
- ground-truth module assignments from `module_abstractor`

## Output JSON

The LLM returns a JSON object with:

- `prompt_version`
- `nodes`
- `notes`

Each node contains:

- `node_id`
- `name`
- `file_path`
- `node_type`
- `module_id`
- `module_name`
- `role`
- `responsibilities`
- `inputs`
- `outputs`
- `dependencies`
- `side_effects`
- `summary`
- `confidence`

## Prompt Design

The prompt is designed around three rules:

1. AST structure is authoritative.
2. Ground-truth modules are authoritative for grouping.
3. The LLM may enrich meaning, but must not invent or delete nodes.

Prompt responsibilities:

- preserve node identity
- preserve module assignment
- infer role and responsibilities
- normalize inputs, outputs, and dependencies
- provide a concise summary and confidence

## Storage

The backend stores:

- `<graph_id>.llm_ingestion.json`
- `<graph_id>.llm_ingestion_prompt.json`

This makes the generated node layer inspectable and debuggable.

## Endpoints

- `GET /llm_ingestion?graph_id=...`
- `GET /llm_ingestion_prompt?graph_id=...`

## Notes

If the LLM does not return valid JSON, the backend falls back to a conservative node-level result built from existing summaries and semantic metadata.
