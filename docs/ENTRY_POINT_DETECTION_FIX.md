# Entry Point Detection Fix

## Problem

The user reported that entry points `main_parser.py` and `leave_one_out.py` were not being detected for the repository at `D:\Documents\Thesis\Project_files\Classify`.

## Root Cause

The heuristic entry point detection in [`_heuristic_repo_analysis()`](../backend/app/bob_client.py:238) only looked for exact filename matches:

```python
# OLD CODE (lines 238-241)
for pattern in ["main.py", "app.py", "__main__.py", "server.py", "index.js", "app.js"]:
    matches = list(repo_root.rglob(pattern))
    if matches:
        key_entry_points.extend([str(m.relative_to(repo_root)).replace("\\", "/") for m in matches[:3]])
```

This approach failed to detect:
- Files with patterns like `main_*.py`, `*_main.py`
- Files with `if __name__ == "__main__"` blocks
- Executable scripts with shebangs

## Solution

Implemented a **multi-strategy entry point detection** system with 4 complementary strategies:

### Strategy 1: Exact Filename Matches
Expanded list of common entry point filenames:
```python
["main.py", "app.py", "__main__.py", "server.py", "index.js", "app.js", "run.py", "start.py"]
```

### Strategy 2: Pattern-Based Matches
Detect files with keywords in their names:
```python
for py_file in repo_root.glob("*.py"):
    filename = py_file.name.lower()
    if any(keyword in filename for keyword in ["main", "run", "start", "execute", "launcher"]):
        entry_point_candidates.add(...)
```

This catches:
- `main_parser.py` ✅
- `leave_one_out.py` (if it has `__main__` block)
- `run_tests.py`
- `start_server.py`
- etc.

### Strategy 3: `__main__` Block Detection
Scan Python files for the entry point pattern:
```python
for py_file in repo_root.glob("*.py"):
    content = py_file.read_text(encoding="utf-8", errors="ignore")
    if 'if __name__' in content and '__main__' in content:
        entry_point_candidates.add(...)
```

### Strategy 4: Shebang Detection
Detect executable scripts:
```python
for py_file in repo_root.glob("*.py"):
    with open(py_file, 'rb') as f:
        first_line = f.readline()
        if first_line.startswith(b'#!') and (b'python' in first_line or b'env' in first_line):
            entry_point_candidates.add(...)
```

## Implementation

### Modified File
- `backend/app/bob_client.py` (lines 236-276)

### Changes
```python
# Find entry points - use multiple strategies
entry_point_candidates = set()

# Strategy 1: Exact filename matches
for pattern in ["main.py", "app.py", "__main__.py", "server.py", "index.js", "app.js", "run.py", "start.py"]:
    matches = list(repo_root.rglob(pattern))
    if matches:
        entry_point_candidates.update([str(m.relative_to(repo_root)).replace("\\", "/") for m in matches[:3]])

# Strategy 2: Pattern-based matches (files with 'main' in name)
for py_file in repo_root.glob("*.py"):
    filename = py_file.name.lower()
    if any(keyword in filename for keyword in ["main", "run", "start", "execute", "launcher"]):
        entry_point_candidates.add(str(py_file.relative_to(repo_root)).replace("\\", "/"))

# Strategy 3: Check for __main__ block in Python files (top-level only)
for py_file in repo_root.glob("*.py"):
    try:
        content = py_file.read_text(encoding="utf-8", errors="ignore")
        if 'if __name__' in content and '__main__' in content:
            entry_point_candidates.add(str(py_file.relative_to(repo_root)).replace("\\", "/"))
    except Exception:
        pass

# Strategy 4: Check for executable scripts (shebang)
for py_file in repo_root.glob("*.py"):
    try:
        with open(py_file, 'rb') as f:
            first_line = f.readline()
            if first_line.startswith(b'#!') and (b'python' in first_line or b'env' in first_line):
                entry_point_candidates.add(str(py_file.relative_to(repo_root)).replace("\\", "/"))
    except Exception:
        pass

key_entry_points = sorted(list(entry_point_candidates))[:10]  # Limit to top 10
```

## Validation

### Test Results

Created `test_entry_points.py` to validate the fix:

```bash
$ python test_entry_points.py

=== Repository Analysis Results ===
Project Type: unknown
Domain: machine_learning
Architecture: unknown
Tech Stack: []
Purpose: A unknown project

Key Entry Points Found: 5
  - Data_aug.py
  - classify_pipeline.py
  - leave_one_out.py          ✅
  - main_parser.py             ✅
  - micromotion_importance.py

Expected Entry Points:
  - main_parser.py
  - leave_one_out.py

[SUCCESS] Both expected entry points detected!
```

### Full Ingestion Test

Created `test_ingestion.py` to test end-to-end:

```bash
$ python test_ingestion.py

=== Ingestion Results ===
Graph ID: 85c395351300
Function Count: 369
Module Count: 34
Semantic Edges: 0

=== Repository Context (Phase 1) ===
Project Type: unknown
Domain: machine_learning
Architecture: unknown
Tech Stack: 
Purpose: A unknown project
Confidence: 0.5
Fallback Used: True

Key Entry Points (5):
  - Data_aug.py
  - classify_pipeline.py
  - leave_one_out.py          ✅
  - main_parser.py             ✅
  - micromotion_importance.py

[SUCCESS] Both expected entry points detected!
```

## Benefits

1. **More Comprehensive**: Detects entry points using 4 different strategies
2. **Pattern Matching**: Catches files like `main_*.py`, `*_main.py`, `run_*.py`
3. **Code Analysis**: Detects `if __name__ == "__main__"` blocks
4. **Executable Detection**: Identifies scripts with shebangs
5. **Backward Compatible**: Still detects all previously detected entry points
6. **Configurable**: Easy to add more patterns or strategies

## Impact

- **Before**: 0 entry points detected for Classify repository
- **After**: 5 entry points detected, including both expected ones
- **Detection Rate**: 100% for the test case

## Future Enhancements

Potential improvements:
1. Add scoring/ranking to prioritize more likely entry points
2. Detect entry points in subdirectories (currently only top-level)
3. Support for other languages (JavaScript, Java, etc.)
4. Machine learning-based entry point prediction
5. Integration with Bob API for smarter detection

## Related Files

- `backend/app/bob_client.py` - Entry point detection logic
- `test_entry_points.py` - Validation test
- `test_ingestion.py` - End-to-end test
- `docs/phase1_implementation.md` - Phase 1 documentation

## Conclusion

The entry point detection issue has been **resolved**. The system now uses a multi-strategy approach that successfully detects `main_parser.py` and `leave_one_out.py` as entry points, along with other potential entry points in the repository.

**Status**: ✅ **Fixed and Validated**