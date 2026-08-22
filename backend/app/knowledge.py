"""Documentation intelligence + rationale extraction (heterogeneous knowledge graph).

Turns the repository's documentation (README, ADRs, RFCs, guides, wikis) and the
developer intent buried in source comments (TODO / FIXME / HACK / WHY / NOTE)
into first-class graph entities, linked to the code nodes they reference.

Every link carries a Provenance record (source, confidence, evidence) so the UI
can distinguish "CALLS — 100% — AST" from "REFERENCES — 70% — regex match".

All extraction here is deterministic (regex + name matching) — no LLM calls —
so it adds negligible time to ingestion and can never fail it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .models import (
    DocumentNode, DocumentSection, FunctionNode, KnowledgeEdge, KnowledgeIndex,
    Provenance, RationaleNote,
)
from .ingestion import iter_repo_files

# ── Document discovery ─────────────────────────────────────────────────────────

_DOC_EXTENSIONS = {".md", ".markdown", ".rst", ".adoc"}
_MAX_DOC_BYTES = 512 * 1024          # skip pathological files
_MAX_DOCS = 200                      # cap per repo

_ADR_RE = re.compile(r"(^|/)(adrs?|architecture[-_]?decisions?|decisions)(/|$)|(^|/)adr[-_]?\d+", re.I)
_RFC_RE = re.compile(r"(^|/)rfcs?(/|$)|(^|/)rfc[-_]?\d+", re.I)
_WIKI_RE = re.compile(r"(^|/)(wiki|wikis)(/|$)", re.I)
_GUIDE_RE = re.compile(r"(^|/)(docs?|documentation|guides?|handbook)(/|$)", re.I)


def _classify_doc(rel_path: str) -> str:
    name = Path(rel_path).name.lower()
    if name.startswith("readme"):
        return "readme"
    if name.startswith("changelog") or name.startswith("history"):
        return "changelog"
    if name.startswith("contributing"):
        return "contributing"
    if _ADR_RE.search(rel_path):
        return "adr"
    if _RFC_RE.search(rel_path):
        return "rfc"
    if _WIKI_RE.search(rel_path):
        return "wiki"
    if _GUIDE_RE.search(rel_path):
        return "guide"
    return "doc"


def _iter_doc_files(repo_path: str) -> Iterator[Path]:
    return iter_repo_files(repo_path, _DOC_EXTENSIONS)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_CODE_SPAN_RE = re.compile(r"`([A-Za-z_][\w./]*(?:\(\))?)`")
# Bare identifiers that look like code: snake_case with underscore, or CamelCase
_IDENTIFIER_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")


def _parse_sections(lines: List[str]) -> List[DocumentSection]:
    sections: List[DocumentSection] = []
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            sections.append(DocumentSection(
                heading=m.group(2).strip(), level=len(m.group(1)), line=i + 1,
            ))
    return sections


def _doc_title(rel_path: str, sections: List[DocumentSection], lines: List[str]) -> str:
    for s in sections:
        if s.level == 1:
            return s.heading
    return Path(rel_path).stem.replace("_", " ").replace("-", " ").strip() or rel_path


def _doc_summary(lines: List[str]) -> str:
    """First non-heading, non-empty prose paragraph, truncated."""
    buf: List[str] = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith("#"):
            if buf:
                break
            continue
        buf.append(stripped)
        if sum(len(b) for b in buf) > 240:
            break
    return " ".join(buf)[:300]


def _build_code_index(
    function_nodes: List[FunctionNode],
) -> Tuple[Dict[str, List[str]], set]:
    """Return (name → node ids, set of repo file paths)."""
    name_index: Dict[str, List[str]] = {}
    file_paths: set = set()
    for n in function_nodes:
        if n.kind == "external":
            continue
        if n.file_path:
            file_paths.add(n.file_path)
        if n.kind == "file":
            continue
        # Skip very short/common names — they'd match everyday prose
        if len(n.name) >= 4:
            name_index.setdefault(n.name, []).append(n.id)
    return name_index, file_paths


def _extract_doc_edges(
    doc_id: str,
    rel_path: str,
    lines: List[str],
    sections: List[DocumentSection],
    name_index: Dict[str, List[str]],
    file_paths: set,
    doc_ids_by_path: Dict[str, str],
) -> List[KnowledgeEdge]:
    """Find code mentions and cross-document links in a document."""
    edges: List[KnowledgeEdge] = []
    seen: set = set()

    def section_at(line_no: int) -> Optional[str]:
        current = None
        for s in sections:
            if s.line <= line_no:
                current = s.heading
            else:
                break
        return current

    def add(edge_type: str, target: str, line_no: int, evidence: str,
            confidence: float, reasoning: str) -> None:
        key = (edge_type, target)
        if key in seen:
            return
        seen.add(key)
        edges.append(KnowledgeEdge(
            type=edge_type,
            source_id=doc_id,
            target_id=target,
            section=section_at(line_no),
            line=line_no,
            provenance=Provenance(
                source="regex", confidence=confidence,
                evidence=evidence.strip()[:200], reasoning=reasoning,
            ),
        ))

    doc_dir = str(Path(rel_path).parent).replace("\\", "/")
    in_fence = False
    for i, line in enumerate(lines):
        line_no = i + 1
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        # Markdown links → other docs or repo files
        for m in _MD_LINK_RE.finditer(line):
            href = m.group(2).split("#")[0]
            if not href or href.startswith(("http://", "https://", "mailto:")):
                continue
            # Resolve relative to the doc's directory
            candidate = os.path.normpath(
                os.path.join(doc_dir, href) if doc_dir not in (".", "") else href
            ).replace("\\", "/").lstrip("./")
            if candidate in doc_ids_by_path:
                add("LINKS_TO", doc_ids_by_path[candidate], line_no, line, 0.95,
                    "Markdown link to another document")
            elif candidate in file_paths:
                add("MENTIONS_FILE", f"file::{candidate}", line_no, line, 0.95,
                    "Markdown link to a source file")

        # Backticked code spans → high-confidence code references
        for m in _CODE_SPAN_RE.finditer(line):
            token = m.group(1).rstrip("()")
            short = token.split(".")[-1].split("/")[-1]
            if token in file_paths:
                add("MENTIONS_FILE", f"file::{token}", line_no, line, 0.9,
                    "Backticked file path")
                continue
            for nid in name_index.get(short, [])[:3]:
                add("REFERENCES", nid, line_no, line, 0.85,
                    f"Backticked identifier `{token}`")

        # Bare identifiers in prose → lower-confidence references
        for m in _IDENTIFIER_RE.finditer(line):
            token = m.group(1)
            for nid in name_index.get(token, [])[:2]:
                add("REFERENCES", nid, line_no, line, 0.6,
                    f"Identifier '{token}' mentioned in prose")

        # Plain file-path mentions
        for fp in _FILE_MENTION_RE.findall(line):
            if fp in file_paths:
                add("MENTIONS_FILE", f"file::{fp}", line_no, line, 0.8,
                    "File path mentioned in prose")

    return edges


_FILE_MENTION_RE = re.compile(r"\b([\w./-]+\.(?:py|js|jsx|ts|tsx|java|c|cc|cpp|h|hpp))\b")


# ── Rationale extraction ───────────────────────────────────────────────────────

_MARKERS = ("TODO", "FIXME", "HACK", "NOTE", "WHY", "XXX", "BUG", "WARNING", "OPTIMIZE")
_RATIONALE_RE = re.compile(
    r"(?:#|//|/\*|\*|<!--)\s*(" + "|".join(_MARKERS) + r")\b[:\s-]*(.+)", re.I
)
_MAX_RATIONALE = 500


def extract_rationale(
    repo_path: str,
    function_nodes: List[FunctionNode],
) -> List[RationaleNote]:
    """Scan source files for intent markers and attach each to its enclosing node.

    Attachment uses the CIR source spans: the innermost function/class whose span
    contains the comment line wins. Comments outside any span stay file-level.
    """
    # file → [(start, end, node)] sorted by span size (innermost match wins)
    spans: Dict[str, List[Tuple[int, int, FunctionNode]]] = {}
    files: set = set()
    for n in function_nodes:
        if not n.file_path or n.kind in ("external", "file"):
            if n.kind == "file" and n.file_path:
                files.add(n.file_path)
            continue
        files.add(n.file_path)
        start = n.source_span.start_line if n.source_span and n.source_span.start_line else n.lineno
        end = n.source_span.end_line if n.source_span and n.source_span.end_line else start
        if start:
            spans.setdefault(n.file_path, []).append((start, end or start, n))
    for fp in spans:
        spans[fp].sort(key=lambda t: (t[1] - t[0]))  # innermost first

    root = Path(repo_path).resolve()
    notes: List[RationaleNote] = []
    for rel in sorted(files):
        if len(notes) >= _MAX_RATIONALE:
            break
        abs_path = root / rel
        try:
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines()):
            m = _RATIONALE_RE.search(line)
            if not m:
                continue
            marker = m.group(1).upper()
            body = m.group(2).strip().rstrip("*/-> ").strip()
            if len(body) < 8:        # "TODO" with no useful text
                continue
            line_no = i + 1
            attached: Optional[FunctionNode] = None
            for start, end, node in spans.get(rel, []):
                if start <= line_no <= end:
                    attached = node
                    break
            notes.append(RationaleNote(
                id=f"rationale::{rel}::{line_no}",
                marker=marker,
                text=body[:300],
                file_path=rel,
                line=line_no,
                attached_node_id=attached.id if attached else None,
                attached_node_name=attached.name if attached else None,
            ))
            if len(notes) >= _MAX_RATIONALE:
                break
    return notes


# ── Public API ─────────────────────────────────────────────────────────────────

def build_knowledge(repo_path: str, function_nodes: List[FunctionNode]) -> KnowledgeIndex:
    """Build the document + rationale knowledge layer for a repository."""
    root = Path(repo_path).resolve()
    name_index, file_paths = _build_code_index(function_nodes)

    # First pass: register all documents so cross-doc links can resolve
    doc_files: List[Tuple[str, List[str]]] = []
    doc_ids_by_path: Dict[str, str] = {}
    for fp in sorted(_iter_doc_files(repo_path))[:_MAX_DOCS]:
        try:
            if fp.stat().st_size > _MAX_DOC_BYTES:
                continue
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(fp.relative_to(root)).replace("\\", "/")
        doc_files.append((rel, text.splitlines()))
        doc_ids_by_path[rel] = f"doc::{rel}"

    documents: List[DocumentNode] = []
    doc_edges: List[KnowledgeEdge] = []
    for rel, lines in doc_files:
        sections = _parse_sections(lines)
        documents.append(DocumentNode(
            id=doc_ids_by_path[rel],
            title=_doc_title(rel, sections, lines),
            file_path=rel,
            doc_type=_classify_doc(rel),
            sections=sections[:50],
            summary=_doc_summary(lines),
            word_count=sum(len(l.split()) for l in lines),
        ))
        doc_edges.extend(_extract_doc_edges(
            doc_ids_by_path[rel], rel, lines, sections,
            name_index, file_paths, doc_ids_by_path,
        ))

    rationale = extract_rationale(repo_path, function_nodes)
    return KnowledgeIndex(documents=documents, doc_edges=doc_edges, rationale_notes=rationale)


# ── Provenance for existing edge kinds ─────────────────────────────────────────

_ADAPTER_PROVENANCE = {
    "python_ast": ("ast", 1.0, "Resolved from the Python AST"),
    "pyan": ("static_analysis", 0.9, "Derived by pyan static analysis"),
    "javascript_structural": ("regex", 0.7, "Structural regex parse (JavaScript)"),
    "typescript_structural": ("regex", 0.7, "Structural regex parse (TypeScript)"),
    "java_structural": ("regex", 0.7, "Structural regex parse (Java)"),
    "c_structural": ("regex", 0.7, "Structural regex parse (C)"),
    "cpp_structural": ("regex", 0.7, "Structural regex parse (C++)"),
}


def edge_provenance(adapter_metadata: dict) -> Provenance:
    """Derive a Provenance record for an AST/structural function edge."""
    adapter_metadata = adapter_metadata or {}
    adapter = adapter_metadata.get("adapter") or adapter_metadata.get("source", "")
    src, conf, why = _ADAPTER_PROVENANCE.get(adapter, ("regex", 0.7, f"Extracted by {adapter or 'structural parser'}"))
    # Call edges resolved by graph_builder's tiered resolver (see
    # _resolve_call_target) carry their own confidence/reasoning — a
    # self.foo() resolved within its own class is far more certain than a
    # bare foo() that happened to be uniquely named repo-wide, and both are
    # more specific than the flat 0.9 this used to hardcode for every
    # name-matched call regardless of how it was actually resolved.
    if "resolution_confidence" in adapter_metadata:
        conf = adapter_metadata["resolution_confidence"]
        why = adapter_metadata.get("resolution_reasoning") or why
    elif adapter == "python_ast" and adapter_metadata.get("callee"):
        conf = 0.9
        why = "AST call site, resolved by name matching"
    return Provenance(source=src, confidence=conf, reasoning=why)


def semantic_edge_provenance(inferred_by: str, confidence: float) -> Provenance:
    """Provenance for a Phase-2 semantic edge."""
    src = "llm" if inferred_by == "bob" else "heuristic" if inferred_by == "heuristic" else "ast"
    return Provenance(
        source=src, confidence=confidence,
        reasoning="Inferred by LLM semantic analysis" if src == "llm"
        else "Heuristic pattern match",
    )
