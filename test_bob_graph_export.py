import sys
from pathlib import Path

sys.path.insert(0, "backend")

from app.bob_export import build_bob_graph_response


def test_bob_graph_export_returns_generated_cir_graph(tmp_path: Path):
    (tmp_path / "main.py").write_text(
        "\n".join(
            [
                "def helper(value):",
                "    return value + 1",
                "",
                "def main():",
                "    return helper(1)",
            ]
        ),
        encoding="utf-8",
    )

    response = build_bob_graph_response(
        str(tmp_path),
        include_llm_ingestion=False,
    )

    assert response["schema_version"] == "bob.graph.v1"
    assert response["graph_id"]
    assert response["cir_version"] == "cir.v1"
    assert response["stats"]["node_count"] >= 3
    assert response["graph"]["function_nodes"]
    assert response["graph"]["function_edges"]
    assert response["view"]["nodes"]
    assert any(
        edge["relationship"] == "INVOKES"
        for edge in response["graph"]["function_edges"]
    )
