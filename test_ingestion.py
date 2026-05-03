"""Test script to verify ingestion with the Classify repository."""
import sys
import json
sys.path.insert(0, 'backend')

from app.pipeline import ingest

# Test ingestion with the Classify repository
repo_path = r"D:\Documents\Thesis\Project_files\Classify"

print("=== Testing Repository Ingestion ===")
print(f"Repository: {repo_path}")
print("\nStarting ingestion (this may take a few minutes)...\n")

try:
    result = ingest(repo_path)
    
    print("\n=== Ingestion Results ===")
    print(f"Graph ID: {result.graph_id}")
    print(f"Function Count: {len(result.function_nodes)}")
    print(f"Module Count: {len(result.module_nodes)}")
    print(f"Semantic Edges: {len(result.semantic_edges)}")
    
    # Load repository context from storage
    from app import storage
    from app.models import RepositoryContext
    
    repo_context_dict = storage.load_meta(result.graph_id, "repo_context")
    if repo_context_dict:
        print("\n=== Repository Context (Phase 1) ===")
        ctx = RepositoryContext(**repo_context_dict)
        print(f"Project Type: {ctx.project_type}")
        print(f"Domain: {ctx.domain}")
        print(f"Architecture: {ctx.architecture}")
        print(f"Tech Stack: {', '.join(ctx.tech_stack)}")
        print(f"Purpose: {ctx.purpose}")
        print(f"Confidence: {ctx.confidence}")
        print(f"Fallback Used: {ctx.fallback_used}")
        
        print(f"\nKey Entry Points ({len(ctx.key_entry_points)}):")
        for ep in ctx.key_entry_points:
            print(f"  - {ep}")
        
        # Check if our expected entry points are present
        entry_points = ctx.key_entry_points
        if 'main_parser.py' in entry_points and 'leave_one_out.py' in entry_points:
            print("\n[SUCCESS] Both expected entry points detected!")
        else:
            print("\n[WARNING] Expected entry points not all detected:")
            if 'main_parser.py' not in entry_points:
                print("  - Missing: main_parser.py")
            if 'leave_one_out.py' not in entry_points:
                print("  - Missing: leave_one_out.py")
    else:
        print("\n[WARNING] No repository context found in storage")
    
    print("\n=== Test Complete ===")
    print(f"Graph saved with ID: {result.graph_id}")
    print(f"You can now query this graph using the /query endpoint")
    
except Exception as e:
    print(f"\n[ERROR] Ingestion failed: {e}")
    import traceback
    traceback.print_exc()

# Made with Bob
