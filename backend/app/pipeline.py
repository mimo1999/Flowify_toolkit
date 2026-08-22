"""End-to-end ingestion pipeline (called from API endpoints)."""
from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Dict, List

import networkx as nx

from . import llm_provider as bob_client, storage, graph_builder, module_abstractor, git_updater, llm_ingestion, learning, knowledge, graph_analytics
from .models import (
    GraphPayload, FunctionNode, RepositoryContext, SemanticMetadata, SemanticEdge,
    FlowSummary, FlowStep, ModuleNode, ModuleEdge,
)


_CRITICALITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _is_callable(node: FunctionNode) -> bool:
    return node.kind in ("function", "callable") or node.type in ("function", "method")


_STUB_PREFIXES = ("(stub) ", "(stub)")

def _is_stub(s: str | None, node: "FunctionNode | None" = None) -> bool:
    """Return True if the summary is absent, a raw stub, or a heuristic placeholder.

    A node whose summary was produced by the heuristic provider is flagged via
    adapter_metadata["summary_source"] == "heuristic".  We treat those as stubs
    so they are overwritten when a real LLM becomes available.
    """
    if not s:
        return True
    if any(s.startswith(p) for p in _STUB_PREFIXES):
        return True
    if node is not None:
        return node.adapter_metadata.get("summary_source") == "heuristic"
    return False

# How many functions to pack into a single batched LLM call.  Larger = fewer
# calls (cheaper) but bigger prompts/responses; ~10 keeps each call well within
# the model's context while cutting call count ~10x.
_LLM_BATCH_SIZE = int(os.environ.get("FLOWIFY_LLM_BATCH", "10"))


def _is_trivial(n: FunctionNode) -> bool:
    """True if a function is small/simple enough that the free heuristic suffices.

    Tiny accessors, dunders (other than __init__), and short wrappers don't
    warrant a cloud call — name+code heuristics describe them just as well, and
    they're the bulk of most codebases.
    """
    code = n.code_snippet or ""
    name = n.name or ""
    if name.startswith("__") and name.endswith("__") and name != "__init__":
        return True
    body = [ln for ln in code.splitlines() if ln.strip()]
    return len(body) <= 6 or len(code) < 160


def _enrich_functions(
    nodes: list[FunctionNode],
    g: nx.DiGraph,
    repo_context: RepositoryContext,
) -> list[SemanticEdge]:
    """Phase 2 (combined): summary + semantics + semantic edges in ONE pass.

    Replaces the old two-pass design that made two LLM calls per function
    (summarize, then analyze).  Trivial functions are handled by free
    heuristics; the rest are batched ``_LLM_BATCH_SIZE`` per cloud call, and
    each call returns both the summary and the semantic analysis.  Net effect:
    cloud calls drop from ~2N to ~(non-trivial N / batch size).

    Returns the list of semantic edges discovered during analysis.
    """
    provider = bob_client.get_provider()
    is_real_llm = not getattr(provider, "_is_heuristic", False)
    heur = bob_client.heuristic_provider()
    ctx = repo_context.model_dump()
    name_to_id = {n.name: n.id for n in nodes}
    semantic_edges: list[SemanticEdge] = []

    callables = [n for n in nodes if _is_callable(n) and n.code_snippet]
    print(f"[Phase 2] Enriching {len(callables)} functions via "
          f"{'batched LLM' if is_real_llm else 'heuristics'}...")

    def neighbors_of(n: FunctionNode) -> list[str]:
        if n.id in g.nodes:
            return [g.nodes[nid].get("name", "") for nid in g.successors(n.id)][:5]
        return []

    def apply(n: FunctionNode, r: dict, source: str) -> None:
        summary = (r.get("summary") or "").strip()
        # Only overwrite a stub summary, so a real LLM summary is never clobbered
        # by a later heuristic pass.
        if summary and _is_stub(n.summary, n):
            n.summary = summary
            n.adapter_metadata["summary_source"] = source
        try:
            n.semantics = SemanticMetadata(
                intent=r.get("intent"),
                complexity=r.get("complexity"),
                criticality=r.get("criticality"),
                patterns=r.get("patterns", []),
                side_effects=r.get("side_effects", []),
                confidence=r.get("confidence", 0.5),
            )
        except Exception:
            n.semantics = SemanticMetadata(confidence=r.get("confidence", 0.0))
        conf = r.get("confidence", 0.5)
        for ed in r.get("semantic_edges", []) or []:
            tgt = name_to_id.get(ed.get("target_name", ""))
            if not tgt or tgt == n.id:
                continue
            try:
                semantic_edges.append(SemanticEdge(
                    type=ed.get("type", "DEPENDS_ON"),
                    source_id=n.id,
                    target_id=tgt,
                    description=ed.get("description"),
                    confidence=conf,
                    inferred_by="bob" if conf > 0.5 else "heuristic",
                ))
            except Exception:
                continue

    batch: list[dict] = []
    batch_nodes: list[FunctionNode] = []

    def flush() -> None:
        if not batch:
            return
        results = provider.analyze_functions_batch(batch, ctx)
        for bn, r in zip(batch_nodes, results):
            apply(bn, r, "llm")
        batch.clear()
        batch_nodes.clear()

    done = 0
    for n in callables:
        item = {
            "name": n.name,
            "code": n.code_snippet,
            "kind": n.kind or n.type or "function",
            "neighbors": neighbors_of(n),
        }
        if not is_real_llm or _is_trivial(n):
            apply(n, heur.analyze_functions_batch([item], ctx)[0], "heuristic")
        else:
            batch.append(item)
            batch_nodes.append(n)
            if len(batch) >= _LLM_BATCH_SIZE:
                flush()
        done += 1
        if done % 50 == 0:
            print(f"  - Enriched {done}/{len(callables)} functions")
    flush()

    print(f"  - Enrichment complete: {len(callables)} functions, "
          f"{len(semantic_edges)} semantic edges")
    return semantic_edges


# A flow summary is meant to be skimmable — cap how many modules appear in both
# the narrative and the diagram so neither becomes an unreadable wall.
_MAX_FLOW_MODULES = 18

_TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_|_test\.")


def _is_test_module(m: ModuleNode, func_by_id: Dict[str, FunctionNode]) -> bool:
    """True if a majority of a module's functions live in test files.

    Test code isn't part of the runtime data flow, so it shouldn't crowd out
    real modules in a high-level architecture view.
    """
    paths = [
        func_by_id[i].file_path
        for i in m.linked_function_ids
        if i in func_by_id and func_by_id[i].file_path
    ]
    if not paths:
        return False
    test_paths = sum(1 for p in paths if _TEST_PATH_RE.search(p))
    return test_paths > len(paths) / 2


def _select_significant_modules(
    module_nodes: List[ModuleNode],
    module_edges: List[ModuleEdge],
    func_by_id: Dict[str, FunctionNode],
    limit: int = _MAX_FLOW_MODULES,
) -> List[ModuleNode]:
    """Return the most significant runtime modules, BFS-ordered from entry points.

    Significance = entry points first, then by number of linked functions.  We
    seed the BFS frontier in significance order so the most important reachable
    modules survive the *limit* cap, then append the biggest leftovers.  Test
    modules are excluded unless there aren't enough runtime modules to fill the
    cap (they're appended last so the diagram stays focused on real flow).
    """
    by_id = {m.id: m for m in module_nodes}
    adjacency: Dict[str, List[str]] = {}
    for e in module_edges:
        adjacency.setdefault(e.source_module_id, []).append(e.target_module_id)

    def size(m: ModuleNode) -> int:
        return len(m.linked_function_ids)

    runtime = [m for m in module_nodes if not _is_test_module(m, func_by_id)]
    pool = runtime or module_nodes  # fall back if everything looks like tests

    entries = [m.id for m in sorted(pool, key=size, reverse=True) if m.is_entry_point]
    if not entries:
        entries = [m.id for m in sorted(pool, key=size, reverse=True)[:1]]
    pool_ids = {m.id for m in pool}

    ordered: List[ModuleNode] = []
    seen: set = set()
    frontier = list(entries)
    while frontier and len(ordered) < limit:
        mid = frontier.pop(0)
        if mid in seen or mid not in by_id or mid not in pool_ids:
            continue
        seen.add(mid)
        ordered.append(by_id[mid])
        # Visit larger neighbours first (runtime modules only)
        nbrs = sorted(
            (i for i in adjacency.get(mid, []) if i in pool_ids),
            key=lambda i: size(by_id[i]) if i in by_id else 0,
            reverse=True,
        )
        frontier.extend(nbrs)

    # Fill remaining slots with the biggest runtime modules not yet reached
    if len(ordered) < limit:
        for m in sorted(pool, key=size, reverse=True):
            if len(ordered) >= limit:
                break
            if m.id not in seen:
                seen.add(m.id)
                ordered.append(m)
    return ordered


def _representative_fn(m: ModuleNode, func_by_id: Dict[str, FunctionNode]) -> str:
    """Return the highest-criticality function name in a module, or ''."""
    fns = sorted(
        (func_by_id[i] for i in m.linked_function_ids if i in func_by_id),
        key=lambda n: _CRITICALITY_RANK.get(n.semantics.criticality if n.semantics else "low", 0),
        reverse=True,
    )
    return fns[0].name if fns else ""


def _disambiguate_labels(
    selected: List[ModuleNode],
    func_by_id: Dict[str, FunctionNode],
) -> Dict[str, str]:
    """Build unique, human-readable labels for modules whose names collide.

    Module names are directory-derived and often repeat (e.g. many "backend/app").
    A bare diagram of nine identical labels is useless, so duplicated names are
    enriched with the module's most critical function ("backend/app · ingest");
    any residual collisions get a short id suffix.
    """
    from collections import Counter
    counts = Counter(m.name for m in selected)
    labels: Dict[str, str] = {}
    used: set = set()
    for m in selected:
        label = m.name
        if counts[m.name] > 1:
            rep = _representative_fn(m, func_by_id)
            if rep:
                label = f"{m.name} · {rep}"
        final, n = label, 2
        while final in used:
            final = f"{label} ({m.id[-4:]})" if n == 2 else f"{label} #{n}"
            n += 1
        used.add(final)
        labels[m.id] = final
    return labels


def _module_mermaid(
    selected: List[ModuleNode],
    module_edges: List[ModuleEdge],
    labels: Dict[str, str],
) -> str:
    """Render a high-level, deterministic Mermaid flowchart for *selected* modules.

    Built purely from the module graph so it can never show a dependency that
    doesn't exist in module_edges.  Only edges between selected modules are drawn,
    keeping the diagram high-level.  Entry-point modules get a distinct class.
    """
    def sid(mid: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", mid)[:40]

    selected_ids = {m.id for m in selected}
    lines = ["flowchart TD"]
    for m in selected:
        label = labels.get(m.id, m.name).replace('"', "'")
        cls = ":::entry" if m.is_entry_point else ""
        lines.append(f'    {sid(m.id)}["{label}"]{cls}')
    for e in module_edges:
        if e.source_module_id in selected_ids and e.target_module_id in selected_ids:
            lines.append(f"    {sid(e.source_module_id)} --> {sid(e.target_module_id)}")
    lines.append("    classDef entry fill:#10b981,color:#fff,stroke:#059669")
    return "\n".join(lines)


def _flow_evidence(
    payload: GraphPayload,
    repo_context: RepositoryContext,
    selected: List[ModuleNode],
    labels: Dict[str, str],
) -> dict:
    """Assemble the compact, module-level evidence pack for the LLM.

    Keeps token cost bounded by working over the *selected* modules only and
    attaching just each module's top-5 functions by criticality.  Modules are
    keyed by their disambiguated *label* so same-named modules stay distinct.
    """
    func_by_id = {n.id: n for n in payload.function_nodes}

    modules = []
    for m in selected:
        fns = sorted(
            (func_by_id[i] for i in m.linked_function_ids if i in func_by_id),
            key=lambda n: _CRITICALITY_RANK.get(
                n.semantics.criticality if n.semantics else "low", 0
            ),
            reverse=True,
        )[:5]
        modules.append({
            "module": labels.get(m.id, m.name),
            "description": m.description,
            "is_entry": m.is_entry_point,
            "key_functions": [
                {
                    "name": f.name,
                    "summary": f.summary or "",
                    "intent": f.semantics.intent if f.semantics else None,
                }
                for f in fns
            ],
        })

    return {
        "purpose": repo_context.purpose,
        "project_type": repo_context.project_type,
        "domain": repo_context.domain,
        "tech_stack": repo_context.tech_stack,
        "modules": modules,
    }


def _build_flow_summary(
    graph_id: str,
    repo_path: str,
    payload: GraphPayload,
    repo_context: RepositoryContext,
) -> FlowSummary:
    """Produce and return the FlowSummary (prose via LLM, diagram deterministic)."""
    func_by_id = {n.id: n for n in payload.function_nodes}
    selected = _select_significant_modules(payload.module_nodes, payload.module_edges, func_by_id)
    labels = _disambiguate_labels(selected, func_by_id)
    evidence = _flow_evidence(payload, repo_context, selected, labels)
    provider = bob_client.get_provider()
    fallback_used = getattr(provider, "_is_heuristic", False)

    llm = bob_client.summarize_repository_flow(evidence)
    module_flows = llm.get("module_flows", {}) or {}

    flows = [
        FlowStep(
            step=i + 1,
            module=m["module"],
            description=module_flows.get(m["module"]) or m.get("description", ""),
            key_functions=[f["name"] for f in m["key_functions"]],
        )
        for i, m in enumerate(evidence["modules"])
    ]

    return FlowSummary(
        graph_id=graph_id,
        repo_path=repo_path,
        what=llm.get("what") or repo_context.purpose,
        usecase=llm.get("usecase", ""),
        flows=flows,
        architecture_mermaid=_module_mermaid(selected, payload.module_edges, labels),
        tech_stack=repo_context.tech_stack,
        techniques=llm.get("techniques", []) or [],
        fallback_used=fallback_used,
    )


def ingest(repo_path: str) -> GraphPayload:
    # Ingestion (repo-context analysis, per-function/module enrichment, and
    # the big llm_ingestion pass) is forced onto the heuristic provider,
    # regardless of what LLM_PROVIDER/BYO-key is configured for the rest of
    # the app. It runs dozens-to-hundreds of LLM calls for a real repo — on
    # a shared, rate-limited server key (e.g. Ollama Cloud's free tier) that
    # would burn the whole session/weekly budget on a single ingest. Query
    # and the flow-summary narrative (_build_flow_summary, below, outside
    # this override) are where "AI mode" actually applies: one or two calls
    # each, on demand, per visitor.
    with bob_client.provider_override(bob_client.heuristic_provider()):
        # Phase 1: Analyze repository context before ingestion
        print(f"[Phase 1] Analyzing repository: {repo_path}")
        repo_context_dict = bob_client.analyze_repository(repo_path)
        try:
            repo_context = RepositoryContext(**repo_context_dict)
        except Exception as e:
            print(f"  - repo context validation failed ({e}); falling back to defaults")
            repo_context = RepositoryContext(fallback_used=True)

        print(f"  - Project type: {repo_context.project_type}")
        print(f"  - Domain: {repo_context.domain}")
        print(f"  - Architecture: {repo_context.architecture}")
        print(f"  - Tech stack: {', '.join(repo_context.tech_stack) if repo_context.tech_stack else 'unknown'}")
        print(f"  - Confidence: {repo_context.confidence:.2f}")
        print(f"  - Fallback used: {repo_context.fallback_used}")

        # Build function graph with context awareness
        g, function_nodes, function_edges = graph_builder.build_function_graph(repo_path)

        # Phase 2: combined summary + semantic analysis (batched, one pass)
        semantic_edges = _enrich_functions(function_nodes, g, repo_context)

        # Push summaries and semantics back into the networkx graph
        for n in function_nodes:
            if n.id in g.nodes:
                g.nodes[n.id]["summary"] = n.summary
                if n.semantics:
                    g.nodes[n.id]["intent"] = n.semantics.intent
                    g.nodes[n.id]["complexity"] = n.semantics.complexity
                    g.nodes[n.id]["criticality"] = n.semantics.criticality

        # Build modules with entry point detection and control flow analysis
        module_nodes, module_edges, mod_to_funcs = module_abstractor.build_modules(
            g,
            function_nodes=function_nodes,
            function_edges=function_edges,
            declared_entry_points=repo_context.key_entry_points,
        )

        graph_id = storage.new_graph_id()
        payload = GraphPayload(
            graph_id=graph_id,
            repo_path=repo_path,
            function_nodes=function_nodes,
            function_edges=function_edges,
            module_nodes=module_nodes,
            module_edges=module_edges,
            module_to_functions=mod_to_funcs,
            semantic_edges=semantic_edges,
        )
        storage.save(payload)

        # Store repository context metadata
        storage.store_meta(graph_id, "repo_context", repo_context.model_dump())

        llm_result, llm_prompt = llm_ingestion.ingest_ast_results(
            repo_context,
            function_nodes,
            function_edges,
            module_nodes,
            mod_to_funcs,
            semantic_edges,
        )
        llm_result.graph_id = graph_id
        llm_result.repo_path = repo_path
        storage.store_meta(graph_id, "llm_ingestion", llm_result.model_dump())
        storage.store_meta(graph_id, "llm_ingestion_prompt", {"prompt": llm_prompt, "version": llm_result.prompt_version})

    # Track current git head for incremental updates.
    _, head = git_updater.changed_files_since(repo_path, None)
    if head:
        storage.store_meta(graph_id, "git", {"head": head})

    # Fix 1: Pre-seed terminology map so the FIRST query benefits from it.
    try:
        learning.seed_terminology_from_graph(graph_id)
    except Exception:
        pass  # Non-critical — ingestion must not fail because of learning

    # Generate the repository flow summary (prose via LLM, diagram deterministic).
    try:
        flow_summary = _build_flow_summary(graph_id, repo_path, payload, repo_context)
        storage.store_meta(graph_id, "flow_summary", flow_summary.model_dump())
    except Exception as e:
        print(f"  - flow summary generation failed ({e}); skipping")

    _build_knowledge_layer(graph_id, repo_path, payload)

    return payload


def _build_knowledge_layer(graph_id: str, repo_path: str, payload: GraphPayload) -> None:
    """Deterministic doc/rationale index + graph analytics; never fails ingestion."""
    try:
        kidx = knowledge.build_knowledge(repo_path, payload.function_nodes)
        kidx.graph_id = graph_id
        storage.store_meta(graph_id, "knowledge", kidx.model_dump())
        print(f"[Knowledge] {len(kidx.documents)} documents, "
              f"{len(kidx.doc_edges)} doc edges, "
              f"{len(kidx.rationale_notes)} rationale notes")
    except Exception as e:
        print(f"  - knowledge layer failed ({e}); skipping")
    try:
        analytics = graph_analytics.compute_analytics(payload)
        storage.store_meta(graph_id, "analytics", analytics)
        print(f"[Analytics] {analytics['stats']['cycle_count']} cycles, "
              f"{len(analytics['god_nodes'])} god nodes ranked")
    except Exception as e:
        print(f"  - graph analytics failed ({e}); skipping")


def update(graph_id: str) -> GraphPayload | None:
    payload = storage.load(graph_id)
    if payload is None:
        return None
    meta = storage.load_meta(graph_id, "git") or {}
    last = meta.get("head")
    changed, new_head = git_updater.changed_files_since(payload.repo_path, last)
    if not changed:
        return payload
    # Re-ingest changed files only.
    repo_root = Path(payload.repo_path).resolve()
    keep_nodes = [n for n in payload.function_nodes if n.file_path not in changed]
    kept_ids = {n.id for n in keep_nodes}
    keep_edges = [
        e for e in payload.function_edges
        if e.source_id in kept_ids and e.target_id in kept_ids
    ]
    new_nodes, new_edges = [], []
    for rel in changed:
        fp = repo_root / rel
        if fp.exists():
            ns, es = graph_builder.parse_file(fp, repo_root)
            new_nodes.extend(ns)
            new_edges.extend(es)

    # Re-resolve symbol edges using the merged node set.
    merged_nodes = keep_nodes + new_nodes
    name_index: Dict[str, list[str]] = {}
    for n in merged_nodes:
        name_index.setdefault(n.name, []).append(n.id)
    resolved_new = []
    for e in new_edges:
        if e.target_id.startswith("<symbol>::"):
            short = e.target_id.split("::", 1)[1]
            for cid in name_index.get(short, []):
                if cid != e.source_id:
                    resolved_new.append(type(e)(
                        type=e.type,
                        relationship=e.relationship,
                        source_id=e.source_id,
                        target_id=cid,
                        adapter_metadata=e.adapter_metadata,
                    ))
        elif not e.target_id.startswith("<"):
            resolved_new.append(e)

    merged_edges = keep_edges + resolved_new

    # Build graph for semantic analysis
    g = nx.DiGraph()
    for n in merged_nodes:
        g.add_node(n.id, **n.model_dump())
    for e in merged_edges:
        if g.has_node(e.source_id) and g.has_node(e.target_id):
            g.add_edge(e.source_id, e.target_id, type=e.type, relationship=e.relationship)

    # Phase 2: combined summary + semantic analysis on new/changed nodes only
    repo_context_dict = storage.load_meta(graph_id, "repo_context") or {}
    try:
        repo_context = RepositoryContext(**repo_context_dict) if repo_context_dict else RepositoryContext(fallback_used=True)
    except Exception:
        repo_context = RepositoryContext(fallback_used=True)
    new_semantic_edges = _enrich_functions(new_nodes, g, repo_context)
    keep_semantic_edges = [
        e for e in payload.semantic_edges
        if e.source_id in kept_ids and e.target_id in kept_ids
    ]
    merged_semantic_edges = keep_semantic_edges + new_semantic_edges
    
    # Push semantics to graph
    for n in merged_nodes:
        if n.id in g.nodes:
            g.nodes[n.id]["summary"] = n.summary
            if n.semantics:
                g.nodes[n.id]["intent"] = n.semantics.intent
                g.nodes[n.id]["complexity"] = n.semantics.complexity
                g.nodes[n.id]["criticality"] = n.semantics.criticality

    # Build modules with entry point detection and control flow analysis
    module_nodes, module_edges, mod_to_funcs = module_abstractor.build_modules(
        g,
        function_nodes=merged_nodes,
        function_edges=merged_edges,
        declared_entry_points=repo_context.key_entry_points,
    )
    payload = GraphPayload(
        graph_id=graph_id,
        repo_path=payload.repo_path,
        function_nodes=merged_nodes,
        function_edges=merged_edges,
        module_nodes=module_nodes,
        module_edges=module_edges,
        module_to_functions=mod_to_funcs,
        semantic_edges=merged_semantic_edges,
    )
    storage.save(payload)
    llm_result, llm_prompt = llm_ingestion.ingest_ast_results(
        repo_context,
        merged_nodes,
        merged_edges,
        module_nodes,
        mod_to_funcs,
        merged_semantic_edges,
    )
    llm_result.graph_id = graph_id
    llm_result.repo_path = payload.repo_path
    storage.store_meta(graph_id, "llm_ingestion", llm_result.model_dump())
    storage.store_meta(graph_id, "llm_ingestion_prompt", {"prompt": llm_prompt, "version": llm_result.prompt_version})
    if new_head:
        storage.store_meta(graph_id, "git", {"head": new_head})

    # Refresh docs/rationale/analytics — changed files may add or remove them.
    _build_knowledge_layer(graph_id, payload.repo_path, payload)
    return payload
