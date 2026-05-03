import sys
from pathlib import Path

sys.path.insert(0, "backend")

from app import graph_builder, module_abstractor
from app.bob_client import _heuristic_repo_analysis


def test_python_m_invocations_are_authoritative_entry_points(tmp_path: Path):
    (tmp_path / "ml_model_training").mkdir()
    (tmp_path / "ts_model_training").mkdir()
    (tmp_path / "config").mkdir()

    (tmp_path / "train_ml_models.sh").write_text(
        "python -m ml_model_training.ml_main --action train\n",
        encoding="utf-8",
    )
    (tmp_path / "train_ts_models.sh").write_text(
        "template=\"python -m ts_model_training.main\"\n",
        encoding="utf-8",
    )
    (tmp_path / "ml_model_training" / "ml_main.py").write_text(
        "\n".join(
            [
                "import argparse",
                "from ml_model_training.data_loader import load",
                "",
                "def main():",
                "    parser = argparse.ArgumentParser()",
                "    parser.parse_args()",
                "    load()",
                "",
                "if __name__ == '__main__':",
                "    main()",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "ts_model_training" / "main.py").write_text(
        "\n".join(
            [
                "import argparse",
                "parser = argparse.ArgumentParser()",
                "args = parser.parse_args()",
                "EnvManager(args).train_full()",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "ml_model_training" / "data_loader.py").write_text(
        "def load():\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "ts_model_training" / "trainer.py").write_text(
        "class Trainer:\n    def train(self):\n        return None\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "constants.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    repo_context = _heuristic_repo_analysis(str(tmp_path))
    _, nodes, edges = graph_builder.build_function_graph(str(tmp_path))
    entry_nodes = module_abstractor.find_entry_files(
        {node.id: node.model_dump() for node in nodes},
        [edge.model_dump() for edge in edges],
        declared_entry_points=repo_context["key_entry_points"],
        max_count=4,
    )

    assert repo_context["key_entry_points"] == [
        "ml_model_training.ml_main",
        "ts_model_training.main",
    ]
    assert [node["label"] for node in entry_nodes] == [
        "ml_model_training.ml_main",
        "ts_model_training.main",
    ]
    assert "data_loader.py" not in {node["label"] for node in entry_nodes}
    assert "trainer.py" not in {node["label"] for node in entry_nodes}
    assert "constants.py" not in {node["label"] for node in entry_nodes}
