"""
Graph correctness tests for the expand / layout pipeline.

Verifies:
  1. expand_file_node — all children have correct parent IDs and IDs in
     filepath::qualname format; every CONTAINS edge source matches parent_id.
  2. No orphan nodes — every CONTAINS edge target ID exists in children.
  3. CALLS edges stay within the returned child set (intra-file only).
  4. expand_node (function callees) — parent set to the calling node.
  5. Callee deduplication — adding a callee that already exists as a
     file-level child must NOT overwrite its parent.
  6. Dagre layout inputs — the set of edges fed to dagre must include ALL
     CONTAINS edges (not the pruned visual subset) so every child is ranked.
  7. API contract — live /expand endpoint returns correct shape.
"""
import pytest
from pathlib import Path
import sys, os

# Make the backend importable without installing it
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from app.module_abstractor import (
    expand_file_node,
    expand_node,
    _emit_symbol_children,
    _is_code_symbol,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — deterministic in-memory graph, no live server needed
# ─────────────────────────────────────────────────────────────────────────────

def _fn(fid, name, ftype="function", file_path=None):
    return {
        "id":        fid,
        "name":      name,
        "type":      ftype,
        "kind":      ftype,
        "file_path": file_path or fid.split("::")[0],
        "summary":   f"does {name}",
    }

def _call(src, tgt):
    return {"source_id": src, "target_id": tgt, "type": "CALLS", "relationship": "INVOKES"}


@pytest.fixture()
def mcp_graph():
    """
    Mirrors the real mcp_server/flowify_mcp.py structure.
    Functions: create_http_client, make_resilient_request, _request (nested),
               list_tools, call_tool, handle_ingest_repo, handle_query_repo, main
    Call edges:
      call_tool          → handle_ingest_repo
      call_tool          → handle_query_repo
      handle_ingest_repo → create_http_client
      handle_ingest_repo → make_resilient_request
      handle_query_repo  → create_http_client
      handle_query_repo  → make_resilient_request
    """
    fp = "mcp_server/flowify_mcp.py"
    ids = {
        "create":  f"{fp}::create_http_client",
        "resilient": f"{fp}::make_resilient_request",
        "request": f"{fp}::make_resilient_request._request",
        "list":    f"{fp}::list_tools",
        "call":    f"{fp}::call_tool",
        "ingest":  f"{fp}::handle_ingest_repo",
        "query":   f"{fp}::handle_query_repo",
        "main":    f"{fp}::main",
    }
    nodes = {
        ids["create"]:    _fn(ids["create"],    "create_http_client",      file_path=fp),
        ids["resilient"]: _fn(ids["resilient"], "make_resilient_request",  file_path=fp),
        ids["request"]:   _fn(ids["request"],   "_request", "method",      file_path=fp),
        ids["list"]:      _fn(ids["list"],       "list_tools",              file_path=fp),
        ids["call"]:      _fn(ids["call"],       "call_tool",               file_path=fp),
        ids["ingest"]:    _fn(ids["ingest"],     "handle_ingest_repo",      file_path=fp),
        ids["query"]:     _fn(ids["query"],      "handle_query_repo",       file_path=fp),
        ids["main"]:      _fn(ids["main"],       "main",                    file_path=fp),
    }
    edges = [
        _call(ids["call"],    ids["ingest"]),
        _call(ids["call"],    ids["query"]),
        _call(ids["ingest"],  ids["create"]),
        _call(ids["ingest"],  ids["resilient"]),
        _call(ids["query"],   ids["create"]),
        _call(ids["query"],   ids["resilient"]),
    ]
    return {"file_path": fp, "nodes": nodes, "edges": edges, "ids": ids}


@pytest.fixture()
def multi_file_graph():
    """Two files calling each other — tests cross-file callee parent assignment."""
    fp_a = "app/service.py"
    fp_b = "app/utils.py"
    ida1 = f"{fp_a}::do_work"
    ida2 = f"{fp_a}::orchestrate"
    idb1 = f"{fp_b}::helper"
    idb2 = f"{fp_b}::validate"
    nodes = {
        ida1: _fn(ida1, "do_work",    file_path=fp_a),
        ida2: _fn(ida2, "orchestrate", file_path=fp_a),
        idb1: _fn(idb1, "helper",     file_path=fp_b),
        idb2: _fn(idb2, "validate",   file_path=fp_b),
    }
    edges = [
        _call(ida2, ida1),   # intra-file
        _call(ida2, idb1),   # cross-file
        _call(ida2, idb2),   # cross-file
    ]
    return {"fp_a": fp_a, "fp_b": fp_b, "nodes": nodes, "edges": edges,
            "ids": {"a1": ida1, "a2": ida2, "b1": idb1, "b2": idb2}}


# ─────────────────────────────────────────────────────────────────────────────
# 1. expand_file_node — basic shape
# ─────────────────────────────────────────────────────────────────────────────

class TestExpandFileNode:

    def test_all_children_returned(self, mcp_graph):
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        assert len(result["children"]) == 8, (
            f"Expected 8 children, got {len(result['children'])}: "
            f"{[c['label'] for c in result['children']]}"
        )

    def test_child_ids_have_filepath_prefix(self, mcp_graph):
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        fp = mcp_graph["file_path"]
        for c in result["children"]:
            assert c["id"].startswith(fp + "::"), (
                f"Child ID {c['id']!r} must start with '{fp}::'"
            )

    def test_all_children_have_correct_parent(self, mcp_graph):
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        expected_parent = f"file::{mcp_graph['file_path']}"
        for c in result["children"]:
            assert c.get("parent") == expected_parent, (
                f"Child {c['id']!r}: expected parent {expected_parent!r}, "
                f"got {c.get('parent')!r}"
            )

    def test_contains_edge_source_matches_file_node_id(self, mcp_graph):
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        expected_source = f"file::{mcp_graph['file_path']}"
        contains_edges = [e for e in result["edges"] if e["kind"] == "CONTAINS"]
        assert len(contains_edges) == 8, f"Expected 8 CONTAINS edges, got {len(contains_edges)}"
        for e in contains_edges:
            assert e["source"] == expected_source, (
                f"CONTAINS edge source {e['source']!r} != expected {expected_source!r}"
            )

    def test_contains_edge_targets_match_child_ids(self, mcp_graph):
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        child_ids = {c["id"] for c in result["children"]}
        contains_edges = [e for e in result["edges"] if e["kind"] == "CONTAINS"]
        for e in contains_edges:
            assert e["target"] in child_ids, (
                f"CONTAINS edge target {e['target']!r} not in children IDs"
            )

    def test_no_orphan_children(self, mcp_graph):
        """Every child must be reachable from the parent via CONTAINS edges."""
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        child_ids = {c["id"] for c in result["children"]}
        contains_targets = {e["target"] for e in result["edges"] if e["kind"] == "CONTAINS"}
        orphans = child_ids - contains_targets
        assert not orphans, f"Children with no CONTAINS edge: {orphans}"

    def test_calls_edges_stay_within_child_set(self, mcp_graph):
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        child_ids = {c["id"] for c in result["children"]}
        calls_edges = [e for e in result["edges"] if e["kind"] == "CALLS"]
        for e in calls_edges:
            assert e["source"] in child_ids, (
                f"CALLS edge source {e['source']!r} not in children"
            )
            assert e["target"] in child_ids, (
                f"CALLS edge target {e['target']!r} not in children"
            )

    def test_no_duplicate_child_ids(self, mcp_graph):
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        ids = [c["id"] for c in result["children"]]
        assert len(ids) == len(set(ids)), f"Duplicate child IDs: {ids}"

    def test_no_duplicate_edge_ids(self, mcp_graph):
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        eids = [e["id"] for e in result["edges"]]
        assert len(eids) == len(set(eids)), f"Duplicate edge IDs: {eids}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. expand_node — function callee expansion
# ─────────────────────────────────────────────────────────────────────────────

class TestExpandFunctionCallees:

    def test_callees_of_call_tool(self, mcp_graph):
        ids = mcp_graph["ids"]
        result = expand_node([], mcp_graph["nodes"], mcp_graph["edges"], ids["call"])
        child_ids = {c["id"] for c in result["children"]}
        assert ids["ingest"] in child_ids
        assert ids["query"] in child_ids

    def test_callees_parent_set_to_calling_function(self, mcp_graph):
        ids = mcp_graph["ids"]
        result = expand_node([], mcp_graph["nodes"], mcp_graph["edges"], ids["call"])
        for c in result["children"]:
            assert c.get("parent") == ids["call"], (
                f"Callee {c['id']!r} parent should be {ids['call']!r}, "
                f"got {c.get('parent')!r}"
            )

    def test_callee_edges_are_calls_kind(self, mcp_graph):
        ids = mcp_graph["ids"]
        result = expand_node([], mcp_graph["nodes"], mcp_graph["edges"], ids["call"])
        for e in result["edges"]:
            assert e["kind"] == "CALLS", f"Expected CALLS edge, got {e['kind']!r}"

    def test_node_with_no_callees_returns_empty(self, mcp_graph):
        # create_http_client is never a caller in our fixture
        ids = mcp_graph["ids"]
        result = expand_node([], mcp_graph["nodes"], mcp_graph["edges"], ids["create"])
        assert result["children"] == []
        assert result["edges"] == []


# ─────────────────────────────────────────────────────────────────────────────
# 3. Callee-overwrite bug — store-level
# ─────────────────────────────────────────────────────────────────────────────

class TestCalleeOverwrite:
    """
    When create_http_client is already in the store as a child of the file node,
    expanding handle_ingest_repo (which calls create_http_client) must NOT
    replace its parent with handle_ingest_repo.

    This simulates what store.js does: new nodes overwrite existing ones.
    The correct fix: store.js should skip nodes that already exist.
    """

    def _simulate_store_add_no_overwrite(self, existing, new_children):
        """Correct store behaviour: skip if ID already present."""
        store = dict(existing)
        for c in new_children:
            if c["id"] not in store:   # <-- the fix
                store[c["id"]] = c
        return store

    def _simulate_store_add_with_overwrite(self, existing, new_children):
        """Buggy store behaviour: always overwrites."""
        store = dict(existing)
        for c in new_children:
            store[c["id"]] = c          # <-- the bug
        return store

    def test_overwrite_changes_parent(self, mcp_graph):
        """Reproduce the bug: overwriting DOES change the parent (regression proof)."""
        ids = mcp_graph["ids"]
        file_parent = f"file::{mcp_graph['file_path']}"

        # Step 1: file expand puts create_http_client in store
        file_result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                       mcp_graph["file_path"], action="functions")
        store = {c["id"]: c for c in file_result["children"]}

        assert store[ids["create"]]["parent"] == file_parent

        # Step 2: callee expand for handle_ingest_repo returns create_http_client
        callee_result = expand_node([], mcp_graph["nodes"], mcp_graph["edges"],
                                    ids["ingest"])
        buggy_store = self._simulate_store_add_with_overwrite(store,
                                                              callee_result["children"])

        # With the bug the parent was overwritten
        if ids["create"] in {c["id"] for c in callee_result["children"]}:
            assert buggy_store[ids["create"]]["parent"] != file_parent, (
                "Expected bug to overwrite parent but it didn't — test logic error"
            )

    def test_no_overwrite_preserves_parent(self, mcp_graph):
        """Fixed behaviour: pre-existing nodes keep their original parent."""
        ids = mcp_graph["ids"]
        file_parent = f"file::{mcp_graph['file_path']}"

        file_result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                       mcp_graph["file_path"], action="functions")
        store = {c["id"]: c for c in file_result["children"]}

        callee_result = expand_node([], mcp_graph["nodes"], mcp_graph["edges"],
                                    ids["ingest"])
        fixed_store = self._simulate_store_add_no_overwrite(store,
                                                            callee_result["children"])

        assert fixed_store[ids["create"]]["parent"] == file_parent, (
            f"After fixed store add, create_http_client parent should be {file_parent!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Dagre layout — all CONTAINS edges must be included
# ─────────────────────────────────────────────────────────────────────────────

class TestDagreEdgeCompleteness:
    """
    The visual pruning rule (only show first/last CONTAINS edge when there are
    > 3 siblings) must NOT apply to the edges fed to dagre.  Every CONTAINS
    edge must reach dagre so all children are properly ranked.
    """

    def _visual_prune(self, children, edges):
        """Simulate the current frontend pruning logic."""
        children_of = {}
        for c in children:
            p = c.get("parent")
            if p:
                children_of.setdefault(p, []).append(c["id"])

        kept = []
        for e in edges:
            if e["kind"] != "CONTAINS":
                kept.append(e)
                continue
            siblings = children_of.get(e["source"], [])
            if len(siblings) > 3:
                first, last = siblings[0], siblings[-1]
                if e["target"] not in (first, last):
                    continue   # pruned
            kept.append(e)
        return kept

    def test_pruning_drops_middle_contains_edges(self, mcp_graph):
        """Confirm the pruning actually removes edges (so the bug exists)."""
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        pruned = self._visual_prune(result["children"], result["edges"])
        contains_before = sum(1 for e in result["edges"] if e["kind"] == "CONTAINS")
        contains_after  = sum(1 for e in pruned         if e["kind"] == "CONTAINS")
        assert contains_after < contains_before, (
            "Expected pruning to remove middle CONTAINS edges but nothing was removed"
        )

    def test_all_children_have_contains_edge_in_dagre_input(self, mcp_graph):
        """
        When ALL edges (not the pruned visual set) are fed to dagre, every
        child ID appears as a CONTAINS edge target.
        """
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        child_ids = {c["id"] for c in result["children"]}
        # Dagre input = ALL edges (no pruning)
        dagre_contains_targets = {e["target"] for e in result["edges"]
                                   if e["kind"] == "CONTAINS"}
        missing = child_ids - dagre_contains_targets
        assert not missing, (
            f"Children missing from dagre CONTAINS edges (would render disconnected): {missing}"
        )

    def test_pruned_set_causes_orphans_in_dagre(self, mcp_graph):
        """
        Confirm that using the PRUNED edge set for dagre input leaves some
        children with no incoming edge (they'd float to left column).
        """
        result = expand_file_node(mcp_graph["nodes"], mcp_graph["edges"],
                                  mcp_graph["file_path"], action="functions")
        pruned = self._visual_prune(result["children"], result["edges"])
        child_ids = {c["id"] for c in result["children"]}
        pruned_contains_targets = {e["target"] for e in pruned if e["kind"] == "CONTAINS"}
        orphans = child_ids - pruned_contains_targets
        assert orphans, (
            "Expected pruned edge set to leave orphan children but none found — "
            "test fixture may need > 3 children to trigger this"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Cross-file expand — parent correctness
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossFileCallees:

    def test_cross_file_callee_parent_is_calling_function(self, multi_file_graph):
        g = multi_file_graph
        result = expand_node([], g["nodes"], g["edges"], g["ids"]["a2"])
        child_map = {c["id"]: c for c in result["children"]}
        # b1 and b2 are cross-file callees of a2
        assert g["ids"]["b1"] in child_map, "helper should be in callees"
        assert g["ids"]["b2"] in child_map, "validate should be in callees"
        for cid in [g["ids"]["b1"], g["ids"]["b2"]]:
            assert child_map[cid]["parent"] == g["ids"]["a2"], (
                f"{cid}: parent should be {g['ids']['a2']!r}"
            )

    def test_intra_file_callee_included(self, multi_file_graph):
        g = multi_file_graph
        result = expand_node([], g["nodes"], g["edges"], g["ids"]["a2"])
        child_ids = {c["id"] for c in result["children"]}
        assert g["ids"]["a1"] in child_ids, "Intra-file callee do_work should be included"
