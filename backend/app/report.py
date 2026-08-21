"""Repository architecture report (GRAPH_REPORT.md) generation.

Assembles everything Flowify already knows about a graph — repo context, flow
summary, graph analytics, documentation index, and rationale notes — into a
single skimmable Markdown report a new developer can read before opening the
graph UI. Deterministic: no LLM calls beyond artifacts already cached at
ingest time.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from . import graph_analytics, storage
from .models import GraphPayload


def _h(level: int, text: str) -> str:
    return f"{'#' * level} {text}"


def _pct(count: int, total: int) -> str:
    return f"{100 * count / total:.0f}%" if total else "0%"


def _suggested_questions(analytics: dict, ctx: dict, knowledge: Optional[dict]) -> List[str]:
    """Derive graph-grounded starter questions for the query bar."""
    qs: List[str] = []
    for gn in (analytics.get("god_nodes") or [])[:3]:
        qs.append(f"What does `{gn['name']}` do and what depends on it?")
    kind_qs = {
        "web_api": ["Which functions expose API endpoints?", "How are requests validated?"],
        "cli_tool": ["What happens when the CLI is invoked?"],
        "data_pipeline": ["How does data flow from ingestion to output?"],
        "library": ["What is the public API surface of this library?"],
    }
    qs.extend(kind_qs.get(ctx.get("project_type", ""), []))
    if analytics.get("cycles"):
        first = analytics["cycles"][0]
        if first:
            qs.append(f"Why do `{first[0]['name']}` and `{first[-1]['name']}` depend on each other?")
    if knowledge and knowledge.get("rationale_notes"):
        hacks = [r for r in knowledge["rationale_notes"] if r["marker"] in ("HACK", "FIXME")]
        if hacks:
            qs.append(f"What is the workaround in `{hacks[0]['file_path']}` about?")
    qs.append("Where is data persisted?")
    return qs[:8]


def build_report(
    graph_id: str,
    payload: GraphPayload,
    analytics: Optional[dict] = None,
) -> str:
    """Render the Markdown architecture report for *graph_id*."""
    ctx = storage.load_meta(graph_id, "repo_context") or {}
    flow = storage.load_meta(graph_id, "flow_summary") or {}
    knowledge = storage.load_meta(graph_id, "knowledge")
    if analytics is None:
        analytics = storage.load_meta(graph_id, "analytics") or graph_analytics.compute_analytics(payload)

    stats = analytics.get("stats", {})
    lines: List[str] = []
    add = lines.append

    add(_h(1, "Repository Architecture Report"))
    add("")
    add(f"- **Repository**: `{payload.repo_path}`")
    add(f"- **Graph ID**: `{graph_id}`")
    add(f"- **Generated**: {datetime.utcnow().isoformat()}Z")
    add("")

    # ── Summary ────────────────────────────────────────────────────────────────
    add(_h(2, "Repository Summary"))
    add("")
    if flow.get("what"):
        add(flow["what"])
        add("")
    elif ctx.get("purpose"):
        add(ctx["purpose"])
        add("")
    add(f"- **Project type**: {ctx.get('project_type', 'unknown')}")
    add(f"- **Architecture style**: {ctx.get('architecture', 'unknown')}")
    add(f"- **Domain**: {ctx.get('domain', 'general')}")
    if ctx.get("tech_stack"):
        add(f"- **Tech stack**: {', '.join(ctx['tech_stack'])}")
    add(f"- **Functions / classes**: {stats.get('callable_nodes', '?')}  ·  "
        f"**Call edges**: {stats.get('call_edges', '?')}  ·  "
        f"**Modules**: {stats.get('modules', '?')}")
    add("")

    langs = stats.get("languages") or {}
    if langs:
        total = sum(langs.values())
        add(_h(3, "Language Distribution"))
        add("")
        add("| Language | Symbols | Share |")
        add("|---|---|---|")
        for lang, count in langs.items():
            add(f"| {lang} | {count} | {_pct(count, total)} |")
        add("")

    if ctx.get("key_entry_points"):
        add(_h(3, "Entry Points"))
        add("")
        for ep in ctx["key_entry_points"][:8]:
            add(f"- `{ep}`")
        add("")

    if flow.get("architecture_mermaid"):
        add(_h(2, "Architecture Diagram"))
        add("")
        add("```mermaid")
        add(flow["architecture_mermaid"])
        add("```")
        add("")

    if flow.get("flows"):
        add(_h(2, "Major Components"))
        add("")
        for f in flow["flows"]:
            add(f"{f['step']}. **{f['module']}** — {f.get('description', '')}")
        add("")

    # ── Most important nodes ───────────────────────────────────────────────────
    god = analytics.get("god_nodes") or []
    if god:
        add(_h(2, "Most Important Functions (by centrality)"))
        add("")
        add("| Rank | Function | File | In | Out | PageRank |")
        add("|---|---|---|---|---|---|")
        for i, gn in enumerate(god[:15], 1):
            add(f"| {i} | `{gn['name']}` | {gn['file_path']} | "
                f"{gn['in_degree']} | {gn['out_degree']} | {gn['pagerank']} |")
        add("")

    bridges = analytics.get("bridges") or []
    if bridges:
        add(_h(3, "Architectural Bottlenecks (critical bridges)"))
        add("")
        add("Removing any of these would disconnect parts of the call graph:")
        add("")
        for b in bridges[:8]:
            add(f"- `{b['name']}` ({b['file_path']}) — degree {b['degree']}")
        add("")

    # ── High risk ─────────────────────────────────────────────────────────────
    add(_h(2, "High-Risk Components"))
    add("")
    risk = analytics.get("high_risk") or []
    if risk:
        for r in risk[:10]:
            add(f"- `{r['name']}` ({r['file_path']}) — {'; '.join(r['reasons'])}")
    else:
        add("_No components matched multiple risk signals._")
    add("")

    cycles = analytics.get("cycles") or []
    if cycles:
        add(_h(3, "Circular Dependencies"))
        add("")
        for cyc in cycles[:10]:
            add("- " + " → ".join(f"`{n['name']}`" for n in cyc) + f" → `{cyc[0]['name']}`")
        add("")

    dead = analytics.get("dead_code") or []
    if dead:
        add(_h(3, "Dead Code Candidates (never called)"))
        add("")
        for d in dead[:15]:
            add(f"- `{d['name']}` ({d['file_path']})")
        if len(dead) > 15:
            add(f"- … and {len(dead) - 15} more")
        add("")

    surprising = analytics.get("surprising_couplings") or []
    if surprising:
        add(_h(3, "Surprising Cross-Module Couplings"))
        add("")
        add("Thin threads between otherwise-unrelated subsystems — worth reviewing:")
        add("")
        for s in surprising[:10]:
            add(f"- `{s['from_module']}` → `{s['to_module']}` "
                f"({s['edge_count']} edge{'s' if s['edge_count'] != 1 else ''}, "
                f"e.g. `{s['example']['source']}` → `{s['example']['target']}`)")
        add("")

    # ── Documentation & rationale ─────────────────────────────────────────────
    if knowledge:
        docs = knowledge.get("documents") or []
        if docs:
            add(_h(2, "Documentation Map"))
            add("")
            edge_counts = {}
            for e in knowledge.get("doc_edges") or []:
                edge_counts[e["source_id"]] = edge_counts.get(e["source_id"], 0) + 1
            for d in sorted(docs, key=lambda d: edge_counts.get(d["id"], 0), reverse=True)[:15]:
                refs = edge_counts.get(d["id"], 0)
                add(f"- **{d['title']}** (`{d['file_path']}`, {d['doc_type']}) — "
                    f"{refs} code reference{'s' if refs != 1 else ''}")
            add("")

        notes = knowledge.get("rationale_notes") or []
        important = [r for r in notes if r["marker"] in ("HACK", "FIXME", "WHY", "BUG", "WARNING")]
        if important:
            add(_h(2, "Developer Rationale Highlights"))
            add("")
            for r in important[:12]:
                loc = f"{r['file_path']}:{r['line']}"
                owner = f" (in `{r['attached_node_name']}`)" if r.get("attached_node_name") else ""
                add(f"- **{r['marker']}**{owner} — {r['text']}  \n  `{loc}`")
            add("")

    # ── Suggested questions ───────────────────────────────────────────────────
    add(_h(2, "Suggested Questions"))
    add("")
    for q in _suggested_questions(analytics, ctx, knowledge):
        add(f"- {q}")
    add("")
    add("---")
    add("_Generated by Flowify — graph-grounded, no hallucinated structure._")

    return "\n".join(lines)
