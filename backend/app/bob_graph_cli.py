"""CLI entrypoint for Bob/MCP graph generation.

Usage:
  python -m app.bob_graph_cli --repo-path /path/to/repo
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys

from .bob_export import build_bob_graph_response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a Flowify CIR graph for Bob.")
    parser.add_argument("--repo-path", required=True, help="Repository root to ingest.")
    parser.add_argument("--depth", type=int, default=3, choices=[1, 2, 3], help="Collapsed view depth.")
    parser.add_argument("--no-full-graph", action="store_true", help="Omit full GraphPayload from output.")
    parser.add_argument("--no-view", action="store_true", help="Omit collapsed graph view from output.")
    parser.add_argument("--no-llm-ingestion", action="store_true", help="Omit LLM ingestion metadata.")
    args = parser.parse_args(argv)

    try:
        with contextlib.redirect_stdout(sys.stderr):
            response = build_bob_graph_response(
                args.repo_path,
                depth=args.depth,
                include_full_graph=not args.no_full_graph,
                include_view=not args.no_view,
                include_llm_ingestion=not args.no_llm_ingestion,
            )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    json.dump(response, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
