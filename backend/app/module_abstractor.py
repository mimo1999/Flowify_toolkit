"""Phase 4 + 5: cluster the function graph into module-level abstractions.

Strategy:
  1. Restrict to function/method/class nodes (drop file nodes for clustering).
  2. Run an undirected community detection (greedy modularity) on the call graph.
  3. For each cluster, ask Bob for a name + description.
  4. Module FLOW edges = aggregated cross-cluster CALLS.
  5. Tag entry points and organize control flow patterns.

Depth handling:
  - depth_level 1: top-level modules (clusters) with entry point tagging
  - depth_level 2: file groupings inside a cluster (sub-modules) with control flow groups
  - depth_level 3: raw functions
The /graph endpoint exposes nodes for the requested depth and collapses deeper ones.
"""
from __future__ import annotations
import os
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Dict, List, Tuple
import uuid

import networkx as nx
from networkx.algorithms.community import greedy_modularity_communities

from .models import ModuleNode, ModuleEdge, FunctionNode, FunctionEdge
from . import bob_client, control_flow_analyzer

# greedy_modularity_communities is the one genuinely expensive step in this
# pipeline — it OOM'd a 512 MB container on a 2275-node call graph (measured:
# every other stage, including parsing that same repo, ran in well under a
# second). Above this many nodes in one connected component, skip it and
# group directly by directory instead: modularity's own output gets merged
# back into same-directory clusters immediately below anyway (see the
# comment in _cluster), so this produces the same shape for a fraction of
# the cost rather than refusing to ingest the repo at all.
_MODULARITY_NODE_LIMIT = int(os.environ.get("FLOWIFY_MODULARITY_LIMIT", "800"))


def _is_code_symbol(data: dict) -> bool:
    return data.get("kind") in ("function", "callable", "type") or data.get("type") in ("function", "method", "class")


def _is_invocation(edge: dict) -> bool:
    return edge.get("relationship") == "INVOKES" or edge.get("type") == "CALLS"


def _function_subgraph(g: nx.DiGraph) -> nx.DiGraph:
    keep = [n for n, d in g.nodes(data=True) if _is_code_symbol(d)]
    return g.subgraph(keep).copy()


def _cluster(fg: nx.DiGraph) -> List[List[str]]:
    if fg.number_of_nodes() == 0:
        return []
    undirected = fg.to_undirected()
    # Each connected component is treated separately, then modularity inside.
    clusters: List[List[str]] = []
    for comp in nx.connected_components(undirected):
        sub = undirected.subgraph(comp)
        if sub.number_of_nodes() <= 4:
            clusters.append(list(comp))
            continue
        if sub.number_of_nodes() > _MODULARITY_NODE_LIMIT:
            # Group by directory directly instead of running modularity —
            # see _MODULARITY_NODE_LIMIT above.
            by_dir: Dict[str, List[str]] = defaultdict(list)
            for nid in comp:
                by_dir[_name_hint([nid], fg)].append(nid)
            clusters.extend(by_dir.values())
            continue
        try:
            communities = greedy_modularity_communities(sub)
            for c in communities:
                clusters.append(list(c))
        except Exception:
            clusters.append(list(comp))

    # Modularity optimizes for call-graph coupling, not directory structure —
    # a directory whose functions rarely call each other (test suites are the
    # extreme case: each test mostly calls the code under test, not other
    # tests) gets fragmented into many small, identically-named clusters.
    # Merge same-directory clusters back into one so the top-level module
    # view shows one node per directory instead of a dozen indistinguishable
    # duplicates all labeled e.g. "tests".
    merged: Dict[str, List[str]] = defaultdict(list)
    for cluster in clusters:
        merged[_name_hint(cluster, fg)].extend(cluster)
    return list(merged.values())


def _name_hint(node_ids: List[str], g: nx.DiGraph) -> str:
    dirs = defaultdict(int)
    for nid in node_ids:
        fp = g.nodes[nid].get("file_path", "")
        parts = PurePosixPath(fp).parts
        if parts:
            dirs[parts[0] if len(parts) == 1 else "/".join(parts[:-1])] += 1
    if not dirs:
        return "module"
    return max(dirs.items(), key=lambda kv: kv[1])[0] or "module"


def build_modules(
    g: nx.DiGraph,
    function_nodes: List[FunctionNode] | None = None,
    function_edges: List[FunctionEdge] | None = None,
    declared_entry_points: List[str] | None = None,
) -> Tuple[List[ModuleNode], List[ModuleEdge], Dict[str, List[str]]]:
    """Build module abstractions with entry point tagging and control flow analysis."""
    fg = _function_subgraph(g)
    clusters = _cluster(fg)

    # Convert function_nodes to dict for easier lookup
    func_nodes_by_id = {}
    if function_nodes:
        func_nodes_by_id = {n.id: n for n in function_nodes}
    
    # Identify entry point functions from declared entry points
    entry_point_functions = set()
    if declared_entry_points and func_nodes_by_id:
        for entry in declared_entry_points:
            # Convert entry point to file path
            file_path = _entry_to_file_path(entry)
            # Find functions in that file
            for fid, node in func_nodes_by_id.items():
                if node.file_path == file_path:
                    # Check if it's a likely entry function
                    if node.name in {"main", "run", "start", "train"} or node.name.endswith("_main"):
                        entry_point_functions.add(fid)

    module_nodes: List[ModuleNode] = []
    module_to_funcs: Dict[str, List[str]] = {}
    func_to_module: Dict[str, str] = {}

    for cluster in clusters:
        if not cluster:
            continue
        mid = f"mod_{uuid.uuid4().hex[:8]}"
        hint = _name_hint(cluster, g)
        summaries = [g.nodes[n].get("summary") or g.nodes[n].get("name", "") for n in cluster[:30]]
        meta = bob_client.summarize_module(hint, summaries)
        
        # Check if this module contains entry points
        module_entry_funcs = [fid for fid in cluster if fid in entry_point_functions]
        is_entry = len(module_entry_funcs) > 0
        
        # Analyze control flow patterns if we have the data
        control_flow_groups = {}
        if function_nodes and function_edges:
            cf_analysis = control_flow_analyzer.group_control_flow_functions(
                function_nodes, function_edges, cluster
            )
            # Aggregate control flow groups at module level
            for func_id, patterns in cf_analysis.items():
                for pattern_type, called_ids in patterns.items():
                    if pattern_type not in control_flow_groups:
                        control_flow_groups[pattern_type] = []
                    control_flow_groups[pattern_type].extend(called_ids)
            # Deduplicate
            for pattern_type in control_flow_groups:
                control_flow_groups[pattern_type] = list(set(control_flow_groups[pattern_type]))
        
        # Determine submodule type
        submodule_type = None
        if is_entry:
            submodule_type = "entry"
        elif any(g.nodes[n].get("intent") == "orchestration" for n in cluster if n in g.nodes):
            submodule_type = "core"
        elif control_flow_groups:
            submodule_type = "control_flow"
        else:
            submodule_type = "utility"
        
        module_nodes.append(ModuleNode(
            id=mid,
            name=meta["name"],
            description=meta["description"],
            depth_level=1,
            linked_function_ids=cluster,
            is_entry_point=is_entry,
            entry_functions=module_entry_funcs,
            control_flow_groups=control_flow_groups,
            submodule_type=submodule_type,
        ))
        module_to_funcs[mid] = cluster
        for fid in cluster:
            func_to_module[fid] = mid

    # Module FLOW edges: aggregate cross-cluster CALLS.
    edge_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    for u, v, d in g.edges(data=True):
        if not _is_invocation(d):
            continue
        mu, mv = func_to_module.get(u), func_to_module.get(v)
        if mu and mv and mu != mv:
            edge_counts[(mu, mv)] += 1

    module_edges = [ModuleEdge(source_module_id=a, target_module_id=b) for (a, b) in edge_counts]
    return module_nodes, module_edges, module_to_funcs


_ENTRY_NAME_BOOST = {
    "main.py": 45, "__main__.py": 45, "app.py": 10, "run.py": 15,
    "manage.py": 10, "cli.py": 15, "server.py": 10, "train.py": 20,
    "main_trainer.py": 20, "index.py": 8, "ml_main.py": 50,
}

_ENTRY_FILE_PENALTY = {
    "__init__.py", "constants.py", "config.py", "settings.py", "data_loader.py",
    "loader.py", "trainer.py", "evaluator.py", "factory.py", "utils.py",
    "util.py", "helpers.py", "helper.py", "dataset.py", "preprocessor.py",
    "logger.py", "saver.py", "tracker.py", "batcher.py", "cycler.py",
}


def _entry_to_file_path(entry: str) -> str:
    normalized = entry.strip().replace("\\", "/")
    if not normalized:
        return ""
    if normalized.endswith(".py"):
        return normalized
    if "/" in normalized:
        return normalized if "." in normalized.rsplit("/", 1)[-1] else f"{normalized}.py"
    return f"{normalized.replace('.', '/')}.py"


def _file_to_module(file_path: str) -> str:
    return file_path[:-3].replace("/", ".") if file_path.endswith(".py") else file_path.replace("/", ".")


def _entry_function_hint(file_path: str, fids: List[str], function_nodes_by_id: Dict[str, dict]) -> str | None:
    candidates = []
    for fid in fids:
        node = function_nodes_by_id.get(fid, {})
        if node.get("type") not in ("function", "method"):
            continue
        name = node.get("name", "")
        qual = node.get("qualified_name") or fid.split("::", 1)[-1]
        score = 0
        if name == "main":
            score += 50
        if name.endswith("_main"):
            score += 35
        if name in {"run", "start", "train"}:
            score += 25
        if file_path.endswith("main.py") or file_path.endswith("ml_main.py"):
            score += 10
        if score:
            candidates.append((score, qual))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1]


def _is_test_file(file_path: str) -> bool:
    """Return True for test files that should never be picked as entry points."""
    parts = file_path.replace("\\", "/").split("/")
    filename = parts[-1]
    return (
        filename.startswith("test_")
        or filename.endswith("_test.py")
        or any(p in {"tests", "test", "spec", "__tests__"} for p in parts[:-1])
    )


def _entry_node(file_path: str, fids: List[str], function_nodes_by_id: Dict[str, dict], source: str, score: float) -> dict:
    symbol_hint = _entry_function_hint(file_path, fids, function_nodes_by_id)
    module_name = _file_to_module(file_path)
    filename = file_path.split("/")[-1]
    # Use a short human-readable label: just the stem (no extension, no dotted path)
    stem = filename[:-3] if filename.endswith(".py") else filename
    entry_name = stem
    return {
        "id": f"file::{file_path}",
        "label": entry_name,
        "kind": "file",
        "description": file_path,
        "function_count": len(fids),
        "file_path": file_path,
        "entry_module": module_name,
        "entry_symbol": symbol_hint,
        "entry_source": source,
        "entry_score": score,
    }


def find_entry_files(
    function_nodes_by_id: Dict[str, dict],
    function_edges: List[dict],
    declared_entry_points: List[str] | None = None,
    max_count: int = 4,
) -> List[dict]:
    """Pick up to `max_count` entry-point files for the initial graph view.

    Heuristics, combined into a score:
      + file is in repo_context.key_entry_points (Phase 1 finder)         (+10)
      + filename matches a well-known entry name (main/app/run/...)       (+_ENTRY_NAME_BOOST)
      + zero incoming inter-file CALLS                                     (+5)
      + file out-degree (calls into other files)                          (small linear)
    Files with no symbols at all are excluded.
    """
    declared_entries = [p for p in (declared_entry_points or []) if p and p.strip()]
    declared_pairs = []
    for entry in declared_entries:
        file_path = _entry_to_file_path(entry)
        if file_path and file_path not in {fp for fp, _ in declared_pairs}:
            declared_pairs.append((file_path, entry.strip()))

    # Collect files actually present and which functions they own.
    files: Dict[str, List[str]] = defaultdict(list)
    known_files = set()
    for fid, f in function_nodes_by_id.items():
        fp = f.get("file_path", "")
        if fp:
            known_files.add(fp)
        if _is_code_symbol(f):
            if fp:
                files[fp].append(fid)
    for fp in known_files:
        files.setdefault(fp, [])

    # Score each declared entry so that well-known entry filenames (main.py, app.py…)
    # float to the top regardless of the order they appear in repo_context.
    declared_scored = []
    for fp, original in declared_pairs:
        if fp not in known_files and fp not in files:
            continue
        filename = fp.split("/")[-1]
        if filename in _ENTRY_FILE_PENALTY:
            continue
        if _is_test_file(fp):
            continue
        boost = _ENTRY_NAME_BOOST.get(filename, 0)
        declared_scored.append((boost, fp, original))
    # Sort: higher boost first, then alphabetically for stability
    declared_scored.sort(key=lambda t: (-t[0], t[1]))
    # Only trust the declared list when at least one entry is a well-known entry
    # filename (main.py, app.py …).  A stub LLM may produce a list of random
    # module names with boost=0; in that case fall through to structural analysis.
    has_known_entry = any(boost > 0 for boost, _, _ in declared_scored)
    if has_known_entry:
        declared_nodes = [
            _entry_node(fp, files.get(fp, []), function_nodes_by_id, "declared", 999.0)
            for _, fp, _ in declared_scored
        ]
        if declared_nodes:
            return declared_nodes[:max_count]

    # File-level call graph: count inter-file CALLS edges.
    in_deg: Dict[str, int] = defaultdict(int)
    out_deg: Dict[str, int] = defaultdict(int)
    func_to_file = {fid: f.get("file_path", "") for fid, f in function_nodes_by_id.items()}
    for e in function_edges:
        if not _is_invocation(e):
            continue
        sf = func_to_file.get(e["source_id"])
        tf = func_to_file.get(e["target_id"])
        if sf and tf and sf != tf:
            out_deg[sf] += 1
            in_deg[tf] += 1

    scored = []
    for fp, fids in files.items():
        filename = fp.split("/")[-1]
        if _is_test_file(fp):
            continue
        score = 0.0
        score += _ENTRY_NAME_BOOST.get(filename, 0)
        if filename in _ENTRY_FILE_PENALTY:
            score -= 40
        if any(part in {"training", "model_training", "ml_model_training", "ts_model_training"} for part in fp.lower().split("/")[:-1]):
            score += 20
        symbol_hint = _entry_function_hint(fp, fids, function_nodes_by_id)
        if symbol_hint:
            score += 30
        if in_deg[fp] == 0:
            score += 2
        # Entry files orchestrate a few downstream modules; utility hubs with
        # many callers should not win just because they are structurally central.
        score += min(out_deg[fp], 6) * 0.5
        if score > 0:
            scored.append((score, fp, fids))

    scored.sort(key=lambda t: (-t[0], t[1]))
    if any(score >= 60 for score, _, _ in scored):
        scored = [item for item in scored if item[0] >= 60]
    chosen = scored[:max_count]
    if not chosen and files:
        # Last-resort: pick the files with highest out-degree.
        out_sorted = sorted(files.keys(), key=lambda fp: -out_deg[fp])[:max_count]
        chosen = [(0, fp, files[fp]) for fp in out_sorted]

    return [
        {
            **_entry_node(fp, fids, function_nodes_by_id, "heuristic", score),
        }
        for score, fp, fids in chosen
    ]


def _emit_symbol_children(
    parent_id: str,
    fids: List[str],
    function_nodes_by_id: Dict[str, dict],
    function_edges: List[dict],
) -> Tuple[List[dict], List[dict]]:
    """Build child nodes and edges for a set of function IDs under a parent.

    Returns (children, edges) where edges covers:
    - CONTAINS: parent → each child
    - CALLS: between any two children (intra-set only)
    """
    children: List[dict] = []
    edges: List[dict] = []
    in_set: set = set()

    for fid in fids:
        f = function_nodes_by_id.get(fid)
        if not f or not _is_code_symbol(f):
            continue
        in_set.add(fid)
        children.append({
            "id": fid, "label": f.get("name", fid),
            "kind": f.get("type", "function"),
            "parent": parent_id, "description": f.get("summary", "") or "",
            "file_path": f.get("file_path"),
            "adapter_metadata": f.get("adapter_metadata") or {},
        })
        edges.append({
            "id": f"contains:{parent_id}->{fid}",
            "source": parent_id, "target": fid, "kind": "CONTAINS",
        })

    for e in function_edges:
        if not _is_invocation(e):
            continue
        if e["source_id"] in in_set and e["target_id"] in in_set:
            edges.append({
                "id": f"calls:{e['source_id']}->{e['target_id']}",
                "source": e["source_id"], "target": e["target_id"], "kind": "CALLS",
            })

    return children, edges


def _rank_and_cap(scored: Dict[str, int], cap: int) -> List[str]:
    """Rank keys by score descending (ties broken alphabetically) and truncate.

    Shared by both expand_file_node branches so "which children survive
    truncation" is one ranking rule, not a per-action special case — only the
    score function and cap differ (call-degree + symbols, vs edge-count + files).
    """
    return sorted(scored, key=lambda k: (-scored[k], k))[:cap]


def expand_file_node(
    function_nodes_by_id: Dict[str, dict],
    function_edges: List[dict],
    file_path: str,
    action: str = "callees",
) -> dict:
    """Expand a file-level entry node.

    action="callees"  → show files this file calls (FLOW edges)
    action="functions" → show function/class/method symbols inside (CONTAINS)
    """
    node_id = f"file::{file_path}"
    children: List[dict] = []
    edges: List[dict] = []

    # Cap: never return more than this many children to keep the canvas readable.
    _MAX_CHILDREN = 12
    # Drilling into a file is deliberate, so it tolerates a bigger canvas; large
    # files still get truncated, dropping the least-connected symbols first.
    _MAX_SYMBOL_CHILDREN = 40

    if action == "functions":
        fids = [fid for fid, f in function_nodes_by_id.items() if f.get("file_path") == file_path]
        degree: Dict[str, int] = defaultdict(int)
        for e in function_edges:
            if _is_invocation(e):
                degree[e["source_id"]] += 1
                degree[e["target_id"]] += 1
        ranked_fids = _rank_and_cap({fid: degree.get(fid, 0) for fid in fids}, _MAX_SYMBOL_CHILDREN)
        children, edges = _emit_symbol_children(
            node_id, ranked_fids, function_nodes_by_id, function_edges,
        )
        return {"parent_id": node_id, "children": children, "edges": edges}

    # action == "callees": show files this file calls (file-level CALLS aggregation).
    func_to_file = {fid: f.get("file_path", "") for fid, f in function_nodes_by_id.items()}
    callee_files: Dict[str, int] = defaultdict(int)
    file_meta: Dict[str, List[str]] = defaultdict(list)
    for fid, f in function_nodes_by_id.items():
        if _is_code_symbol(f) and f.get("file_path"):
            file_meta[f["file_path"]].append(fid)

    for e in function_edges:
        if not _is_invocation(e):
            continue
        sf = func_to_file.get(e["source_id"])
        tf = func_to_file.get(e["target_id"])
        if sf == file_path and tf and tf != file_path:
            callee_files[tf] += 1

    for tf in _rank_and_cap(callee_files, _MAX_CHILDREN):
        tid = f"file::{tf}"
        children.append({
            "id": tid, "label": tf.split("/")[-1], "kind": "file",
            "parent": node_id, "description": tf,
            "function_count": len(file_meta.get(tf, [])),
            "file_path": tf,
        })
        edges.append({
            "id": f"flow:{node_id}->{tid}",
            "source": node_id, "target": tid, "kind": "FLOW",
        })
    return {"parent_id": node_id, "children": children, "edges": edges}


def expand_node(
    module_nodes: List[ModuleNode],
    function_nodes_by_id: Dict[str, dict],
    function_edges: List[dict],
    node_id: str,
) -> dict:
    """Return one level of children for a given node, plus relevant edges.

    - module (`mod_xxx`)            → file-level submodules + CONTAINS + intra-module FLOW
                                      + control flow groups if present
    - submodule (`mod_xxx::path`)   → function nodes in that file + CONTAINS + CALLS
                                      (caller filters by visibility)
    - function (`path::qualname`)   → callees one CALLS hop out + CALLS edges
    """
    module_by_id = {m.id: m for m in module_nodes}
    children: List[dict] = []
    edges: List[dict] = []

    if node_id in module_by_id:
        m = module_by_id[node_id]
        files = defaultdict(list)
        for fid in m.linked_function_ids:
            fp = function_nodes_by_id.get(fid, {}).get("file_path", "?")
            files[fp].append(fid)
        for fp, fids in files.items():
            sub_id = f"{m.id}::{fp}"
            children.append({
                "id": sub_id, "label": fp.split("/")[-1], "kind": "submodule",
                "parent": m.id, "description": fp,
                "function_count": len(fids), "file_path": fp,
            })
            edges.append({
                "id": f"contains:{m.id}->{sub_id}",
                "source": m.id, "target": sub_id, "kind": "CONTAINS",
            })
        
        # Add control flow group nodes if present
        if m.control_flow_groups:
            for pattern_type, func_ids in m.control_flow_groups.items():
                cf_id = f"{m.id}::cf::{pattern_type}"
                children.append({
                    "id": cf_id,
                    "label": f"{pattern_type.replace('_', ' ').title()}",
                    "kind": "control_flow_group",
                    "parent": m.id,
                    "description": control_flow_analyzer._pattern_description(pattern_type),
                    "function_count": len(func_ids),
                    "pattern_type": pattern_type,
                    "function_ids": func_ids,
                })
                edges.append({
                    "id": f"contains:{m.id}->{cf_id}",
                    "source": m.id, "target": cf_id, "kind": "CONTAINS",
                })
        # Intra-module file→file FLOW (aggregated CALLS).
        sub_id_for = {
            fid: f"{m.id}::{function_nodes_by_id.get(fid, {}).get('file_path', '?')}"
            for fid in m.linked_function_ids
        }
        seen_pairs = set()
        for e in function_edges:
            if not _is_invocation(e):
                continue
            s = sub_id_for.get(e["source_id"])
            t = sub_id_for.get(e["target_id"])
            if s and t and s != t and (s, t) not in seen_pairs:
                seen_pairs.add((s, t))
                edges.append({
                    "id": f"flow:{s}->{t}",
                    "source": s, "target": t, "kind": "FLOW",
                })
        return {"parent_id": node_id, "children": children, "edges": edges}

    # Submodule (file): "mod_xxx::path/to/file.py"
    if "::" in node_id and node_id.split("::", 1)[0] in module_by_id:
        mid, fp = node_id.split("::", 1)
        m = module_by_id[mid]
        fids = [fid for fid in m.linked_function_ids
                if function_nodes_by_id.get(fid, {}).get("file_path") == fp]
        children, edges = _emit_symbol_children(node_id, fids, function_nodes_by_id, function_edges)
        return {"parent_id": node_id, "children": children, "edges": edges}

    # Function-level node — expand callees one hop.
    if node_id in function_nodes_by_id:
        for e in function_edges:
            if not _is_invocation(e) or e["source_id"] != node_id:
                continue
            tgt = function_nodes_by_id.get(e["target_id"])
            if not tgt:
                continue
            children.append({
                "id": e["target_id"], "label": tgt.get("name", e["target_id"]),
                "kind": tgt.get("type", "function"),
                "parent": node_id, "description": tgt.get("summary", "") or "",
                "file_path": tgt.get("file_path"),
                "adapter_metadata": tgt.get("adapter_metadata") or {},
            })
            edges.append({
                "id": f"calls:{node_id}->{e['target_id']}",
                "source": node_id, "target": e["target_id"], "kind": "CALLS",
            })
        return {"parent_id": node_id, "children": children, "edges": edges}

    return {"parent_id": node_id, "children": [], "edges": []}


def collapse_for_depth(
    module_nodes: List[ModuleNode],
    module_edges: List[ModuleEdge],
    module_to_funcs: Dict[str, List[str]],
    function_nodes_by_id: Dict[str, dict],
    depth: int,
    function_edges: List[dict] | None = None,
) -> dict:
    """Return a JSON-friendly graph for the requested depth (1..3).

    Edges emitted at each depth:
      depth 1: FLOW between modules (aggregated cross-cluster CALLS).
      depth 2: FLOW between modules, FLOW between file-level submodules
               (aggregated cross-file CALLS), CONTAINS module → submodule.
      depth 3: FLOW between modules, CALLS between individual functions,
               CONTAINS module → function.
    """
    function_edges = function_edges or []
    nodes_out, edges_out = [], []

    # Always include the modules themselves with enhanced metadata.
    for m in module_nodes:
        node_data = {
            "id": m.id, "label": m.name, "kind": "module",
            "description": m.description,
            "function_count": len(m.linked_function_ids),
            "is_entry_point": m.is_entry_point,
            "submodule_type": m.submodule_type,
        }
        # Include control flow summary if present
        if m.control_flow_groups:
            node_data["control_flow_patterns"] = list(m.control_flow_groups.keys())
            node_data["control_flow_summary"] = {
                pattern: len(funcs) for pattern, funcs in m.control_flow_groups.items()
            }
        nodes_out.append(node_data)
    for e in module_edges:
        edges_out.append({
            "id": f"flow:{e.source_module_id}->{e.target_module_id}",
            "source": e.source_module_id, "target": e.target_module_id, "kind": "FLOW",
        })

    if depth <= 1:
        return {"nodes": nodes_out, "edges": edges_out, "depth": 1}

    # ---- depth >= 2: file-level submodules + structural edges --------------
    func_to_module: Dict[str, str] = {}
    for mid, fids in module_to_funcs.items():
        for fid in fids:
            func_to_module[fid] = mid

    if depth == 2:
        # File-level grouping inside each module.
        sub_id_for: Dict[str, str] = {}  # function_id -> submodule_id
        seen_subs: set = set()
        for m in module_nodes:
            files = defaultdict(list)
            for fid in m.linked_function_ids:
                fp = function_nodes_by_id.get(fid, {}).get("file_path", "?")
                files[fp].append(fid)
            for fp, fids in files.items():
                sub_id = f"{m.id}::{fp}"
                if sub_id not in seen_subs:
                    seen_subs.add(sub_id)
                    nodes_out.append({
                        "id": sub_id, "label": fp.split("/")[-1], "kind": "submodule",
                        "parent": m.id, "description": fp,
                        "function_count": len(fids),
                    })
                    edges_out.append({
                        "id": f"contains:{m.id}->{sub_id}",
                        "source": m.id, "target": sub_id, "kind": "CONTAINS",
                    })
                for fid in fids:
                    sub_id_for[fid] = sub_id

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
        return {"nodes": nodes_out, "edges": edges_out, "depth": 2}

    # ---- depth 3: real function nodes + real CALLS edges -------------------
    visible_func_ids: set = set()
    for m in module_nodes:
        for fid in m.linked_function_ids:
            f = function_nodes_by_id.get(fid)
            if not f:
                continue
            visible_func_ids.add(fid)
            nodes_out.append({
                "id": fid, "label": f.get("name", fid),
                "kind": f.get("type", "function"),
                "parent": m.id, "description": f.get("summary", "") or "",
                "file_path": f.get("file_path"),
                "adapter_metadata": f.get("adapter_metadata") or {},
            })
            edges_out.append({
                "id": f"contains:{m.id}->{fid}",
                "source": m.id, "target": fid, "kind": "CONTAINS",
            })
    for e in function_edges:
        if not _is_invocation(e):
            continue
        s, t = e["source_id"], e["target_id"]
        if s in visible_func_ids and t in visible_func_ids:
            edges_out.append({
                "id": f"calls:{s}->{t}",
                "source": s, "target": t, "kind": "CALLS",
            })
    return {"nodes": nodes_out, "edges": edges_out, "depth": 3}
