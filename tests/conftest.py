"""Test isolation. Runs before any test module imports `app.*`.

- FLOWIFY_STORE -> a throwaway dir, so ingest tests never write to the dev store
  (storage.py reads it at import time).
- LLM_PROVIDER=heuristic -> no auto-detected Ollama/API calls; local and CI agree.
"""
import os
import tempfile

os.environ["FLOWIFY_STORE"] = tempfile.mkdtemp(prefix="flowify-test-store-")
os.environ["LLM_PROVIDER"] = "heuristic"
