"""Guards for the parts of ingestion whose cost must not grow with repo size."""
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import FunctionNode, FunctionEdge  # noqa: E402
from app import control_flow_analyzer, llm_ingestion  # noqa: E402


def _fn(name, code=""):
    return FunctionNode(
        id=f"m.py::{name}", name=name, file_path="m.py", type="function",
        source_language="python", code_snippet=textwrap.dedent(code),
    )


def _call(src, dst):
    return FunctionEdge(type="CALLS", relationship="INVOKES",
                        source_id=f"m.py::{src}", target_id=f"m.py::{dst}")


def test_control_flow_groups_only_calls_that_exist_inside_the_module():
    run = _fn("run", """
        def run(x):
            if x:
                helper()
            else:
                other()
    """)
    helper, other = _fn("helper"), _fn("other")
    by_id = {n.id: n for n in (run, helper, other)}
    calls = control_flow_analyzer.index_invocations([_call("run", "helper")])  # run -> other has no edge

    got = control_flow_analyzer.group_control_flow_functions(by_id, calls, list(by_id))
    assert got == {"m.py::run": {"conditional_branch": ["m.py::helper"]}}

    # a callee outside the module is ignored even though the edge exists
    got = control_flow_analyzer.group_control_flow_functions(by_id, calls, ["m.py::run", "m.py::other"])
    assert got == {}


def test_llm_ingestion_describes_only_the_most_connected_nodes():
    nodes = [_fn(f"f{i}") for i in range(50)]
    edges = [_call("f7", f"f{i}") for i in range(20) if i != 7]  # f7 is the hub
    top = llm_ingestion._top_connected(nodes, edges, limit=5)
    assert len(top) == 5 and top[0].name == "f7"
    assert llm_ingestion._top_connected(nodes, edges, limit=500) is nodes


def test_coarsen_merges_many_directories_into_a_dozen_lanes():
    from app import module_abstractor as ma

    dirs = {f"pkg/{a}/{b}": [f"pkg/{a}/{b}::f{i}" for i in range(3)] for a in "abcdefgh" for b in "wxyz"}
    dirs.update({f"tests/{a}": [f"tests/{a}::t"] for a in "abcdefgh"})
    dirs["setup.py"] = ["setup.py::s"]
    assert len(dirs) > 20

    lanes = ma._coarsen(dirs)
    assert len(lanes) <= 14
    flat = [nid for ids in lanes.values() for nid in ids]
    assert sorted(flat) == sorted(nid for ids in dirs.values() for nid in ids)  # nothing lost or duplicated

    small = {"src/a": ["x"], "src/b": ["y"]}
    assert ma._coarsen(small) is small  # already a readable size


def test_raw_edges_are_not_held_in_memory_while_parsing(tmp_path, monkeypatch):
    import gc
    import weakref

    from app import graph_builder

    for i in range(4):
        (tmp_path / f"m{i}.py").write_text(f"def f{i}():\n    g{i}()\n\ndef g{i}():\n    pass\n")
    parse, earlier, alive = graph_builder.parse_file, [], []

    def spy(path, root):
        gc.collect()
        alive.append(sum(r() is not None for r in earlier))
        nodes, edges = parse(path, root)
        earlier.extend(weakref.ref(e) for e in edges)
        return nodes, edges

    monkeypatch.setattr(graph_builder, "parse_file", spy)
    _, _, edges = graph_builder.build_function_graph(str(tmp_path))
    assert max(alive) <= 3  # at most the previous file's (two DEFINES, one CALLS)
    assert sum(e.type == "CALLS" for e in edges) == 4


def _saved_graph():
    from app import storage
    from app.models import GraphPayload, ModuleNode

    a, b = _fn("load_config"), _fn("read_file", "def read_file(): pass")
    payload = GraphPayload(
        graph_id=storage.new_graph_id(), repo_path="r", function_nodes=[a, b],
        function_edges=[_call("load_config", "read_file")],
        module_nodes=[ModuleNode(id="mod", name="m", description="", depth_level=1, linked_function_ids=[a.id, b.id])],
        module_edges=[], module_to_functions={"mod": [a.id, b.id]},
    )
    storage.save(payload)
    return payload


def _forbid_full_load(monkeypatch):
    from app import storage

    def fail(*_args, **_kw):
        raise AssertionError("loaded the whole graph")

    monkeypatch.setattr(storage, "load_light", fail)


def test_learning_does_not_reload_the_graph(monkeypatch):
    from app import learning

    p = _saved_graph()
    _forbid_full_load(monkeypatch)
    learning.seed_terminology_from_graph(p.graph_id, p)
    learning.record_query(p.graph_id, "configuration", ["m.py::load_config"])
    terms = learning.load_learning_insights(p.graph_id).terminology_map
    assert "load_config" in terms["configuration"].mapped_functions


def test_query_graph_does_not_copy_source_code():
    from app import retrieval

    g = retrieval._graph_from_payload(_saved_graph())
    assert all("code_snippet" not in d for _, d in g.nodes(data=True))


def test_graph_view_reads_only_what_it_shows_and_matches_a_full_load(monkeypatch):
    from app import main, module_abstractor, storage

    p = _saved_graph()
    # rows from older stores: a full load fills these in, so the lean read must as well
    with storage._tx() as cur:
        cur.execute("UPDATE nodes SET name=NULL, file_path=NULL, type=NULL WHERE graph_id=? AND id='m.py::read_file'", (p.graph_id,))
        cur.execute("UPDATE edges SET relationship=NULL WHERE graph_id=?", (p.graph_id,))
    full = storage.load_light(p.graph_id)
    nodes, edges = main._serialize_payload(full)
    cases = [(1, None), (2, None), (3, None), (3, 1)]
    expected = [
        module_abstractor.collapse_for_depth(
            full.module_nodes, full.module_edges, full.module_to_functions, nodes, depth, edges,
            tier1_budget=main._TIER1_BUDGET if tier else None,
        )
        for depth, tier in cases
    ]
    _forbid_full_load(monkeypatch)
    for (depth, tier), want in zip(cases, expected):
        got = main.get_graph(p.graph_id, depth, tier)
        assert {k: got[k] for k in want} == want
