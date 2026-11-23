import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from app.models import FunctionNode  # noqa: F401  (models import must come first)
from app import graph_analytics, main


def test_importance_scores_rank_central_nodes_higher():
    # Importance is a percentile, so it needs a population to rank against.
    metrics = {f"n{i}": {"pagerank": 0.001 * i, "in_degree": i // 4, "criticality": "low"} for i in range(40)}
    metrics.update({
        "hub": {"pagerank": 0.2, "in_degree": 10, "criticality": "high"},
        "mid": {"pagerank": 0.05, "in_degree": 3, "criticality": "low"},
        "leaf": {"pagerank": 0.0, "in_degree": 0, "criticality": "low"},
    })
    s = graph_analytics.importance_scores(metrics)
    assert s["hub"] > s["mid"] > s["leaf"]
    assert all(0.0 <= v <= 1.0 for v in s.values())
    assert graph_analytics.importance_level(s["hub"]) == "high"
    assert graph_analytics.importance_level(s["leaf"]) == "low"


def test_rank_callees_orders_caps_and_reports_hidden(monkeypatch):
    metrics = {f"f{i}": {"pagerank": i / 100, "in_degree": i, "criticality": "low"} for i in range(1, 6)}
    monkeypatch.setattr(main, "get_graph_analytics", lambda gid: {"node_metrics": metrics})
    result = {
        "children": [{"id": f"f{i}", "label": f"f{i}"} for i in [1, 2, 3, 4, 5, 3]],
        "edges": [{"id": f"e{i}", "source": "root", "target": f"f{i}"} for i in [1, 2, 3, 4, 5]],
    }
    main._rank_callees("g", result, limit=2)
    assert [c["id"] for c in result["children"]] == ["f5", "f4"]
    assert result["hidden"] == 3
    assert {e["target"] for e in result["edges"]} == {"f5", "f4"}

    result_all = {"children": [{"id": "f1", "label": "f1"}, {"id": "f2", "label": "f2"}], "edges": []}
    main._rank_callees("g", result_all, limit=0)
    assert result_all["hidden"] == 0 and len(result_all["children"]) == 2
