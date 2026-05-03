import sys
from pathlib import Path

sys.path.insert(0, "backend")

from app.graph_builder import build_function_graph, parse_file
from app.models import FunctionEdge, FunctionNode, GraphPayload


def test_python_adapter_emits_canonical_cir(tmp_path: Path):
    source = tmp_path / "example.py"
    source.write_text(
        "\n".join(
            [
                "from functools import wraps",
                "",
                "def deco(fn):",
                "    return fn",
                "",
                "class Base:",
                "    pass",
                "",
                "@deco",
                "class Service(Base):",
                "    @deco",
                "    def handle(self, value: int) -> str:",
                "        return str(value)",
                "",
                "def run():",
                "    __import__('json')",
                "    svc = Service()",
                "    return svc.handle(1)",
            ]
        ),
        encoding="utf-8",
    )

    _, adapter_edges = parse_file(source, tmp_path)
    graph, nodes, edges = build_function_graph(str(tmp_path))
    by_name = {node.name: node for node in nodes}

    service = by_name["Service"]
    handle = by_name["handle"]
    run = by_name["run"]

    assert service.kind == "type"
    assert service.source_language == "python"
    assert service.adapter_metadata["decorators"] == ["deco"]
    assert service.adapter_metadata["bases"] == ["Base"]

    assert handle.kind == "function"
    assert handle.signature.type_system == "explicit"
    assert handle.signature.returns == "str"
    assert handle.adapter_metadata["decorators"] == ["deco"]

    assert run.kind == "function"
    assert any(edge.relationship == "INVOKES" for edge in edges)
    assert any(edge.relationship == "CONTAINS" for edge in edges)
    assert any(
        edge.relationship == "DEPENDS_ON"
        and edge.adapter_metadata.get("dependency_kind") == "dynamic_import"
        for edge in adapter_edges
    )
    assert all(data.get("relationship") for _, _, data in graph.edges(data=True))


def test_legacy_payloads_are_upgraded_to_cir_defaults():
    payload = GraphPayload(
        graph_id="g",
        repo_path=".",
        function_nodes=[
            FunctionNode(id="a.py::f", name="f", file_path="a.py", type="function"),
        ],
        function_edges=[
            FunctionEdge(type="CALLS", source_id="a.py::f", target_id="a.py::g"),
        ],
        module_nodes=[],
        module_edges=[],
        module_to_functions={},
    )

    assert payload.cir_version == "cir.v1"
    assert payload.function_nodes[0].kind == "function"
    assert payload.function_edges[0].relationship == "INVOKES"


def test_ingest_merges_structural_multilanguage_cir(tmp_path: Path):
    (tmp_path / "app.js").write_text(
        "\n".join(
            [
                "import { helper } from './helper.js';",
                "class Controller extends BaseController {",
                "  handle(value) {",
                "    return helper(value);",
                "  }",
                "}",
                "function start() {",
                "  return helper(1);",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "types.ts").write_text(
        "export const convert = (value: number): string => String(value);\n",
        encoding="utf-8",
    )
    (tmp_path / "Service.java").write_text(
        "\n".join(
            [
                "import java.util.List;",
                "public class Service extends BaseService {",
                "  public String handle(int value) {",
                "    return convert(value);",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "main.c").write_text(
        "\n".join(
            [
                "#include <stdio.h>",
                "int compute(int value) {",
                "  return value + 1;",
                "}",
                "int main() {",
                "  return compute(1);",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "widget.cpp").write_text(
        "\n".join(
            [
                "class Widget {",
                "};",
                "int render() {",
                "  return compute(1);",
                "}",
            ]
        ),
        encoding="utf-8",
    )

    _, nodes, edges = build_function_graph(str(tmp_path))
    languages = {node.source_language for node in nodes}
    names = {node.name for node in nodes}

    assert {"javascript", "typescript", "java", "c", "cpp"}.issubset(languages)
    assert {"Controller", "handle", "start", "convert", "Service", "compute", "main", "Widget", "render"}.issubset(names)
    assert all(node.kind in {"file", "type", "function", "callable", "external"} for node in nodes)
    assert any(edge.relationship == "DEPENDS_ON" for edge in edges)
    assert any(edge.relationship == "INVOKES" for edge in edges)
    assert any(edge.source_id.endswith("main") and edge.target_id.endswith("compute") for edge in edges)
