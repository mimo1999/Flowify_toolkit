"""Unit tests for the knowledge layer (docs + rationale + provenance) and
graph analytics. No server required — runs the modules directly against a
synthetic fixture repo and against Flowify's own backend sources.

Run from the project root:  pytest tests/test_knowledge_analytics.py -v
"""
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import graph_analytics, knowledge  # noqa: E402
from app.graph_builder import build_function_graph  # noqa: E402
from app.models import GraphPayload, Provenance  # noqa: E402


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory):
    """A tiny repo with code, docs, and rationale comments."""
    repo = tmp_path_factory.mktemp("krepo")
    (repo / "auth.py").write_text(textwrap.dedent('''\
        def authenticate_user(token):
            # WHY: retries exist because the token service is eventually consistent
            return validate_token(token)

        def validate_token(token):
            # HACK: temporary workaround for bug #421
            return bool(token)

        def unused_helper_function():
            # TODO: wire this into the login flow once SSO ships
            return None
    '''))
    (repo / "README.md").write_text(textwrap.dedent('''\
        # Demo Project

        Authentication is handled by `authenticate_user` in [auth.py](auth.py).

        ## Architecture

        See [docs/adr/adr-001-auth.md](docs/adr/adr-001-auth.md) for the decision record.
        The validate_token helper checks JWTs.
    '''))
    adr_dir = repo / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "adr-001-auth.md").write_text(
        "# ADR-001: Token auth\n\nWe use `validate_token` for all requests.\n"
    )
    return str(repo)


@pytest.fixture(scope="module")
def fixture_payload(fixture_repo):
    g, nodes, edges = build_function_graph(fixture_repo)
    return GraphPayload(
        graph_id="test00000000", repo_path=fixture_repo,
        function_nodes=nodes, function_edges=edges,
        module_nodes=[], module_edges=[], module_to_functions={},
    )


class TestKnowledge:
    def test_documents_discovered_and_classified(self, fixture_repo, fixture_payload):
        kidx = knowledge.build_knowledge(fixture_repo, fixture_payload.function_nodes)
        by_type = {d.doc_type for d in kidx.documents}
        assert "readme" in by_type
        assert "adr" in by_type
        readme = next(d for d in kidx.documents if d.doc_type == "readme")
        assert readme.title == "Demo Project"
        assert any(s.heading == "Architecture" for s in readme.sections)

    def test_code_references_extracted_with_provenance(self, fixture_repo, fixture_payload):
        kidx = knowledge.build_knowledge(fixture_repo, fixture_payload.function_nodes)
        refs = [e for e in kidx.doc_edges if e.type == "REFERENCES"]
        targets = {e.target_id for e in refs}
        assert any("authenticate_user" in t for t in targets)
        assert any("validate_token" in t for t in targets)
        for e in refs:
            assert 0.0 <= e.provenance.confidence <= 1.0
            assert e.provenance.source == "regex"
            assert e.provenance.evidence
        # Backticked mention should outrank bare prose mention
        backticked = next(e for e in refs if "authenticate_user" in e.target_id)
        assert backticked.provenance.confidence >= 0.85

    def test_cross_document_links(self, fixture_repo, fixture_payload):
        kidx = knowledge.build_knowledge(fixture_repo, fixture_payload.function_nodes)
        links = [e for e in kidx.doc_edges if e.type == "LINKS_TO"]
        assert any("adr-001-auth.md" in e.target_id for e in links)

    def test_file_mentions(self, fixture_repo, fixture_payload):
        kidx = knowledge.build_knowledge(fixture_repo, fixture_payload.function_nodes)
        file_refs = [e for e in kidx.doc_edges if e.type == "MENTIONS_FILE"]
        assert any(e.target_id == "file::auth.py" for e in file_refs)

    def test_rationale_extraction_and_attachment(self, fixture_repo, fixture_payload):
        notes = knowledge.extract_rationale(fixture_repo, fixture_payload.function_nodes)
        markers = {n.marker for n in notes}
        assert {"WHY", "HACK", "TODO"} <= markers
        hack = next(n for n in notes if n.marker == "HACK")
        assert hack.attached_node_name == "validate_token"
        assert "421" in hack.text
        why = next(n for n in notes if n.marker == "WHY")
        assert why.attached_node_name == "authenticate_user"

    def test_edge_provenance_mapping(self):
        p = knowledge.edge_provenance({"adapter": "python_ast", "callee": "foo"})
        assert p.source == "ast" and p.confidence == 0.9
        p = knowledge.edge_provenance({"source": "pyan"})
        assert p.source == "static_analysis"
        p = knowledge.semantic_edge_provenance("bob", 0.63)
        assert p.source == "llm" and p.confidence == 0.63

    def test_knowledge_index_serializable(self, fixture_repo, fixture_payload):
        kidx = knowledge.build_knowledge(fixture_repo, fixture_payload.function_nodes)
        dumped = kidx.model_dump()
        assert isinstance(dumped["documents"], list)
        assert isinstance(dumped["rationale_notes"], list)


class TestGraphAnalytics:
    def test_analytics_bundle_shape(self, fixture_payload):
        a = graph_analytics.compute_analytics(fixture_payload)
        for key in ("stats", "god_nodes", "cycles", "bridges", "dead_code",
                    "surprising_couplings", "high_risk"):
            assert key in a
        assert a["stats"]["callable_nodes"] >= 3

    def test_dead_code_detects_unused(self, fixture_payload):
        a = graph_analytics.compute_analytics(fixture_payload)
        names = {d["name"] for d in a["dead_code"]}
        assert "unused_helper_function" in names
        # validate_token is called by authenticate_user → not dead
        assert "validate_token" not in names

    def test_god_nodes_ranked(self, fixture_payload):
        a = graph_analytics.compute_analytics(fixture_payload)
        assert a["god_nodes"], "expected at least one ranked node"
        scores = [g["score"] for g in a["god_nodes"]]
        assert scores == sorted(scores, reverse=True)

    def test_shortest_path(self, fixture_payload):
        path = graph_analytics.shortest_path(
            fixture_payload, "authenticate_user", "validate_token")
        assert path is not None
        assert [p["name"] for p in path] == ["authenticate_user", "validate_token"]

    def test_shortest_path_missing(self, fixture_payload):
        assert graph_analytics.shortest_path(fixture_payload, "nope", "validate_token") is None


class TestSelfRepo:
    """Run the layer over Flowify's own backend — a realistic corpus."""

    def test_self_knowledge(self):
        g, nodes, edges = build_function_graph(str(ROOT / "backend"))
        kidx = knowledge.build_knowledge(str(ROOT), nodes)
        assert any(d.doc_type == "readme" for d in kidx.documents)
        assert kidx.doc_edges, "README should reference at least one code symbol"

    def test_self_analytics(self):
        g, nodes, edges = build_function_graph(str(ROOT / "backend"))
        payload = GraphPayload(
            graph_id="selftest00000", repo_path=str(ROOT / "backend"),
            function_nodes=nodes, function_edges=edges,
            module_nodes=[], module_edges=[], module_to_functions={},
        )
        a = graph_analytics.compute_analytics(payload)
        assert a["stats"]["callable_nodes"] > 50
        assert len(a["god_nodes"]) == 20
        assert "python" in a["stats"]["languages"]
