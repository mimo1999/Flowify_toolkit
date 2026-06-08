"""
Unit tests for MCP endpoints with mocked FastAPI calls.

These tests verify the MCP endpoint contracts without requiring
a running FastAPI server or actual repository ingestion.
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from fastapi.testclient import TestClient
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from app.main import app
from app.models import MCPIngestResponse, MCPQueryResponse


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_pipeline():
    """Mock pipeline module."""
    with patch('app.main.pipeline') as mock:
        yield mock


@pytest.fixture
def mock_storage():
    """Mock storage module."""
    with patch('app.main.storage') as mock:
        yield mock


@pytest.fixture
def mock_retrieval():
    """Mock retrieval module."""
    with patch('app.main.retrieval') as mock:
        yield mock


class TestMCPIngestEndpoint:
    """Test suite for /mcp/ingest endpoint."""
    
    def test_ingest_success_new_repo(self, client, mock_pipeline, mock_storage):
        """Test successful ingestion of a new repository."""
        # Setup mocks
        mock_payload = Mock()
        mock_payload.graph_id = "test-graph-123"
        mock_payload.repo_path = "/test/repo"
        mock_payload.function_nodes = [Mock() for _ in range(10)]
        mock_payload.module_nodes = [Mock() for _ in range(3)]
        
        mock_pipeline.ingest.return_value = mock_payload
        mock_storage.list_graphs.return_value = []
        mock_storage.load_meta.return_value = {
            "project_type": "web_api",
            "domain": "code_analysis",
            "architecture": "modular_pipeline",
            "purpose": "Test repository"
        }
        
        # Make request
        response = client.post(
            "/mcp/ingest",
            json={"repo_path": "/test/repo"}
        )
        
        # Assertions
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["graph_id"] == "test-graph-123"
        assert data["repo_path"] == "/test/repo"
        assert data["function_count"] == 10
        assert data["module_count"] == 3
        assert data["repo_context"] is not None
        assert data["repo_context"]["project_type"] == "web_api"
        
        # Verify mocks called
        mock_pipeline.ingest.assert_called_once_with("/test/repo")
        mock_storage.load_meta.assert_called_once_with("test-graph-123", "repo_context")
    
    def test_ingest_idempotent_existing_repo(self, client, mock_pipeline, mock_storage):
        """Test idempotent behavior - returns existing graph."""
        # Setup mocks
        existing_payload = Mock()
        existing_payload.graph_id = "existing-graph-456"
        existing_payload.repo_path = "/test/repo"
        existing_payload.function_nodes = [Mock() for _ in range(5)]
        existing_payload.module_nodes = [Mock() for _ in range(2)]
        
        mock_storage.list_graphs.return_value = ["existing-graph-456"]
        mock_storage.load.return_value = existing_payload
        mock_storage.load_meta.return_value = {"project_type": "library"}
        
        # Make request
        response = client.post(
            "/mcp/ingest",
            json={"repo_path": "/test/repo"}
        )
        
        # Assertions
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["graph_id"] == "existing-graph-456"
        assert data["function_count"] == 5
        
        # Verify pipeline.ingest was NOT called (idempotent)
        mock_pipeline.ingest.assert_not_called()
    
    def test_ingest_with_custom_repo_id(self, client, mock_pipeline, mock_storage):
        """Test ingestion with custom repo_id."""
        mock_payload = Mock()
        mock_payload.graph_id = "test-graph-789"
        mock_payload.repo_path = "/test/repo"
        mock_payload.function_nodes = []
        mock_payload.module_nodes = []
        
        mock_pipeline.ingest.return_value = mock_payload
        mock_storage.list_graphs.return_value = []
        mock_storage.load_meta.return_value = None
        
        response = client.post(
            "/mcp/ingest",
            json={"repo_path": "/test/repo", "repo_id": "custom-id-123"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["repo_id"] == "custom-id-123"
    
    def test_ingest_error_handling(self, client, mock_pipeline, mock_storage):
        """Test error handling during ingestion."""
        mock_storage.list_graphs.return_value = []
        mock_pipeline.ingest.side_effect = Exception("Ingestion failed")
        
        response = client.post(
            "/mcp/ingest",
            json={"repo_path": "/test/repo"}
        )
        
        assert response.status_code == 200  # Returns 200 with error in body
        data = response.json()
        assert data["success"] is False
        assert "Ingestion failed" in data["error"]
    
    def test_ingest_validation_missing_repo_path(self, client):
        """Test validation when repo_path is missing."""
        response = client.post(
            "/mcp/ingest",
            json={}
        )
        
        assert response.status_code == 422  # Validation error


def _make_func_node(i):
    """Helper: build a mock FunctionNode for test_query_limits_function_count.
    Defined at module level to avoid nested-function graph-builder edge issues."""
    n = Mock()
    n.id = f"func{i}"
    n.name = f"function_{i}"
    n.file_path = f"/src/file{i}.py"
    n.type = "function"
    n.summary = f"Function {i}"
    n.semantics = None
    n.adapter_metadata = {}
    return n


class TestMCPQueryEndpoint:
    """Test suite for /mcp/query endpoint."""

    def test_query_success(self, client, mock_storage, mock_retrieval):
        """Test successful query execution."""
        # Setup mocks
        mock_payload = Mock()
        mock_payload.graph_id = "test-graph-123"
        
        # Create mock function nodes
        mock_func1 = Mock()
        mock_func1.id = "func1"
        mock_func1.name = "authenticate_user"
        mock_func1.file_path = "/src/auth.py"
        mock_func1.type = "function"
        mock_func1.summary = "Authenticates user credentials"
        mock_func1.semantics = Mock()
        mock_func1.semantics.intent = "validation"
        mock_func1.semantics.complexity = "medium"
        mock_func1.semantics.criticality = "high"
        
        mock_func2 = Mock()
        mock_func2.id = "func2"
        mock_func2.name = "check_password"
        mock_func2.file_path = "/src/auth.py"
        mock_func2.type = "function"
        mock_func2.summary = "Verifies password hash"
        mock_func2.semantics = None
        
        mock_payload.function_nodes = [mock_func1, mock_func2]
        
        mock_storage.load.return_value = mock_payload
        import networkx as nx
        mock_retrieval.retrieve_subgraph.return_value = (
            ["func1", "func2"],
            {"nodes": [], "edges": []},
            "query-id-123",
            nx.DiGraph(),
        )
        mock_retrieval.explain.return_value = "Authentication flow explanation"
        
        # Make request
        response = client.post(
            "/mcp/query",
            json={
                "graph_id": "test-graph-123",
                "query": "how does authentication work?",
                "depth": 2
            }
        )
        
        # Assertions
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["graph_id"] == "test-graph-123"
        assert data["query"] == "how does authentication work?"
        assert "Authentication flow" in data["explanation"]
        assert len(data["relevant_functions"]) == 2
        assert data["relevant_functions"][0]["name"] == "authenticate_user"
        assert data["relevant_functions"][0]["intent"] == "validation"
        assert data["query_id"] == "query-id-123"
        
        # Verify mocks called
        mock_retrieval.retrieve_subgraph.assert_called_once()
        mock_retrieval.explain.assert_called_once()
    
    def test_query_graph_not_found(self, client, mock_storage):
        """Test query when graph doesn't exist."""
        mock_storage.load.return_value = None
        
        response = client.post(
            "/mcp/query",
            json={
                "graph_id": "nonexistent-graph",
                "query": "test query"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["error"].lower()
    
    def test_query_depth_validation(self, client, mock_storage, mock_retrieval):
        """Test depth parameter validation."""
        mock_payload = Mock()
        mock_payload.function_nodes = []
        mock_storage.load.return_value = mock_payload
        import networkx as nx
        mock_retrieval.retrieve_subgraph.return_value = ([], {}, "qid", nx.DiGraph())
        mock_retrieval.explain.return_value = "No results"
        
        # Test valid depth
        response = client.post(
            "/mcp/query",
            json={"graph_id": "test", "query": "test", "depth": 3}
        )
        assert response.status_code == 200
        
        # Test depth too high (should be clamped or rejected)
        response = client.post(
            "/mcp/query",
            json={"graph_id": "test", "query": "test", "depth": 10}
        )
        assert response.status_code == 422  # Validation error
    
    def test_query_error_handling(self, client, mock_storage, mock_retrieval):
        """Test error handling during query."""
        mock_payload = Mock()
        mock_storage.load.return_value = mock_payload
        mock_retrieval.retrieve_subgraph.side_effect = Exception("Query failed")
        
        response = client.post(
            "/mcp/query",
            json={"graph_id": "test", "query": "test"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Query failed" in data["error"]
    
    def test_query_validation_missing_fields(self, client):
        """Test validation when required fields are missing."""
        # Missing query
        response = client.post(
            "/mcp/query",
            json={"graph_id": "test"}
        )
        assert response.status_code == 422
        
        # Missing graph_id
        response = client.post(
            "/mcp/query",
            json={"query": "test"}
        )
        assert response.status_code == 422
    
    def test_query_limits_function_count(self, client, mock_storage, mock_retrieval):
        """Test that response limits function count to 20."""
        mock_payload = Mock()
        # Create 30 mock functions using the module-level helper.
        # (Nested function definitions cause graph-builder edge source issues in test_self_graph.)
        mock_payload.function_nodes = [_make_func_node(i) for i in range(30)]
        
        mock_storage.load.return_value = mock_payload
        import networkx as nx
        mock_retrieval.retrieve_subgraph.return_value = (
            [f"func{i}" for i in range(30)],
            {},
            "qid",
            nx.DiGraph(),
        )
        mock_retrieval.explain.return_value = "Explanation"
        
        response = client.post(
            "/mcp/query",
            json={"graph_id": "test", "query": "test"}
        )
        
        assert response.status_code == 200
        data = response.json()
        # Should limit to 20 functions
        assert len(data["relevant_functions"]) == 20
        assert len(data["execution_path"]) == 30  # Full path preserved


class TestRepoIdGeneration:
    """Test repo_id generation logic."""
    
    def test_repo_id_consistency(self, client, mock_pipeline, mock_storage):
        """Test that same repo_path generates same repo_id."""
        mock_payload = Mock()
        mock_payload.graph_id = "graph1"
        mock_payload.repo_path = "/test/repo"
        mock_payload.function_nodes = []
        mock_payload.module_nodes = []
        
        mock_pipeline.ingest.return_value = mock_payload
        mock_storage.list_graphs.return_value = []
        mock_storage.load_meta.return_value = None
        
        # First request
        response1 = client.post(
            "/mcp/ingest",
            json={"repo_path": "/test/repo"}
        )
        repo_id1 = response1.json()["repo_id"]
        
        # Second request with same path
        response2 = client.post(
            "/mcp/ingest",
            json={"repo_path": "/test/repo"}
        )
        repo_id2 = response2.json()["repo_id"]
        
        # Should generate same repo_id
        assert repo_id1 == repo_id2
        assert len(repo_id1) == 12  # SHA256 truncated to 12 chars


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

# Made with Bob
