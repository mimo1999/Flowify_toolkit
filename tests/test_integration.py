"""
Integration tests for FastAPI + MCP flow.

These tests verify the complete flow:
1. Start FastAPI server
2. Ingest repository via MCP endpoint
3. Query repository via MCP endpoint
4. Verify end-to-end behavior

Note: These tests require a running FastAPI server or use subprocess to start one.
"""
import pytest
import httpx
import asyncio
import time
from pathlib import Path
import tempfile
import shutil


# Test configuration
FASTAPI_URL = "http://localhost:8000"
TEST_TIMEOUT = 30.0


@pytest.fixture(scope="module")
def test_repo():
    """Create a temporary test repository with Python files."""
    temp_dir = tempfile.mkdtemp()
    repo_path = Path(temp_dir) / "test_repo"
    repo_path.mkdir()
    
    # Create simple Python files
    (repo_path / "main.py").write_text("""
def main():
    '''Main entry point'''
    result = process_data()
    return result

def process_data():
    '''Process some data'''
    return validate_input()

def validate_input():
    '''Validate input data'''
    return True
""")
    
    (repo_path / "utils.py").write_text("""
def helper_function():
    '''Helper utility'''
    return "helper"

def another_helper():
    '''Another helper'''
    return helper_function()
""")
    
    yield str(repo_path)
    
    # Cleanup
    shutil.rmtree(temp_dir)


@pytest.fixture(scope="module")
async def fastapi_client():
    """Create async HTTP client for FastAPI."""
    async with httpx.AsyncClient(
        base_url=FASTAPI_URL,
        timeout=TEST_TIMEOUT
    ) as client:
        # Wait for server to be ready
        for _ in range(10):
            try:
                response = await client.get("/")
                if response.status_code == 200:
                    break
            except httpx.ConnectError:
                await asyncio.sleep(1)
        else:
            pytest.skip("FastAPI server not available")
        
        yield client


@pytest.mark.asyncio
class TestIntegrationFlow:
    """Integration tests for complete ingest → query flow."""
    
    async def test_complete_flow(self, fastapi_client, test_repo):
        """Test complete flow: ingest → query → verify results."""
        
        # Step 1: Ingest repository
        ingest_response = await fastapi_client.post(
            "/mcp/ingest",
            json={"repo_path": test_repo}
        )
        
        assert ingest_response.status_code == 200
        ingest_data = ingest_response.json()
        assert ingest_data["success"] is True
        assert "graph_id" in ingest_data
        assert ingest_data["function_count"] > 0
        
        graph_id = ingest_data["graph_id"]
        print(f"✓ Ingested repository: graph_id={graph_id}")
        
        # Step 2: Query the repository
        query_response = await fastapi_client.post(
            "/mcp/query",
            json={
                "graph_id": graph_id,
                "query": "how does the main function work?",
                "depth": 2
            }
        )
        
        assert query_response.status_code == 200
        query_data = query_response.json()
        assert query_data["success"] is True
        assert len(query_data["relevant_functions"]) > 0
        assert len(query_data["explanation"]) > 0
        
        print(f"✓ Query returned {len(query_data['relevant_functions'])} functions")
        
        # Step 3: Verify function details
        functions = query_data["relevant_functions"]
        function_names = [f["name"] for f in functions]
        
        # Should find main-related functions
        assert any("main" in name.lower() for name in function_names), \
            f"Expected 'main' in function names, got: {function_names}"
        
        print(f"✓ Found relevant functions: {function_names}")
    
    async def test_idempotent_ingestion(self, fastapi_client, test_repo):
        """Test that re-ingesting same repo returns same graph_id."""
        
        # First ingestion
        response1 = await fastapi_client.post(
            "/mcp/ingest",
            json={"repo_path": test_repo}
        )
        data1 = response1.json()
        graph_id1 = data1["graph_id"]
        
        # Second ingestion (should be idempotent)
        response2 = await fastapi_client.post(
            "/mcp/ingest",
            json={"repo_path": test_repo}
        )
        data2 = response2.json()
        graph_id2 = data2["graph_id"]
        
        # Should return same graph_id
        assert graph_id1 == graph_id2
        print(f"✓ Idempotent ingestion verified: {graph_id1}")
    
    async def test_query_nonexistent_graph(self, fastapi_client):
        """Test querying a non-existent graph."""
        
        response = await fastapi_client.post(
            "/mcp/query",
            json={
                "graph_id": "nonexistent-graph-id",
                "query": "test query"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["error"].lower()
        print("✓ Non-existent graph handled correctly")
    
    async def test_query_depth_variations(self, fastapi_client, test_repo):
        """Test queries with different depth parameters."""
        
        # Ingest first
        ingest_response = await fastapi_client.post(
            "/mcp/ingest",
            json={"repo_path": test_repo}
        )
        graph_id = ingest_response.json()["graph_id"]
        
        # Test different depths
        for depth in [1, 2, 3]:
            response = await fastapi_client.post(
                "/mcp/query",
                json={
                    "graph_id": graph_id,
                    "query": "show me all functions",
                    "depth": depth
                }
            )
            
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            print(f"✓ Depth {depth}: {len(data['relevant_functions'])} functions")
    
    async def test_multiple_queries_same_graph(self, fastapi_client, test_repo):
        """Test multiple queries on the same graph."""
        
        # Ingest
        ingest_response = await fastapi_client.post(
            "/mcp/ingest",
            json={"repo_path": test_repo}
        )
        graph_id = ingest_response.json()["graph_id"]
        
        # Multiple queries
        queries = [
            "how does main work?",
            "what are the helper functions?",
            "show me validation logic"
        ]
        
        for query in queries:
            response = await fastapi_client.post(
                "/mcp/query",
                json={"graph_id": graph_id, "query": query}
            )
            
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            print(f"✓ Query '{query}': {len(data['relevant_functions'])} functions")


@pytest.mark.asyncio
class TestErrorHandling:
    """Test error handling in integration scenarios."""
    
    async def test_invalid_repo_path(self, fastapi_client):
        """Test ingestion with invalid repository path."""
        
        response = await fastapi_client.post(
            "/mcp/ingest",
            json={"repo_path": "/nonexistent/path/to/repo"}
        )
        
        assert response.status_code == 200
        data = response.json()
        # Should handle gracefully (may succeed with empty graph or fail)
        assert "graph_id" in data or "error" in data
        print("✓ Invalid repo path handled")
    
    async def test_malformed_requests(self, fastapi_client):
        """Test handling of malformed requests."""
        
        # Missing required fields
        response = await fastapi_client.post(
            "/mcp/ingest",
            json={}
        )
        assert response.status_code == 422  # Validation error
        
        response = await fastapi_client.post(
            "/mcp/query",
            json={"graph_id": "test"}  # Missing query
        )
        assert response.status_code == 422
        
        print("✓ Malformed requests rejected")


@pytest.mark.asyncio
class TestPerformance:
    """Basic performance tests."""
    
    async def test_query_response_time(self, fastapi_client, test_repo):
        """Test that queries complete within reasonable time."""
        
        # Ingest
        ingest_response = await fastapi_client.post(
            "/mcp/ingest",
            json={"repo_path": test_repo}
        )
        graph_id = ingest_response.json()["graph_id"]
        
        # Time query
        start = time.time()
        response = await fastapi_client.post(
            "/mcp/query",
            json={
                "graph_id": graph_id,
                "query": "test query"
            }
        )
        elapsed = time.time() - start
        
        assert response.status_code == 200
        assert elapsed < 5.0  # Should complete within 5 seconds
        print(f"✓ Query completed in {elapsed:.2f}s")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

# Made with Bob
