"""Test script to verify entry point detection."""
import sys
sys.path.insert(0, 'backend')

from app.bob_client import _heuristic_repo_analysis

# Test with the Classify repository
repo_path = r"D:\Documents\Thesis\Project_files\Classify"
result = _heuristic_repo_analysis(repo_path)

print("=== Repository Analysis Results ===")
print(f"Project Type: {result['project_type']}")
print(f"Domain: {result['domain']}")
print(f"Architecture: {result['architecture']}")
print(f"Tech Stack: {result['tech_stack']}")
print(f"Purpose: {result['purpose']}")
print(f"\nKey Entry Points Found: {len(result['key_entry_points'])}")
for ep in result['key_entry_points']:
    print(f"  - {ep}")

print(f"\nExpected Entry Points:")
print(f"  - main_parser.py")
print(f"  - leave_one_out.py")

if 'main_parser.py' in result['key_entry_points'] and 'leave_one_out.py' in result['key_entry_points']:
    print("\n[SUCCESS] Both expected entry points detected!")
    print("\nDetection strategies used:")
    print("  1. Exact filename matches (main.py, app.py, etc.)")
    print("  2. Pattern-based matches (files with 'main', 'run', etc. in name)")
    print("  3. Files with if __name__ == '__main__' block")
    print("  4. Executable scripts with shebang")
else:
    print("\n[FAILURE] Entry points not detected correctly")
    print("\nMissing entry points:")
    if 'main_parser.py' not in result['key_entry_points']:
        print("  - main_parser.py")
    if 'leave_one_out.py' not in result['key_entry_points']:
        print("  - leave_one_out.py")

# Made with Bob
