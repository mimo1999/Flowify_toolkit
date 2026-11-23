"""Risk score and impacted-services catalogue for one change chunk.

A chunk is a changed function, or a module-level edit (a constant, a config table). The
functions here are pure: no git, no storage, only data `pr_impact` already has.

The score is 0-100 and every point belongs to a named factor, so a reviewer can see why a chunk
ranks where it does. Confidence is reported separately and never lowers the score: thin evidence
raises a floor instead, so a gap in the graph cannot make a chunk read as "safe".
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

CONFIG_NAME = "flowify.services.json"

# Calibrated 2026-11 on 13 vLLM regressions (benchmarks/regressions, docs/change-risk.md): culprit
# chunks scored 29 to 61, so the old 70/50 cut-offs put 2 of 13 above "medium". The score separates
# poorly in absolute terms; rank within a change does better, which is why "Must check" also takes
# a change's top 3 (pr_impact.bucket).
BANDS = [(60, "critical"), (40, "high"), (30, "medium"), (0, "low")]
TOP_N = 3  # a change's highest-scoring chunks are always "Must check"
FLOOR = 30  # a chunk whose evidence is incomplete never rates below "medium"

_EXPOSURE = {"USES_DB": "touches the database", "EMITS_EVENT": "emits events", "CONSUMES_EVENT": "consumes events"}


def band(score: int) -> str:
    return next(name for cut, name in BANDS if score >= cut)


def score_chunk(*, upstream: int, endpoints: int = 0, semantic_kind: str = "CALLS", has_tests: bool = False,
                tested: bool = True, header_changed: bool = False, new_code: bool = False,
                module_level: bool = False, lines_changed: int = 1, importance: float | None = None,
                services_crossed: int = 0, possible: int = 0, name_shared: int = 0,
                findings: list | None = None, incidents: dict | None = None) -> dict:
    """{score, band, factors: [{name, points, why}], confidence: {level, notes}}.

    upstream    functions that depend on the chunk (for a module-level chunk: files importing it)
    importance  0..1 percentile of the function among the repo's nodes (graph_analytics), if known
    has_tests   the repo has tests at all; without any, "no test reaches it" says nothing
    findings    contract_checks findings for the chunk
    incidents   history.incidents entry for the chunk's file: {"count", "latest": [sha, subject]}
    """
    factors: list = []

    def add(name: str, points: int, why: str, cap: int) -> None:
        if points > 0:
            factors.append({"name": name, "points": points, "max": cap, "why": why})

    unit = "files import it" if module_level else "functions depend on it"
    add("reach", round(30 * min(1.0, math.log1p(upstream) / math.log1p(200))), f"{upstream:,} {unit}", 30)
    if endpoints:
        # scaled: a helper one endpoint uses is not in the same class as one half the API runs through
        add("exposure", min(20, 6 + 4 * endpoints), f"reached from {endpoints:,} API endpoint{'s' if endpoints != 1 else ''}", 20)
    elif semantic_kind == "EXPOSES_API":
        add("exposure", 20, "is itself an API endpoint", 20)
    elif semantic_kind in _EXPOSURE:
        add("exposure", 8, _EXPOSURE[semantic_kind], 20)
    if has_tests and not tested:
        add("coverage", 15, "no test in its caller chain" if not module_level else "no test imports it", 15)
    if module_level:
        add("kind", 8, "module-level constant or config changed", 15)
    elif new_code:  # before the header test: a new function's `def` line is trivially "changed"
        add("kind", 3, "new code, nothing depended on it before", 15)
    elif header_changed:
        add("kind", 15, "signature line changed, so callers may now be wrong", 15)
    else:
        add("kind", 5, "body edited", 15)
    add("size", round(8 * min(1.0, lines_changed / 40)), f"{lines_changed:,} lines changed", 8)
    if importance is not None:
        add("centrality", 6 if importance >= 0.95 else 3 if importance >= 0.80 else 0,
            f"in the top {round((1 - importance) * 100)}% most central in the repo", 6)
    add("crossings", min(6, 2 * services_crossed), f"used from {services_crossed} other service{'s' if services_crossed != 1 else ''}", 6)
    breaks = sum(1 for f in findings or [] if f["severity"] == "breaks")
    if breaks:
        add("contract", 10, f"{breaks} static check{'s' if breaks != 1 else ''} found code this breaks", 10)
    elif findings:
        add("contract", 4, f"{len(findings)} static check{'s' if len(findings) != 1 else ''} to look at", 10)
    if incidents and incidents.get("count"):
        n, (sha, subject) = incidents["count"], incidents["latest"]
        add("history", 8 if n >= 2 else 4,
            f"file touched by {n} revert/hotfix commit{'s' if n != 1 else ''} in 12 months (latest {sha} \"{subject}\")", 8)

    score = min(100, sum(f["points"] for f in factors))  # the factor maxima sum past 100

    notes = []
    if name_shared:
        notes.append(f"{name_shared:,} functions share this name, so calls by name alone are not attributed")
    if possible:
        notes.append(f"{possible:,} unconfirmed caller{'s' if possible != 1 else ''}")
    if module_level:
        notes.append("file-level evidence only: uses inside the same file are not listed")
    level = "low" if (name_shared or possible >= 10 or module_level) else "medium" if possible else "high"
    if level == "low" and score < FLOOR:
        add("uncertainty floor", FLOOR - score, "evidence is incomplete, so it is not rated low", FLOOR)
        score = FLOOR
    return {"score": score, "band": band(score), "factors": factors, "confidence": {"level": level, "notes": notes}}


# ── services ────────────────────────────────────────────────────────────────────────────────

def _glob(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def load_services(repo_path: str) -> tuple[list, str | None]:
    """The optional `flowify.services.json` at the repo root:
        {"services": [{"name": "scheduler", "paths": ["vllm/v1/core/**"], "owner": "@team"}]}
    Returns ([service...], error). A bad file is ignored and reported, never fatal.
    ponytail: JSON, not YAML: no new dependency (tomllib needs Python 3.11)."""
    p = Path(repo_path) / CONFIG_NAME
    if not p.is_file():
        return [], None
    try:
        out = []
        for s in json.loads(p.read_text(encoding="utf-8-sig"))["services"]:
            if not isinstance(s["name"], str) or not s["paths"] or not all(isinstance(g, str) for g in s["paths"]):
                raise ValueError("each service needs a name and a list of path globs")
            out.append({"name": s["name"], "owner": s.get("owner"), "res": [_glob(g) for g in s["paths"]]})
        return out, None
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [], f"{CONFIG_NAME} ignored: {e}"


def service_resolver(paths, config: list, is_test):
    """path -> (service name, owner). Config globs first, first match wins. Without a match, the
    service is the directory prefix at the smallest depth (at most 3) where the repo's non-test
    code splits into two or more groups, so a single top-level package is not one big service."""
    dirs = [tuple(p.split("/")[:-1]) for p in paths if "/" in p and not is_test(p)]  # root files are no group
    depth = 1
    while depth < 3 and len({d[:depth] for d in dirs}) < 2:
        depth += 1

    def resolve(path: str):
        for s in config:
            if any(r.match(path) for r in s["res"]):
                return s["name"], s["owner"]
        return "/".join(path.split("/")[:-1][:depth]) or ".", None

    return resolve


def catalogue(up, nodes: dict, resolve, is_test, own_path: str) -> dict:
    """What a chunk can reach, grouped for a reviewer: services (with dependents counted and a few
    examples), API endpoints, database access, events, tests. Lists are capped; counts are not."""
    services: dict = {}
    endpoints, db, events, tests, runnable = [], [], [], [], []
    for u in sorted(up):
        n = nodes.get(u)
        if n is None or not n.file_path:
            continue
        label = n.file_path if n.kind == "file" else (n.qualified_name or n.name)
        if is_test(n.file_path):
            tests.append(label)
            if n.name.startswith("test") and n.file_path.endswith(".py") and n.kind != "file":
                # a pytest node id, so the reaching tests can be run; uncapped on purpose
                runnable.append(f"{n.file_path}::{(n.qualified_name or n.name).replace('.', '::')}")
            continue
        name, owner = resolve(n.file_path)
        s = services.setdefault(name, {"name": name, "owner": owner, "dependents": 0, "examples": []})
        s["dependents"] += 1
        if len(s["examples"]) < 3:
            s["examples"].append(label)
        kind = n.adapter_metadata.get("semantic_kind")
        if kind == "EXPOSES_API":
            endpoints.append({"name": n.name, "file": n.file_path})
        elif kind == "USES_DB":
            db.append(label)
        elif kind in ("EMITS_EVENT", "CONSUMES_EVENT"):
            events.append(label)
    own = resolve(own_path)[0]
    ranked = sorted(services.values(), key=lambda s: (-s["dependents"], s["name"]))

    def cap(items: list, k: int = 10) -> dict:
        return {"count": len(items), "examples": items[:k]}

    return {"own_service": own, "services": ranked, "crosses": sum(1 for s in ranked if s["name"] != own),
            "endpoints": cap(endpoints), "db": cap(db), "events": cap(events),
            "tests": {**cap(tests, 5), "run": runnable}}
