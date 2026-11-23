"""Dry-run trace: source order, control-flow context, unresolved calls, cycles."""
import sys
import textwrap
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import GraphPayload  # noqa: E402,F401  (models import must come first)
from app import llm_provider, pipeline, storage, trace  # noqa: E402

REPO = '''
def main():
    setup()
    if cond():
        run()
    else:
        fallback()
    for x in y:
        step(x)
    try:
        risky()
    except Exception:
        recover()
    mystery.thing()
    thing.go()

def setup(): pass
def cond(): pass
def run(): return again()
def fallback(): pass
def step(x): pass
def risky(): pass
def recover(): pass
def again(): return run()
class A:
    def go(self): pass
class B:
    def go(self): pass
def a_b(): pass
def axb(): pass
'''


@pytest.fixture
def graph(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(storage, "_local", threading.local())
    monkeypatch.setenv("LLM_PROVIDER", "heuristic")
    monkeypatch.setattr(llm_provider, "_provider", None)
    trace._cache.clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(textwrap.dedent(REPO))
    return pipeline.ingest(str(repo)).graph_id


def _id(name):
    return f"app.py::{name}"


def test_steps_follow_source_order_and_tag_control_flow(graph):
    result = trace.dry_run(graph, _id("main"), fanout=8)
    top = [(s["name"], s["via"]["ctx"]) for s in result["steps"] if s["depth"] == 1]
    assert top == [
        ("setup", None), ("cond", None), ("run", "cond"), ("fallback", "cond"),
        ("step", "loop"), ("risky", "try"), ("recover", "except"),
    ]
    main = result["steps"][0]
    # `go` exists twice, so the graph cannot pick one; `mystery.thing` is not repo code at all
    assert [(u["callee"], u["candidates"]) for u in main["unresolved"]] == [("go", 2)]
    assert result["stats"]["unresolved_calls"] == 1

    # by default a node shows at most `fanout` callees and says how many it left out
    assert trace.dry_run(graph, _id("main"))["steps"][0]["hidden_calls"] == 1


def test_a_cycle_is_marked_instead_of_followed(graph):
    result = trace.dry_run(graph, _id("run"))
    names = [(s["name"], s.get("cycle", False)) for s in result["steps"]]
    assert names == [("run", False), ("again", False), ("run", True)]


def test_unknown_start_and_suggested_starts(graph):
    assert trace.dry_run(graph, "app.py::nope") is None
    assert trace.starts(graph)[0]["name"] == "main"


def test_search_treats_underscore_literally(graph):
    names = {r["name"] for r in storage.search_callables(graph, "a_b")}
    assert names == {"a_b"}  # not "axb"


def test_path_finds_the_shortest_chain_and_reports_a_missing_one(graph):
    found = trace.path(graph, _id("main"), _id("again"))
    assert [s["name"] for s in found["steps"]] == ["main", "run", "again"]
    assert found["stats"]["found"] and found["stats"]["alternatives"] == 1
    assert found["steps"][1]["via"]["ctx"] == "cond"

    none = trace.path(graph, _id("recover"), _id("main"))
    assert none["stats"]["found"] is False and len(none["steps"]) == 1
    assert trace.path(graph, "app.py::nope", _id("main")) is None


def test_impact_reports_callers_it_could_not_confirm(graph):
    from app import main

    impact = main.get_impact(graph, _id("run"))
    assert [c["name"] for c in impact.callers] == ["main", "again"]
    assert impact.callers[0]["confidence"] is not None  # shows how sure the graph is of each caller

    go = main.get_impact(graph, "app.py::A.go")
    assert go.caller_count == 0
    assert [(c["name"], c["candidates"]) for c in go.possible_callers] == [("main", 2)]  # `thing.go()` might be this one
    assert go.possible_caller_count == 1 and go.name_shared == 0


def test_a_name_many_functions_share_is_a_caveat_not_a_caller_count(graph, monkeypatch):
    from app import main

    monkeypatch.setattr(storage, "MAX_NAME_CANDIDATES", 1)  # `go` has 2 candidates: now "too common"
    go = main.get_impact(graph, "app.py::A.go")
    assert go.possible_caller_count == 0 and go.possible_callers == []
    assert go.name_shared == 2
    assert go.risk_level != "low"  # an uncheckable name must not read as safe
