"""PR impact report: which functions a diff changes and who depends on them.

    python backend/app/pr_impact.py --repo . --base origin/main > comment.md

Ingests the checked-out (head) tree, maps the diff's changed lines to functions,
walks the call graph upstream and prints a Markdown comment. Because the graph is
incomplete (see docs/accuracy.md) each function also lists *possible* callers the
graph could not pin down, so a missing caller reads as a warning, not as safety.
"""
import ast
import os
import re
import subprocess
import sys
import tempfile
from bisect import bisect_left
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import GraphPayload  # noqa: E402,F401  (models import must come first)
from app import change_risk  # noqa: E402
from app.trace import upstream  # noqa: E402

_HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.py$|\.test\.[jt]sx?$")
_is_test = lambda path: bool(_TEST_PATH.search(path or ""))  # noqa: E731
MAX_SHOWN = 15  # functions listed in the comment; the rest are counted


def changed_lines(base: str, repo: str, worktree: bool = False) -> tuple[dict, list]:
    """({file: [(start, end)]} on the head side, [deleted files]) for `base...HEAD`.

    `worktree=True` compares the working tree (uncommitted edits included) with the
    merge base instead, which is what a local "review my change" wants."""
    return parse_diff(_diff_text(base, repo, worktree))


def _diff_text(base: str, repo: str, worktree: bool) -> str:
    rev = ["--merge-base", base] if worktree else [f"{base}...HEAD"]
    return subprocess.run(["git", "diff", "-U0", "--no-color", "--relative", *rev], cwd=repo,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout


def removed_lines(out: str) -> dict:
    """`git diff -U0` text -> {file: [(new-side line the text sat at, [removed lines])]}:
    the "before" half of each hunk, which the line ranges from parse_diff do not carry."""
    files: dict = {}
    cur = hunk = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            cur, hunk = (None if line == "+++ /dev/null" else line[6:]), None
        elif cur and (m := _HUNK.match(line)):
            hunk = (int(m.group(1)), [])
            files.setdefault(cur, []).append(hunk)
        elif cur and hunk and line.startswith("-") and not line.startswith("--- "):
            hunk[1].append(line[1:])
    return {f: [h for h in hs if h[1]] for f, hs in files.items()}


_OLD_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+")


def old_ranges(out: str) -> dict:
    """`git diff -U0` text -> {old path: [(start, end)]} on the base side. Lets a branch be triaged
    against the base's graph, whose line numbers are the base's. A pure addition (count 0) touches
    the line it was inserted after."""
    files: dict = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("--- "):
            cur = None if line == "--- /dev/null" else line[6:]
        elif cur and (m := _OLD_HUNK.match(line)):
            start, count = int(m.group(1)), int(m.group(2) or 1)
            files.setdefault(cur, []).append((max(start, 1), max(start, 1) + max(count, 1) - 1))
    return files


def _chunk_hash(text: list, c: dict) -> str:
    """Stable id of a chunk's content: file, name, the changed lines and the removed lines. A
    reviewer's "checked" and an author's note are tied to it, so both go stale when the code changes."""
    import hashlib
    changed = [text[l - 1] for s, e in c["hunks"] for l in range(s, e + 1) if 0 < l <= len(text)]
    removed = [l for r in c["removed"] for l in r["lines"]]
    blob = "\n".join([c["file"], c["name"], *changed, "\x00", *removed])
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]


def _git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def parse_diff(out: str) -> tuple[dict, list]:
    """`git diff -U0` text -> ({file: [(start, end)]} on the new side, [deleted files])."""
    files: dict = {}
    deleted, cur = [], None
    for line in out.splitlines():
        if line.startswith("+++ "):
            cur = None if line == "+++ /dev/null" else line[6:]
            if cur:
                files.setdefault(cur, [])
        elif line.startswith("--- a/"):
            deleted.append(line[6:])  # dropped again below if the file survives
        elif cur and (m := _HUNK.match(line)):
            start, count = int(m.group(1)), int(m.group(2) or 1)
            # a pure deletion has count 0: it touches the line where the text used to be
            files[cur].append((start, start + max(count, 1) - 1))
    return files, [d for d in deleted if d not in files]


def _span(n):
    s = n.source_span
    if s and s.start_line:
        return s.start_line, s.end_line or s.start_line
    return (n.lineno, n.lineno) if n.lineno else None


def changed_functions(payload, files: dict) -> list:
    """Functions/methods whose span overlaps a changed hunk; innermost wins per hunk."""
    by_file: dict = {}
    for n in payload.function_nodes:
        if n.type in ("function", "method") and _span(n):
            by_file.setdefault(n.file_path, []).append(n)
    hit: dict = {}
    for path, hunks in files.items():
        for a, b in hunks:
            over = [n for n in by_file.get(path, []) if _span(n)[0] <= b and a <= _span(n)[1]]
            for n in over:
                s = _span(n)
                inner = any(m is not n and s[0] <= _span(m)[0] and _span(m)[1] <= s[1] and _span(m) != s for m in over)
                if not inner:
                    hit[n.id] = n
    return list(hit.values())


def analyze(payload, files: dict, graph_id: str | None = None, resolve=None) -> list:
    """One row per changed function, most-depended-on first. With a service resolver
    (change_risk.service_resolver) each row also carries `impact`, its impacted-services catalogue."""
    from app import storage
    nodes = {n.id: n for n in payload.function_nodes}
    callers: dict = {}
    meta: dict = {}
    for e in payload.function_edges:
        if e.relationship == "INVOKES" or e.type == "CALLS":
            callers.setdefault(e.target_id, []).append(e.source_id)
            meta[(e.source_id, e.target_id)] = e.adapter_metadata or {}
    rows = []
    for fn in changed_functions(payload, files):
        up = upstream(callers, fn.id)
        direct = [c for c in callers.get(fn.id, []) if c in nodes]
        possible, _, shared = storage.possible_callers(graph_id, fn.name, up | {fn.id}) if graph_id else (0, [], 0)
        rows.append({
            "id": fn.id, "name": fn.qualified_name or fn.name, "file": fn.file_path, "line": _span(fn)[0],
            # each caller carries its evidence: where the call is and why the graph linked it
            "direct": [{"id": c, "name": nodes[c].qualified_name or nodes[c].name, "file": nodes[c].file_path,
                        "line": meta.get((c, fn.id), {}).get("line"),
                        "rule": meta.get((c, fn.id), {}).get("resolution_reasoning"),
                        "confidence": meta.get((c, fn.id), {}).get("resolution_confidence")} for c in direct],
            "upstream": len(up),
            "endpoints": sorted({nodes[u].name for u in up if u in nodes
                                 and nodes[u].adapter_metadata.get("semantic_kind") == "EXPOSES_API"}),
            # test code is not "untested": nothing is expected to cover a test
            "tested": _is_test(fn.file_path) or any(_is_test(nodes[u].file_path) for u in up if u in nodes),
            "possible": possible, "name_shared": shared,
            **({"impact": change_risk.catalogue(up, nodes, resolve, _is_test, fn.file_path)} if resolve else {}),
        })
    return sorted(rows, key=lambda r: (-len(r["endpoints"]), -r["upstream"]))


def _importance(graph_id):
    """node id -> 0..1 percentile among the repo's nodes, or None when the graph has no analytics."""
    from app import graph_analytics, storage
    metrics = (storage.load_meta(graph_id, "analytics") or {}).get("node_metrics") if graph_id else None
    scores = graph_analytics.importance_scores(metrics) if metrics else {}
    return scores.get


def _assigned_names(st) -> list:
    targets = st.targets if isinstance(st, ast.Assign) else [st.target]
    return [x.id for t in targets for x in ast.walk(t) if isinstance(x, ast.Name)]


def module_chunks(payload, files: dict, before: dict) -> list:
    """Changed lines outside every function: module-level constants, config tables, class
    attributes. Python only (it needs the AST), not in test files. `names` are what the edit defines; `importers`
    are the files that import one of those names, `module_importers` those that import the whole
    module. Uses inside the same file are not found.
    ponytail: imports are matched on the dotted module's tail, so two modules with the same short
    name can be confused; the chunk is rated low-confidence for that reason anyway."""
    spans: dict = {}
    for n in payload.function_nodes:
        if n.type in ("function", "method") and _span(n):
            spans.setdefault(n.file_path, []).append(_span(n))
    imports = {n.file_path: n.adapter_metadata.get("import_map") or {} for n in payload.function_nodes
               if n.kind == "file" and n.file_path.endswith(".py")}
    out = []
    for path, hunks in files.items():
        if path not in imports or _is_test(path):  # test code is verification, not a risk to rank
            continue
        covered = {l for a, b in spans.get(path, []) for l in range(a, b + 1)}
        lines = sorted({l for a, b in hunks for l in range(a, b + 1)} - covered)
        if not lines:
            continue
        try:
            src = (Path(payload.repo_path) / path).read_text(encoding="utf-8-sig")
            tree = ast.parse(src)
        except (OSError, SyntaxError, ValueError):
            continue

        def touched(st) -> bool:
            i = bisect_left(lines, st.lineno)
            return i < len(lines) and lines[i] <= (st.end_lineno or st.lineno)

        names = set()
        for st in tree.body:
            if not touched(st):
                continue
            if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                names.update(_assigned_names(st))
            elif isinstance(st, ast.ClassDef):  # a changed field default: importers import the class
                if any(isinstance(x, (ast.Assign, ast.AnnAssign)) and touched(x) for x in st.body):
                    names.add(st.name)
        if not names:  # a changed import, docstring or bare call has no name an importer could depend on
            continue
        dotted = path[:-3].replace("/", ".")
        dotted = dotted[: -len(".__init__")] if dotted.endswith(".__init__") else dotted
        match = lambda mod: bool(mod) and (dotted == mod or dotted.endswith("." + mod))  # noqa: E731
        exact, whole = set(), set()
        for fp, imap in imports.items():
            if fp == path:
                continue
            for full in imap.values():
                mod, _, last = full.rpartition(".")
                if match(mod) and last in names:
                    exact.add(fp)
                elif match(full):
                    whole.add(fp)
        ranges, start = [], lines[0]
        for prev, cur in zip(lines, lines[1:] + [None]):
            if cur != prev + 1:
                ranges.append([start, prev])
                start = cur
        have = set(lines)
        text = src.splitlines()
        lo = max(1, lines[0] - 3)
        hi = min(len(text), lines[0] + 120, lines[-1] + 3)  # capped: a rewritten config table can be huge
        out.append({
            "excerpt": {"start_line": lo, "code": "\n".join(text[lo - 1:hi])},
            "id": f"{path}::<file>", "name": ", ".join(sorted(names)), "file": path,
            "line": lines[0], "hunks": ranges, "names": sorted(names), "lines_changed": len(lines),
            "removed": [{"at": at, "lines": ls} for at, ls in before.get(path, []) if at in have],
            "importers": sorted(exact), "module_importers": sorted(whole - exact),
        })
    return out


def score_row(r: dict, n, has_tests: bool, importance) -> dict:
    """Score one changed-function row in place. Needs `hunks` and `removed` on the row; `findings`
    and `history` are optional (the regression replay has no git checkout to compute them from)."""
    a, b = _span(n)
    changed = sum(e - s + 1 for s, e in r["hunks"])
    r.update(change_risk.score_chunk(
        upstream=r["upstream"], endpoints=len(r["endpoints"]),
        semantic_kind=n.adapter_metadata.get("semantic_kind", "CALLS"), has_tests=has_tests, tested=r["tested"],
        header_changed=any(s <= a <= e for s, e in r["hunks"]),  # the `def` line itself is in the diff
        new_code=changed >= 0.9 * (b - a + 1) and not r["removed"],  # all added, nothing replaced
        lines_changed=changed, importance=importance(r["id"]),
        services_crossed=r["impact"]["crosses"], possible=r["possible"], name_shared=r["name_shared"],
        findings=r.get("findings"), incidents=r.get("history")))
    return r


def mark_top(chunks: list) -> list:
    """Flag a change's TOP_N highest-scoring chunks: on the vLLM replay the culprit was in the top 3
    of its change 10 times in 13, where the absolute band caught 3."""
    for i, c in enumerate(sorted(chunks, key=lambda c: (-c["score"], c["name"]))):
        c["top"] = i < change_risk.TOP_N
    return chunks


def bucket(c: dict) -> str:
    """The review screen's bucket (ReviewPanel.jsx bucketOf): must / glance / skim."""
    if c.get("top") or c["band"] in ("critical", "high") or any(f["severity"] == "breaks" for f in c.get("findings") or []):
        return "must"
    return "glance" if c["band"] == "medium" else "skim"


def review(payload, base: str, committed: bool = False) -> dict:
    """The UI's "review a change": working tree vs `base`. One row per changed function and one per
    module-level edit, each with the lines that changed, a 0-100 risk score with its factors, the
    confidence in that score, and the catalogue of services, endpoints and tests it can reach,
    most risky first. `committed` compares `base...HEAD` (a PR) instead of the working tree.
    Raises CalledProcessError on a bad ref."""
    diff = _diff_text(base, payload.repo_path, worktree=not committed)
    files, deleted = parse_diff(diff)
    before = removed_lines(diff)
    config, config_error = change_risk.load_services(payload.repo_path)
    by_id = {n.id: n for n in payload.function_nodes}
    resolve = change_risk.service_resolver(
        [n.file_path for n in payload.function_nodes if n.kind == "file"], config, _is_test)
    has_tests = any(_is_test(n.file_path) for n in payload.function_nodes if n.kind == "file")
    importance = _importance(payload.graph_id)
    from app import contract_checks, history
    repo = payload.repo_path
    head = _git(repo, "rev-parse", "HEAD")
    src = contract_checks.Sources(repo, contract_checks.merge_base(repo, base))
    bases = contract_checks.class_bases(payload)
    incoming = Counter(e.target_id for e in payload.function_edges if e.relationship == "INVOKES" or e.type == "CALLS")
    past = history.incidents(repo, head) if head else {}

    rows = analyze(payload, files, payload.graph_id, resolve)
    changed_ids = {r["id"] for r in rows}
    for r in rows:
        n = by_id[r["id"]]
        a, b = _span(n)
        r["hunks"] = [[max(s, a), min(e, b)] for s, e in files.get(n.file_path, []) if s <= b and a <= e]
        r["removed"] = [{"at": at, "lines": ls} for at, ls in before.get(n.file_path, []) if a <= at <= b + 1]
        r["hash"] = _chunk_hash(src.head(n.file_path)[0], r)
        r["findings"] = contract_checks.check_function(src, n, r["direct"], bases, set(by_id), incoming)
        r["history"] = past.get(n.file_path)
        # callers that reach this chunk but were not changed with it: the "did you forget" list
        r["not_updated"] = [{**c, "is_test": _is_test(c["file"]), "file_in_diff": c["file"] in files}
                            for c in r["direct"] if c["id"] not in changed_ids]
        score_row(r, n, has_tests, importance)
        r["risk"] = r["band"]  # one vocabulary: the pointers' severity is the chunk's band
    rows.sort(key=lambda r: (-r["score"], r["name"]))

    modules = []
    for m in module_chunks(payload, files, before):
        users = sorted(set(m["importers"]) | set(m["module_importers"]))
        impact = change_risk.catalogue({f"{u}::<file>" for u in users}, by_id, resolve, _is_test, m["file"])
        m["upstream"] = len(users)
        m["tested"] = impact["tests"]["count"] > 0
        m["impact"] = impact
        m["hash"] = _chunk_hash(src.head(m["file"])[0], m)
        m["findings"] = contract_checks.check_module(src, m)
        m["history"] = past.get(m["file"])
        m["not_updated"] = []
        m.update(change_risk.score_chunk(
            upstream=len(users), has_tests=has_tests, tested=m["tested"], module_level=True,
            lines_changed=m["lines_changed"], services_crossed=impact["crosses"],
            findings=m["findings"], incidents=m["history"]))
        modules.append(m)
    modules.sort(key=lambda m: (-m["score"], m["name"]))
    mark_top(rows + modules)

    known = {n.file_path for n in payload.function_nodes}
    return {"graph_id": payload.graph_id, "base": base, "head": head, "dirty": not committed and bool(files),
            "functions": rows, "module_chunks": modules,
            "deleted": deleted, "pointers": pointers(rows, set(files)), "has_tests": has_tests,
            "services_config": {"found": bool(config), "error": config_error},
            "could_not_check": [{"id": r["id"], "name": r["name"], "shared_by": r["name_shared"]}
                                for r in rows if r["name_shared"]],
            "files_changed": len(files), "unmapped": [f for f in files if f not in known]}


def pointers(rows: list, changed_files: set) -> list:
    """Where to look, most useful first. Each one is a claim with its evidence:
      not_updated  a function that calls changed code but is not itself in the diff
      test         the same, when the caller is a test
      untested     a changed function no test reaches (only said when some other one is tested)
      reaches_api  a changed function an API endpoint depends on
    ponytail: direct callers only; a test three calls away is counted in `tested` but gets no
    pointer. Add transitive ones (with the call chain as evidence) if the replay shows misses."""
    changed = {r["id"] for r in rows}
    callers: dict = {}
    for r in rows:
        for c in r["direct"]:
            if c["id"] not in changed:
                p = callers.setdefault(c["id"], {
                    "kind": "not_updated", **c, "is_test": bool(_TEST_PATH.search(c["file"])),
                    "file_in_diff": c["file"] in changed_files, "targets": []})
                p["targets"].append({"id": r["id"], "name": r["name"]})
    # a caller in a file the PR never opened is the likeliest to have been forgotten; tests first
    out = sorted(callers.values(), key=lambda p: (p["file_in_diff"], not p["is_test"], -(p["confidence"] or 0)))
    if any(r["tested"] for r in rows):
        out += [{"kind": "untested", "id": r["id"], "name": r["name"], "file": r["file"], "line": r["line"]}
                for r in rows if not r["tested"]]
    out += [{"kind": "reaches_api", "id": r["id"], "name": r["name"], "file": r["file"], "line": r["line"],
             "endpoints": r["endpoints"]} for r in rows if r["endpoints"]]
    # Severity = the risk of the changed function the pointer is about (the worst one, for a
    # caller of several). Stable sort: within a severity the order above is kept.
    # A test that calls changed code is something to re-run or update, not a risk in itself:
    # it is its own kind, carries no severity and is listed after the code.
    risk = {r["id"]: r.get("risk", "low") for r in rows}
    for p in out:
        if p.get("is_test"):
            p["kind"] = "test"
        else:
            p["severity"] = min((risk[t["id"]] for t in p.get("targets", [p])), key=_SEVERITY.index)
    return sorted(out, key=lambda p: (p["kind"] == "test", _SEVERITY.index(p.get("severity", "low"))))


_SEVERITY = ["critical", "high", "medium", "low"]


def _reach(c: dict) -> str:
    imp = c["impact"]
    if c.get("importers") is not None:  # a module-level edit
        parts = [f"imported by {len(c['importers']) + len(c['module_importers'])} files"]
    else:
        parts = [f"{c['upstream']:,} dependent{'s' if c['upstream'] != 1 else ''}"] if c["upstream"] else ["no dependents"]
    if imp["endpoints"]["count"]:
        parts.append(f"{imp['endpoints']['count']} endpoint{'s' if imp['endpoints']['count'] != 1 else ''}")
    if imp["crosses"]:
        parts.append(f"{imp['crosses']} other service{'s' if imp['crosses'] != 1 else ''}")
    return " · ".join(parts)


def render(out: dict) -> str:
    """The PR comment: every change chunk ranked by risk score, then why."""
    chunks = sorted(out["functions"] + out["module_chunks"], key=lambda c: (-c["score"], c["name"]))
    if not chunks and not out["deleted"]:
        return "### Flowify impact\n\nNo functions changed" + (
            f" ({len(out['unmapped'])} non-code file(s) touched)." if out["unmapped"] else ".")
    eps = {e for r in out["functions"] for e in r["endpoints"]}
    crossed = {s["name"] for c in chunks for s in c["impact"]["services"] if s["name"] != c["impact"]["own_service"]}
    head = f"**{len(chunks)}** change{'s' if len(chunks) != 1 else ''} ranked by risk"
    head += f" · reaches **{len(eps)}** API endpoint{'s' if len(eps) != 1 else ''}" if eps else ""
    head += f" · touches **{len(crossed)}** other service{'s' if len(crossed) != 1 else ''}" if crossed else ""
    lines = ["### Flowify impact", "", head, "", "| # | Risk | Change | Reaches | Test in chain |", "|---|---|---|---|---|"]
    for i, c in enumerate(chunks[:MAX_SHOWN], 1):
        tested = "–" if not out["has_tests"] else "yes" if c["tested"] else "**no**"
        kind = " (module-level)" if c.get("importers") is not None else ""
        low = " ⚠" if c["confidence"]["level"] == "low" else ""
        lines.append(f"| {i} | **{c['score']} {c['band'].title()}**{low} | `{c['name']}`{kind} ({c['file']}:{c['line']}) "
                     f"| {_reach(c)} | {tested} |")
    if len(chunks) > MAX_SHOWN:
        lines.append(f"\n…and {len(chunks) - MAX_SHOWN} more.")
    lines += ["", "<details><summary>Why these scores</summary>", ""]
    for i, c in enumerate(chunks[:5], 1):
        why = ", ".join(f"{f['name']} +{f['points']} ({f['why']})" for f in c["factors"])
        lines.append(f"{i}. `{c['name']}`: {why}" + (f" — {'; '.join(c['confidence']['notes'])}" if c["confidence"]["notes"] else ""))
    lines += ["", "</details>"]
    if out["deleted"]:
        lines.append(f"\nDeleted file(s): {', '.join(f'`{d}`' for d in out['deleted'][:5])} — callers may now dangle.")
    lines += ["", "<sub>Risk is a 0-100 triage score; every point is a named factor. ⚠ = low confidence: the static call graph "
                  "resolves roughly half of real calls (see docs/accuracy.md), so incomplete evidence raises a floor and a short "
                  "list is a place to start, never proof of safety.</sub>"]
    return "\n".join(lines)


def main(argv: list) -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=".")
    p.add_argument("--base", required=True, help="base ref, e.g. origin/main")
    a = p.parse_args(argv)
    repo = str(Path(a.repo).resolve())
    import contextlib
    import shutil
    import threading
    from app import pipeline, storage
    # `storage` was imported (via trace) before this ran, so FLOWIFY_STORE is already read: point it
    # at a scratch directory directly, and remove that directory afterwards. (The old
    # `os.environ.setdefault("FLOWIFY_STORE", mkdtemp())` here changed nothing and leaked a folder.)
    scratch = None if "FLOWIFY_STORE" in os.environ else tempfile.mkdtemp(prefix="flowify_pr_")
    if scratch:
        storage._DB_PATH, storage._local = Path(scratch) / "flowify.db", threading.local()
    try:
        with contextlib.redirect_stdout(sys.stderr):  # ingest logs must not end up in the comment
            payload = pipeline.ingest(repo)
        sys.stdout.reconfigure(encoding="utf-8")
        print(render(review(payload, a.base, committed=True)))
    finally:
        if scratch:
            storage._conn().close()  # Windows cannot delete a sqlite file that is still open
            shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
