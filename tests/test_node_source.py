"""The inspector's two endpoints: a function's source with real line numbers, and re-rooting the canvas on it."""
import sys
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import GraphPayload  # noqa: E402,F401  (models import must come first)
from app import llm_provider, main, pipeline, storage, trace  # noqa: E402

SRC = "import os\n\n\ndef helper():\n    return 1\n\n\ndef caller():\n    return helper()\n"


@pytest.fixture
def graph(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(storage, "_local", threading.local())
    monkeypatch.setattr(llm_provider, "_provider", None)
    trace._cache.clear()
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "app.py").write_text(SRC)
    return pipeline.ingest(str(tmp_path / "repo"))


def test_source_has_code_and_start_line(graph):
    d = main.get_node_source(graph.graph_id, "app.py::helper")
    assert d["code"].startswith("def helper()") and d["start_line"] == 4 and not d["truncated"]


def test_focus_returns_graph_node_shape(graph):
    n = main.get_focus_node(graph.graph_id, "app.py::caller")
    assert n["label"] == "caller" and n["file_path"] == "app.py"


def test_unknown_node_is_404(graph):
    for fn in (main.get_node_source, main.get_focus_node):
        with pytest.raises(HTTPException) as e:
            fn(graph.graph_id, "app.py::nope")
        assert e.value.status_code == 404
