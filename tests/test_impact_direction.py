"""Impact is about who calls a function (upstream), and gaps in the graph must not read as safe."""
import sys
import textwrap
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import GraphPayload  # noqa: E402,F401  (models import must come first)
from app import graph_analytics, llm_provider, main, pipeline, storage, trace  # noqa: E402

REPO = "\n".join(
    [f"def user{i}():\n    return helper()\n" for i in range(12)]
    + ['''
def helper():
    return 1

def orchestrator():
    a1(); a2(); a3(); a4(); a5()

def a1(): pass
def a2(): pass
def a3(): pass
def a4(): pass
def a5(): pass

def truly_unused(): pass

class A:
    def hook_target(self): pass
class B:
    def hook_target(self): pass

def dispatch(o):
    o.hook_target()
'''])


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
    return pipeline.ingest(str(repo))


def _risk(graph, name):
    return main.get_impact(graph.graph_id, f"app.py::{name}")


def test_widely_called_leaf_outranks_wide_orchestrator(graph):
    helper, orch = _risk(graph, "helper"), _risk(graph, "orchestrator")
    assert helper.upstream_count == 12 and helper.risk_level == "high"
    assert orch.upstream_count == 0 and orch.risk_level == "low"  # calls 5 things, nobody calls it


def test_dispatch_target_is_not_reported_as_uncalled(graph):
    # `o.hook_target()` cannot be pinned to A or B: unseen, not unused
    dead = {d["name"] for d in graph_analytics.compute_analytics(graph)["dead_code"]}
    assert "truly_unused" in dead
    assert "hook_target" not in dead


def test_risk_never_lower_for_unconfirmed_callers_or_api():
    assert trace.risk_level(0, "medium", "CALLS", 0) == "low"
    assert trace.risk_level(0, "medium", "CALLS", 5) == "medium"
    assert trace.risk_level(0, "medium", "EXPOSES_API", 0) == "high"
    assert trace.risk_level(40, "low", "CALLS", 0) == "critical"
