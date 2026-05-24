"""Self-graph integration test suite.

Ingests the Flowify repository itself and verifies that the backend produces
exactly the expected graph structure — file nodes, key function nodes, and
the critical cross-file CALLS edges documented in docs/self_graph.md.

Run:
    cd backend
    ../.venv/Scripts/python -m pytest ../tests/test_self_graph.py -v

The fixture is session-scoped so ingestion runs only once.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

# ── path setup ────────────────────────────────────────────────────────────────
REPO_ROOT = str(Path(__file__).parent.parent.resolve())
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from app import pipeline, module_abstractor, storage
from app.models import GraphPayload, FunctionNode, FunctionEdge


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def payload() -> GraphPayload:
    """Ingest the Flowify repo once for the entire test session."""
    return pipeline.ingest(REPO_ROOT)


@pytest.fixture(scope="session")
def fn_by_id(payload: GraphPayload) -> Dict[str, FunctionNode]:
    return {n.id: n for n in payload.function_nodes}


@pytest.fixture(scope="session")
def calls_edges(payload: GraphPayload) -> Set[Tuple[str, str]]:
    """Set of (source_id, target_id) for every CALLS / INVOKES edge."""
    return {
        (e.source_id, e.target_id)
        for e in payload.function_edges
        if e.type == "CALLS" or e.relationship == "INVOKES"
    }


@pytest.fixture(scope="session")
def fn_dicts(payload: GraphPayload) -> Dict[str, dict]:
    """function_nodes_by_id in plain-dict form (for module_abstractor calls)."""
    return {n.id: n.model_dump() for n in payload.function_nodes}


@pytest.fixture(scope="session")
def fe_dicts(payload: GraphPayload) -> List[dict]:
    """function_edges as plain dicts (for module_abstractor calls)."""
    return [e.model_dump() for e in payload.function_edges]


@pytest.fixture(scope="session")
def entry_nodes(payload: GraphPayload, fn_dicts, fe_dicts):
    """find_entry_files result for the Flowify repo."""
    return module_abstractor.find_entry_files(
        fn_dicts, fe_dicts,
        declared_entry_points=None,
        max_count=4,
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _nodes_for_file(file_path: str, fn_by_id: Dict[str, FunctionNode]) -> List[FunctionNode]:
    """Return all non-file function/class nodes that belong to a given file path."""
    return [
        n for n in fn_by_id.values()
        if n.file_path == file_path and n.kind != "file"
    ]


def _has_fn(name: str, file_path: str, fn_by_id: Dict[str, FunctionNode]) -> bool:
    return any(
        n.name == name and n.file_path == file_path
        for n in fn_by_id.values()
    )


def _fn_id(name: str, file_path: str, fn_by_id: Dict[str, FunctionNode]) -> str | None:
    for n in fn_by_id.values():
        if n.name == name and n.file_path == file_path:
            return n.id
    return None


def _calls(caller_name: str, caller_file: str,
           callee_name: str, callee_file: str,
           fn_by_id: Dict[str, FunctionNode],
           calls_edges: Set[Tuple[str, str]]) -> bool:
    src = _fn_id(caller_name, caller_file, fn_by_id)
    tgt = _fn_id(callee_name, callee_file, fn_by_id)
    if src is None or tgt is None:
        return False
    return (src, tgt) in calls_edges


# Canonical file paths (POSIX, relative to repo root)
M = {
    "main":      "backend/app/main.py",
    "pipeline":  "backend/app/pipeline.py",
    "gb":        "backend/app/graph_builder.py",
    "ma":        "backend/app/module_abstractor.py",
    "ret":       "backend/app/retrieval.py",
    "llm":       "backend/app/llm_provider.py",
    "llmi":      "backend/app/llm_ingestion.py",
    "storage":   "backend/app/storage.py",
    "models":    "backend/app/models.py",
    "learning":  "backend/app/learning.py",
    "mcp":       "mcp_server/flowify_mcp.py",
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. PAYLOAD — file presence
# ══════════════════════════════════════════════════════════════════════════════

class TestPayloadFiles:
    """Every core backend file must appear as a parsed node in the payload."""

    def test_main_py_parsed(self, fn_by_id):
        assert _nodes_for_file(M["main"], fn_by_id), "main.py produced no function nodes"

    def test_pipeline_py_parsed(self, fn_by_id):
        assert _nodes_for_file(M["pipeline"], fn_by_id)

    def test_graph_builder_py_parsed(self, fn_by_id):
        assert _nodes_for_file(M["gb"], fn_by_id)

    def test_module_abstractor_py_parsed(self, fn_by_id):
        assert _nodes_for_file(M["ma"], fn_by_id)

    def test_retrieval_py_parsed(self, fn_by_id):
        assert _nodes_for_file(M["ret"], fn_by_id)

    def test_llm_provider_py_parsed(self, fn_by_id):
        assert _nodes_for_file(M["llm"], fn_by_id)

    def test_llm_ingestion_py_parsed(self, fn_by_id):
        assert _nodes_for_file(M["llmi"], fn_by_id)

    def test_storage_py_parsed(self, fn_by_id):
        assert _nodes_for_file(M["storage"], fn_by_id)

    def test_models_py_parsed(self, fn_by_id):
        assert _nodes_for_file(M["models"], fn_by_id)

    def test_mcp_server_parsed(self, fn_by_id):
        assert _nodes_for_file(M["mcp"], fn_by_id)

    def test_no_venv_files_ingested(self, fn_by_id):
        """Files inside .venv must never appear in the graph."""
        venv_nodes = [n for n in fn_by_id.values() if ".venv" in (n.file_path or "")]
        assert not venv_nodes, f"Unexpected .venv nodes: {[n.id for n in venv_nodes[:5]]}"

    def test_no_pycache_files_ingested(self, fn_by_id):
        pycache = [n for n in fn_by_id.values() if "__pycache__" in (n.file_path or "")]
        assert not pycache


# ══════════════════════════════════════════════════════════════════════════════
# 2. PAYLOAD — function / class presence
# ══════════════════════════════════════════════════════════════════════════════

class TestPayloadFunctions:
    """Key named symbols must exist inside the expected source files."""

    # main.py — HTTP endpoints
    @pytest.mark.parametrize("name", [
        "ingest_repo", "expand_graph_node", "get_entry_points",
        "query_graph", "submit_feedback", "update_graph", "provider_info",
    ])
    def test_main_endpoint_exists(self, name, fn_by_id):
        assert _has_fn(name, M["main"], fn_by_id), f"main.py missing: {name}"

    # pipeline.py
    @pytest.mark.parametrize("name", ["ingest", "update"])
    def test_pipeline_fn_exists(self, name, fn_by_id):
        assert _has_fn(name, M["pipeline"], fn_by_id), f"pipeline.py missing: {name}"

    # graph_builder.py
    @pytest.mark.parametrize("name", [
        "build_function_graph", "parse_file", "parse_python_file",
        "parse_js_ts_file", "_append_call_edges",
    ])
    def test_graph_builder_fn_exists(self, name, fn_by_id):
        assert _has_fn(name, M["gb"], fn_by_id), f"graph_builder.py missing: {name}"

    # module_abstractor.py
    @pytest.mark.parametrize("name", [
        "build_modules", "find_entry_files", "expand_file_node",
        "expand_node", "collapse_for_depth", "_is_test_file",
    ])
    def test_module_abstractor_fn_exists(self, name, fn_by_id):
        assert _has_fn(name, M["ma"], fn_by_id), f"module_abstractor.py missing: {name}"

    # retrieval.py
    @pytest.mark.parametrize("name", ["retrieve_subgraph", "explain", "_entry_nodes"])
    def test_retrieval_fn_exists(self, name, fn_by_id):
        assert _has_fn(name, M["ret"], fn_by_id), f"retrieval.py missing: {name}"

    # llm_provider.py
    @pytest.mark.parametrize("name", [
        "get_provider", "ask", "ask_json", "summarize_function",
        "summarize_module", "explain_flow", "interpret_query",
    ])
    def test_llm_provider_fn_exists(self, name, fn_by_id):
        assert _has_fn(name, M["llm"], fn_by_id), f"llm_provider.py missing: {name}"

    # llm_ingestion.py
    @pytest.mark.parametrize("name", ["ingest_ast_results", "build_prompt"])
    def test_llm_ingestion_fn_exists(self, name, fn_by_id):
        assert _has_fn(name, M["llmi"], fn_by_id), f"llm_ingestion.py missing: {name}"

    # storage.py
    @pytest.mark.parametrize("name", ["save", "load", "list_graphs", "store_meta", "load_meta"])
    def test_storage_fn_exists(self, name, fn_by_id):
        assert _has_fn(name, M["storage"], fn_by_id), f"storage.py missing: {name}"

    def test_every_node_has_file_path(self, fn_by_id):
        """Every non-external node must have a non-empty file_path."""
        bad = [n.id for n in fn_by_id.values() if not n.file_path and n.kind != "external"]
        assert not bad, f"Nodes without file_path: {bad[:10]}"

    def test_every_node_id_matches_pattern(self, fn_by_id):
        """Node IDs must be either 'file::<path>' or '<path>::<qualname>'."""
        bad = []
        for nid in fn_by_id:
            if not (nid.startswith("file::") or "::" in nid):
                bad.append(nid)
        assert not bad, f"Malformed node IDs: {bad[:10]}"


# ══════════════════════════════════════════════════════════════════════════════
# 3. PAYLOAD — cross-file CALLS edges
# ══════════════════════════════════════════════════════════════════════════════

class TestPayloadCallEdges:
    """Critical cross-file call relationships that define Flowify's data-flow."""

    def test_ingest_repo_calls_pipeline_ingest(self, fn_by_id, calls_edges):
        assert _calls("ingest_repo", M["main"], "ingest", M["pipeline"], fn_by_id, calls_edges), \
            "main.ingest_repo must call pipeline.ingest"

    def test_pipeline_ingest_calls_build_function_graph(self, fn_by_id, calls_edges):
        assert _calls("ingest", M["pipeline"], "build_function_graph", M["gb"], fn_by_id, calls_edges), \
            "pipeline.ingest must call graph_builder.build_function_graph"

    def test_pipeline_ingest_calls_build_modules(self, fn_by_id, calls_edges):
        assert _calls("ingest", M["pipeline"], "build_modules", M["ma"], fn_by_id, calls_edges), \
            "pipeline.ingest must call module_abstractor.build_modules"

    def test_pipeline_ingest_calls_storage_save(self, fn_by_id, calls_edges):
        assert _calls("ingest", M["pipeline"], "save", M["storage"], fn_by_id, calls_edges), \
            "pipeline.ingest must call storage.save"

    def test_pipeline_ingest_calls_llm_ingestion(self, fn_by_id, calls_edges):
        assert _calls("ingest", M["pipeline"], "ingest_ast_results", M["llmi"], fn_by_id, calls_edges), \
            "pipeline.ingest must call llm_ingestion.ingest_ast_results"

    def test_expand_calls_expand_file_node(self, fn_by_id, calls_edges):
        assert _calls("expand_graph_node", M["main"], "expand_file_node", M["ma"], fn_by_id, calls_edges), \
            "main.expand_graph_node must call module_abstractor.expand_file_node"

    def test_expand_calls_expand_node(self, fn_by_id, calls_edges):
        assert _calls("expand_graph_node", M["main"], "expand_node", M["ma"], fn_by_id, calls_edges), \
            "main.expand_graph_node must call module_abstractor.expand_node"

    def test_get_entry_points_calls_find_entry_files(self, fn_by_id, calls_edges):
        assert _calls("get_entry_points", M["main"], "find_entry_files", M["ma"], fn_by_id, calls_edges), \
            "main.get_entry_points must call module_abstractor.find_entry_files"

    def test_query_graph_calls_retrieve_subgraph(self, fn_by_id, calls_edges):
        assert _calls("query_graph", M["main"], "retrieve_subgraph", M["ret"], fn_by_id, calls_edges), \
            "main.query_graph must call retrieval.retrieve_subgraph"

    def test_query_graph_calls_explain(self, fn_by_id, calls_edges):
        assert _calls("query_graph", M["main"], "explain", M["ret"], fn_by_id, calls_edges), \
            "main.query_graph must call retrieval.explain"

    def test_entry_nodes_calls_interpret_query(self, fn_by_id, calls_edges):
        # retrieve_subgraph delegates to _entry_nodes which calls interpret_query
        assert _calls("_entry_nodes", M["ret"], "interpret_query", M["llm"], fn_by_id, calls_edges), \
            "retrieval._entry_nodes must call llm_provider.interpret_query"

    def test_explain_calls_explain_flow(self, fn_by_id, calls_edges):
        assert _calls("explain", M["ret"], "explain_flow", M["llm"], fn_by_id, calls_edges), \
            "retrieval.explain must call llm_provider.explain_flow"

    def test_llmi_calls_ask_json(self, fn_by_id, calls_edges):
        assert _calls("ingest_ast_results", M["llmi"], "ask_json", M["llm"], fn_by_id, calls_edges), \
            "llm_ingestion.ingest_ast_results must call llm_provider.ask_json"

    def test_build_function_graph_calls_parse_file(self, fn_by_id, calls_edges):
        assert _calls("build_function_graph", M["gb"], "parse_file", M["gb"], fn_by_id, calls_edges), \
            "graph_builder.build_function_graph must call parse_file"

    def test_parse_file_calls_parse_python_file(self, fn_by_id, calls_edges):
        assert _calls("parse_file", M["gb"], "parse_python_file", M["gb"], fn_by_id, calls_edges), \
            "graph_builder.parse_file must call parse_python_file"

    def test_update_graph_calls_pipeline_update(self, fn_by_id, calls_edges):
        assert _calls("update_graph", M["main"], "update", M["pipeline"], fn_by_id, calls_edges), \
            "main.update_graph must call pipeline.update"


# ══════════════════════════════════════════════════════════════════════════════
# 4. PAYLOAD — structural invariants
# ══════════════════════════════════════════════════════════════════════════════

class TestPayloadInvariants:
    """The payload must satisfy basic graph-theory invariants."""

    def test_no_duplicate_node_ids(self, payload: GraphPayload):
        ids = [n.id for n in payload.function_nodes]
        assert len(ids) == len(set(ids)), "Duplicate node IDs in function_nodes"

    def test_all_edge_sources_exist(self, payload: GraphPayload, fn_by_id):
        missing = {
            e.source_id for e in payload.function_edges
            if e.source_id not in fn_by_id
        }
        assert not missing, f"Edges with unknown source_id: {list(missing)[:10]}"

    def test_all_edge_targets_exist(self, payload: GraphPayload, fn_by_id):
        missing = {
            e.target_id for e in payload.function_edges
            if e.target_id not in fn_by_id
        }
        assert not missing, f"Edges with unknown target_id: {list(missing)[:10]}"

    def test_no_self_loops(self, payload: GraphPayload):
        loops = [e for e in payload.function_edges if e.source_id == e.target_id]
        assert not loops, f"Self-loop edges: {[e.source_id for e in loops[:5]]}"

    def test_no_duplicate_edges(self, payload: GraphPayload):
        seen = set()
        dups = []
        for e in payload.function_edges:
            key = (e.source_id, e.target_id, e.type)
            if key in seen:
                dups.append(key)
            seen.add(key)
        assert not dups, f"Duplicate edges: {dups[:5]}"

    def test_every_node_has_name(self, fn_by_id):
        unnamed = [n.id for n in fn_by_id.values() if not n.name]
        assert not unnamed, f"Nodes with empty name: {unnamed[:10]}"

    def test_backend_has_substantial_nodes(self, payload: GraphPayload):
        """Sanity check: Flowify's own backend must produce a non-trivial graph."""
        fn_count = sum(
            1 for n in payload.function_nodes
            if n.kind in ("function", "callable") or n.type in ("function", "method")
        )
        assert fn_count >= 80, f"Expected ≥80 function nodes, got {fn_count}"

    def test_backend_has_cross_file_calls(self, calls_edges):
        """The resolved call graph must have at least 30 cross-file edges."""
        assert len(calls_edges) >= 30, f"Only {len(calls_edges)} CALLS edges — resolution may have broken"


# ══════════════════════════════════════════════════════════════════════════════
# 5. ENTRY POINTS — find_entry_files output
# ══════════════════════════════════════════════════════════════════════════════

class TestEntryPoints:
    """Verifies what the initial graph view shows when Flowify ingests itself."""

    def test_entry_points_not_empty(self, entry_nodes):
        assert entry_nodes, "find_entry_files returned nothing for the Flowify repo"

    def test_at_most_four_entry_points(self, entry_nodes):
        assert len(entry_nodes) <= 4

    def test_main_py_is_first_entry(self, entry_nodes):
        first_id = entry_nodes[0]["id"]
        assert first_id == "file::backend/app/main.py", \
            f"Expected main.py first, got: {first_id}"

    def test_no_test_files_in_entries(self, entry_nodes):
        for node in entry_nodes:
            fp = node.get("file_path", node.get("id", ""))
            assert "test" not in fp.lower(), \
                f"Test file in entry points: {fp}"

    def test_entry_node_ids_use_file_prefix(self, entry_nodes):
        for node in entry_nodes:
            assert node["id"].startswith("file::"), \
                f"Entry node ID must start with 'file::': {node['id']}"

    def test_entry_nodes_have_file_path(self, entry_nodes):
        for node in entry_nodes:
            assert node.get("file_path"), f"Entry node missing file_path: {node}"

    def test_entry_nodes_have_function_count(self, entry_nodes):
        for node in entry_nodes:
            assert "function_count" in node, f"Entry node missing function_count: {node}"

    def test_entry_main_has_many_symbols(self, entry_nodes):
        main_node = next((n for n in entry_nodes if "main.py" in n.get("file_path", "")), None)
        assert main_node is not None
        assert main_node["function_count"] >= 10, \
            f"main.py should expose ≥10 symbols, got {main_node['function_count']}"

    def test_venv_not_in_entries(self, entry_nodes):
        for node in entry_nodes:
            assert ".venv" not in node["id"], f".venv in entry: {node['id']}"


# ══════════════════════════════════════════════════════════════════════════════
# 6. EXPAND — expand_file_node("backend/app/main.py", action="functions")
# ══════════════════════════════════════════════════════════════════════════════

class TestExpandMainPy:
    """Expanding main.py must return its endpoints as children with correct structure."""

    @pytest.fixture(scope="class")
    def expanded(self, fn_dicts, fe_dicts):
        return module_abstractor.expand_file_node(
            fn_dicts, fe_dicts,
            file_path=M["main"],
            action="functions",
        )

    def test_returns_children(self, expanded):
        assert expanded["children"], "expand_file_node(main.py) returned no children"

    def test_parent_id_is_file_node(self, expanded):
        assert expanded["parent_id"] == "file::backend/app/main.py"

    def test_all_children_parent_set_correctly(self, expanded):
        for child in expanded["children"]:
            assert child["parent"] == "file::backend/app/main.py", \
                f"Child {child['id']} has wrong parent: {child['parent']}"

    def test_all_children_have_file_path(self, expanded):
        for child in expanded["children"]:
            assert child.get("file_path") == M["main"], \
                f"Child {child['id']} has wrong file_path: {child.get('file_path')}"

    def test_key_endpoints_in_children(self, expanded):
        names = {c["label"] for c in expanded["children"]}
        expected = {"ingest_repo", "query_graph", "expand_graph_node", "get_entry_points"}
        missing = expected - names
        assert not missing, f"main.py expand missing: {missing}"

    def test_contains_edges_present(self, expanded):
        contains = [e for e in expanded["edges"] if e["kind"] == "CONTAINS"]
        assert contains, "No CONTAINS edges in expand_file_node result"

    def test_contains_edge_sources_match_parent(self, expanded):
        for e in expanded["edges"]:
            if e["kind"] == "CONTAINS":
                assert e["source"] == "file::backend/app/main.py", \
                    f"CONTAINS edge has wrong source: {e['source']}"

    def test_all_children_have_contains_edge(self, expanded):
        # expand_file_node caps children at _MAX_CHILDREN=12 but emits edges for all
        # symbols. Assert the weaker correct invariant: every *child* has a CONTAINS edge.
        child_ids = {c["id"] for c in expanded["children"]}
        edge_targets = {e["target"] for e in expanded["edges"] if e["kind"] == "CONTAINS"}
        missing = child_ids - edge_targets
        assert not missing, f"Children with no CONTAINS edge: {missing}"

    def test_no_duplicate_child_ids(self, expanded):
        ids = [c["id"] for c in expanded["children"]]
        assert len(ids) == len(set(ids)), "Duplicate child IDs in expand result"

    def test_no_orphan_children(self, expanded):
        """Every child must have a CONTAINS edge from parent."""
        child_ids = {c["id"] for c in expanded["children"]}
        edge_targets = {e["target"] for e in expanded["edges"] if e["kind"] == "CONTAINS"}
        orphans = child_ids - edge_targets
        assert not orphans, f"Orphan children (no CONTAINS edge): {orphans}"


# ══════════════════════════════════════════════════════════════════════════════
# 7. EXPAND — expand_file_node("backend/app/pipeline.py", action="functions")
# ══════════════════════════════════════════════════════════════════════════════

class TestExpandPipelinePy:
    @pytest.fixture(scope="class")
    def expanded(self, fn_dicts, fe_dicts):
        return module_abstractor.expand_file_node(
            fn_dicts, fe_dicts,
            file_path=M["pipeline"],
            action="functions",
        )

    def test_ingest_in_children(self, expanded):
        names = {c["label"] for c in expanded["children"]}
        assert "ingest" in names

    def test_update_in_children(self, expanded):
        names = {c["label"] for c in expanded["children"]}
        assert "update" in names

    def test_intra_file_calls_edges_present(self, expanded):
        """pipeline.py has internal calls between its own helpers."""
        calls = [e for e in expanded["edges"] if e["kind"] == "CALLS"]
        assert calls, "No intra-file CALLS edges in pipeline.py expand"

    def test_parent_id_correct(self, expanded):
        assert expanded["parent_id"] == "file::backend/app/pipeline.py"


# ══════════════════════════════════════════════════════════════════════════════
# 8. EXPAND callees — pipeline.ingest's outgoing CALLS to other files
# ══════════════════════════════════════════════════════════════════════════════

class TestExpandPipelineIngestCallees:
    """Expanding pipeline.py as callees from main.py must reach core modules."""

    @pytest.fixture(scope="class")
    def expanded_callees(self, fn_dicts, fe_dicts):
        # action="callees" on a file node returns file-level callees
        return module_abstractor.expand_file_node(
            fn_dicts, fe_dicts,
            file_path=M["pipeline"],
            action="callees",
        )

    def test_calls_graph_builder(self, expanded_callees):
        callee_files = {c["file_path"] for c in expanded_callees["children"]}
        assert M["gb"] in callee_files, \
            f"pipeline.py must call graph_builder.py; callee files: {callee_files}"

    def test_calls_storage(self, expanded_callees):
        callee_files = {c["file_path"] for c in expanded_callees["children"]}
        assert M["storage"] in callee_files, \
            f"pipeline.py must call storage.py; callee files: {callee_files}"

    def test_calls_module_abstractor(self, expanded_callees):
        callee_files = {c["file_path"] for c in expanded_callees["children"]}
        assert M["ma"] in callee_files, \
            f"pipeline.py must call module_abstractor.py; callee files: {callee_files}"

    def test_callee_edges_are_flow_kind(self, expanded_callees):
        for e in expanded_callees["edges"]:
            assert e["kind"] == "FLOW", f"Callee edge should be FLOW, got: {e['kind']}"

    def test_callee_file_nodes_have_function_count(self, expanded_callees):
        for child in expanded_callees["children"]:
            assert "function_count" in child, f"Callee child missing function_count: {child}"


# ══════════════════════════════════════════════════════════════════════════════
# 9. EXPAND — expand_file_node("backend/app/graph_builder.py", action="functions")
# ══════════════════════════════════════════════════════════════════════════════

class TestExpandGraphBuilderPy:
    @pytest.fixture(scope="class")
    def expanded(self, fn_dicts, fe_dicts):
        return module_abstractor.expand_file_node(
            fn_dicts, fe_dicts,
            file_path=M["gb"],
            action="functions",
        )

    def test_build_function_graph_exists_in_payload(self, fn_by_id):
        # expand caps at _MAX_CHILDREN=12; graph_builder.py has many helpers defined
        # before build_function_graph so it's cut from the visible children.
        # Assert against the payload directly instead.
        assert _has_fn("build_function_graph", M["gb"], fn_by_id)

    def test_parse_python_file_exists_in_payload(self, fn_by_id):
        assert _has_fn("parse_python_file", M["gb"], fn_by_id)

    def test_visitor_class_in_children(self, expanded):
        names = {c["label"] for c in expanded["children"]}
        assert "_Visitor" in names

    def test_internal_calls_edges(self, expanded):
        """graph_builder helpers call each other — expect intra-file CALLS edges."""
        calls = [e for e in expanded["edges"] if e["kind"] == "CALLS"]
        assert calls, "Expected intra-file CALLS edges in graph_builder.py"


# ══════════════════════════════════════════════════════════════════════════════
# 10. GRAPH completeness — all files reachable from main.py via CALLS
# ══════════════════════════════════════════════════════════════════════════════

class TestGraphReachability:
    """Core modules must be reachable from main.py via CALLS edges (BFS)."""

    def _reachable_files(
        self, start_file: str,
        fn_by_id: Dict[str, FunctionNode],
        calls_edges: Set[Tuple[str, str]],
    ) -> Set[str]:
        """BFS from all nodes in start_file over CALLS edges; collect file_paths visited."""
        start_ids = {n.id for n in fn_by_id.values() if n.file_path == start_file}
        visited_ids: Set[str] = set()
        queue = list(start_ids)
        while queue:
            nid = queue.pop()
            if nid in visited_ids:
                continue
            visited_ids.add(nid)
            for (src, tgt) in calls_edges:
                if src == nid and tgt not in visited_ids:
                    queue.append(tgt)
        return {
            fn_by_id[nid].file_path
            for nid in visited_ids
            if nid in fn_by_id and fn_by_id[nid].file_path
        }

    def test_pipeline_reachable_from_main(self, fn_by_id, calls_edges):
        reachable = self._reachable_files(M["main"], fn_by_id, calls_edges)
        assert M["pipeline"] in reachable

    def test_graph_builder_reachable_from_main(self, fn_by_id, calls_edges):
        reachable = self._reachable_files(M["main"], fn_by_id, calls_edges)
        assert M["gb"] in reachable

    def test_module_abstractor_reachable_from_main(self, fn_by_id, calls_edges):
        reachable = self._reachable_files(M["main"], fn_by_id, calls_edges)
        assert M["ma"] in reachable

    def test_retrieval_reachable_from_main(self, fn_by_id, calls_edges):
        reachable = self._reachable_files(M["main"], fn_by_id, calls_edges)
        assert M["ret"] in reachable

    def test_storage_reachable_from_main(self, fn_by_id, calls_edges):
        reachable = self._reachable_files(M["main"], fn_by_id, calls_edges)
        assert M["storage"] in reachable

    def test_llm_provider_reachable_from_main(self, fn_by_id, calls_edges):
        reachable = self._reachable_files(M["main"], fn_by_id, calls_edges)
        assert M["llm"] in reachable
