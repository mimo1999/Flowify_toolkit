"""Where do known bugs land, and does Claude find them?

    # 1. a clean vLLM git checkout (a `git worktree` of it is fine), then plant the five bugs as
    #    uncommitted edits; vllm_manifest.json in this folder is the answer key for that commit
    python benchmarks/ai_review/inject_vllm.py /path/to/vllm-copy benchmarks/ai_review/vllm_manifest.json
    python benchmarks/ai_review/verify_vllm.py /path/to/vllm-copy     # each planted bug actually misbehaves
    # 2. rank and bucket of each culprit chunk (free, deterministic)
    python benchmarks/ai_review/run.py /path/to/vllm-copy
    # 3. the same plus Claude's notes on the must-check chunks (costs API money)
    python benchmarks/ai_review/run.py /path/to/vllm-copy --ai

Step 2 is the calibration input for change_risk.BANDS: a culprit should land in "Must check".
Step 3 reports recall (a grounded note within 5 lines of the planted bug) and notes per clean chunk.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def main(argv) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("repo")
    p.add_argument("--manifest", default=str(Path(__file__).with_name("vllm_manifest.json")))
    p.add_argument("--ai", action="store_true", help="also ask Claude about the must-check chunks (costs money)")
    a = p.parse_args(argv)
    from app.models import GraphPayload  # noqa: F401  (models import must come first)
    from app import ai_review, contract_checks, pipeline, pr_impact, storage
    scratch = tempfile.mkdtemp(prefix="flowify_eval_")
    storage._DB_PATH, storage._local = Path(scratch) / "flowify.db", threading.local()
    try:
        payload = pipeline.ingest(a.repo)
        out = pr_impact.review(payload, "HEAD")
        chunks = sorted(out["functions"] + out["module_chunks"], key=lambda c: -c["score"])
        bucket = pr_impact.bucket
        manifest = json.loads(Path(a.manifest).read_text())

        def culprit(bug):
            hits = [c for c in chunks if c["file"] == bug["file"]
                    and any(s - 2 <= bug["line"] <= e + 2 for s, e in c["hunks"])]
            return hits[0] if hits else None

        rows = []
        for bug in manifest:
            c = culprit(bug)
            rows.append({"bug": bug["kind"], "file": f"{bug['file']}:{bug['line']}",
                         "rank": chunks.index(c) + 1 if c else None, "of": len(chunks),
                         "score": c and c["score"], "bucket": c and bucket(c),
                         "findings": c and [f["kind"] for f in c["findings"]]})
        must = [c for c in chunks if bucket(c) == "must"]
        report = {"chunks": len(chunks), "must_check": len(must), "bugs": rows}

        if a.ai:
            by_id = {n.id: n for n in payload.function_nodes}
            src = contract_checks.Sources(payload.repo_path, None)
            items = []
            for c in must:
                n = by_id.get(c["id"])
                span = pr_impact._span(n) if n else None
                text, allowed = ai_review.build_context(c, span, lambda path: src.head(path)[0], [])
                items.append((c["id"], text, allowed))
            res = ai_review.review_chunks(items)
            culprits = {culprit(b)["id"]: b for b in manifest if culprit(b)}
            found = sum(1 for cid, b in culprits.items() if any(
                abs(e["line"] - b["line"]) <= 5 for n in res["results"].get(cid, {}).get("notes", []) for e in n["evidence"]))
            clean = [r for cid, r in res["results"].items() if cid not in culprits]
            from collections import Counter
            statuses = Counter(r["status"] for r in res["results"].values())
            if statuses.get("ok", 0) == 0:
                errors = sorted({r.get("error", r["status"]) for r in res["results"].values()})
                report["ai"] = {"failed": f"no chunk was reviewed: {errors}", "statuses": dict(statuses)}
                print(json.dumps(report, indent=1))
                return 1
            report["ai"] = {"recall": f"{found}/{len(culprits)}", "statuses": dict(statuses), "usage": res["usage"],
                            "notes_per_clean_chunk": round(sum(len(r["notes"]) for r in clean) / max(1, len(clean)), 2),
                            "dropped": sum(r["dropped"] for r in res["results"].values())}
        print(json.dumps(report, indent=1))
    finally:
        storage._conn().close()
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
