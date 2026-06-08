"""SQLite-backed graph store.

Replaces the previous flat-JSON approach with a single SQLite database at
$FLOWIFY_STORE/flowify.db, giving:

  - Atomic writes (no half-written graph files)
  - Partial reads: load_light() omits code_snippet for query/expand hot paths
  - Proper list/delete without filesystem scanning
  - WAL mode for concurrent reader + writer access

Public API (unchanged from callers' perspective, plus two additions):
  new_graph_id()            → str
  save(payload)             → None
  load(graph_id)            → GraphPayload | None        (includes code_snippet)
  load_light(graph_id)      → GraphPayload | None        (omits  code_snippet)
  list_graphs()             → list[str]
  delete(graph_id)          → bool                       (NEW)
  store_meta(gid, key, val) → None
  load_meta(gid, key)       → dict | None
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .models import (
    FunctionEdge,
    FunctionNode,
    GraphPayload,
    ModuleEdge,
    ModuleNode,
    SemanticEdge,
)

# ── Store location ─────────────────────────────────────────────────────────────
_STORE_DIR = Path(os.environ.get("FLOWIFY_STORE", "_store"))
_STORE_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = _STORE_DIR / "flowify.db"

# ── Thread-local SQLite connection ─────────────────────────────────────────────
_local = threading.local()


def _conn() -> sqlite3.Connection:
    """Return (or open) a per-thread SQLite connection."""
    if not hasattr(_local, "db"):
        db = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")    # concurrent reads + single writer
        db.execute("PRAGMA synchronous=NORMAL")  # fsync on checkpoint, not every commit
        db.execute("PRAGMA foreign_keys=ON")
        _init_schema(db)
        _local.db = db
    return _local.db


@contextmanager
def _tx() -> Iterator[sqlite3.Cursor]:
    """Yield a cursor inside an explicit transaction; rollback on error."""
    db = _conn()
    cur = db.cursor()
    try:
        yield cur
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()


# ── Schema ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS graphs (
    graph_id    TEXT PRIMARY KEY,
    repo_path   TEXT NOT NULL,
    cir_version TEXT,
    created_at  REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS nodes (
    id               TEXT NOT NULL,
    graph_id         TEXT NOT NULL,
    name             TEXT,
    file_path        TEXT,
    type             TEXT,
    kind             TEXT,
    source_language  TEXT,
    qualified_name   TEXT,
    lineno           INTEGER,
    summary          TEXT,
    semantics        TEXT,            -- JSON SemanticMetadata | NULL
    adapter_metadata TEXT,            -- JSON dict
    signature        TEXT,            -- JSON CIRSignature | NULL
    source_span      TEXT,            -- JSON CIRSourceSpan | NULL
    code_snippet     TEXT,            -- stored last; omitted in light reads
    PRIMARY KEY (id, graph_id)
);
CREATE INDEX IF NOT EXISTS idx_nodes_graph ON nodes(graph_id);

CREATE TABLE IF NOT EXISTS edges (
    rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_id     TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    type         TEXT,
    relationship TEXT,
    adapter_meta TEXT             -- JSON dict
);
CREATE INDEX IF NOT EXISTS idx_edges_graph ON edges(graph_id);

CREATE TABLE IF NOT EXISTS module_nodes (
    id                  TEXT NOT NULL,
    graph_id            TEXT NOT NULL,
    name                TEXT,
    description         TEXT,
    depth_level         INTEGER,
    linked_fn_ids       TEXT,    -- JSON list[str]
    is_entry_point      INTEGER DEFAULT 0,
    entry_functions     TEXT,    -- JSON list[str]
    control_flow_groups TEXT,    -- JSON dict
    submodule_type      TEXT,
    PRIMARY KEY (id, graph_id)
);
CREATE INDEX IF NOT EXISTS idx_modnodes_graph ON module_nodes(graph_id);

CREATE TABLE IF NOT EXISTS module_edges (
    rowid            INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_id         TEXT NOT NULL,
    source_module_id TEXT NOT NULL,
    target_module_id TEXT NOT NULL,
    type             TEXT DEFAULT 'FLOW'
);
CREATE INDEX IF NOT EXISTS idx_modedges_graph ON module_edges(graph_id);

CREATE TABLE IF NOT EXISTS module_to_functions (
    graph_id    TEXT NOT NULL,
    module_id   TEXT NOT NULL,
    function_id TEXT NOT NULL,
    PRIMARY KEY (graph_id, module_id, function_id)
);
CREATE INDEX IF NOT EXISTS idx_m2f_graph ON module_to_functions(graph_id);

CREATE TABLE IF NOT EXISTS semantic_edges (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_id    TEXT NOT NULL,
    type        TEXT,
    source_id   TEXT,
    target_id   TEXT,
    confidence  REAL,
    description TEXT,
    inferred_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_semedges_graph ON semantic_edges(graph_id);

CREATE TABLE IF NOT EXISTS metadata (
    graph_id TEXT NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT,            -- JSON
    PRIMARY KEY (graph_id, key)
);
"""


def _init_schema(db: sqlite3.Connection) -> None:
    db.executescript(_SCHEMA)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _j(v) -> Optional[str]:
    """Serialize v to a JSON string (None if v is None)."""
    if v is None:
        return None
    if hasattr(v, "model_dump"):
        return json.dumps(v.model_dump())
    return json.dumps(v)


def _row_to_node(row: sqlite3.Row, include_code: bool) -> FunctionNode:
    return FunctionNode(
        id=row["id"],
        name=row["name"] or "",
        file_path=row["file_path"] or "",
        type=row["type"] or "function",
        kind=row["kind"],
        source_language=row["source_language"] or "unknown",
        qualified_name=row["qualified_name"],
        lineno=row["lineno"],
        summary=row["summary"],
        semantics=json.loads(row["semantics"]) if row["semantics"] else None,
        adapter_metadata=json.loads(row["adapter_metadata"]) if row["adapter_metadata"] else {},
        signature=json.loads(row["signature"]) if row["signature"] else None,
        source_span=json.loads(row["source_span"]) if row["source_span"] else None,
        code_snippet=row["code_snippet"] if include_code else None,
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def new_graph_id() -> str:
    return uuid.uuid4().hex[:12]


def save(payload: GraphPayload) -> None:
    """Persist a full GraphPayload atomically, replacing any previous version."""
    gid = payload.graph_id
    with _tx() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO graphs(graph_id, repo_path, cir_version) VALUES (?,?,?)",
            (gid, payload.repo_path, payload.cir_version),
        )

        # Clear old rows so re-ingestion is a clean replace, not an append
        for tbl in ("nodes", "edges", "module_nodes", "module_edges",
                    "module_to_functions", "semantic_edges"):
            cur.execute(f"DELETE FROM {tbl} WHERE graph_id=?", (gid,))

        # nodes
        cur.executemany(
            """INSERT INTO nodes
               (id, graph_id, name, file_path, type, kind, source_language,
                qualified_name, lineno, summary, semantics, adapter_metadata,
                signature, source_span, code_snippet)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    n.id, gid, n.name, n.file_path, n.type, n.kind,
                    n.source_language, n.qualified_name, n.lineno, n.summary,
                    _j(n.semantics), _j(n.adapter_metadata),
                    _j(n.signature), _j(n.source_span), n.code_snippet,
                )
                for n in payload.function_nodes
            ],
        )

        # edges
        cur.executemany(
            """INSERT INTO edges
               (graph_id, source_id, target_id, type, relationship, adapter_meta)
               VALUES (?,?,?,?,?,?)""",
            [
                (gid, e.source_id, e.target_id, e.type, e.relationship,
                 _j(e.adapter_metadata))
                for e in payload.function_edges
            ],
        )

        # module nodes
        cur.executemany(
            """INSERT INTO module_nodes
               (id, graph_id, name, description, depth_level, linked_fn_ids,
                is_entry_point, entry_functions, control_flow_groups, submodule_type)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    m.id, gid, m.name, m.description, m.depth_level,
                    _j(m.linked_function_ids), int(m.is_entry_point),
                    _j(m.entry_functions), _j(m.control_flow_groups),
                    m.submodule_type,
                )
                for m in payload.module_nodes
            ],
        )

        # module edges
        cur.executemany(
            """INSERT INTO module_edges
               (graph_id, source_module_id, target_module_id, type)
               VALUES (?,?,?,?)""",
            [(gid, e.source_module_id, e.target_module_id, e.type)
             for e in payload.module_edges],
        )

        # module → functions mapping
        m2f_rows = [
            (gid, mod_id, fn_id)
            for mod_id, fn_ids in payload.module_to_functions.items()
            for fn_id in fn_ids
        ]
        if m2f_rows:
            cur.executemany(
                """INSERT OR IGNORE INTO module_to_functions
                   (graph_id, module_id, function_id) VALUES (?,?,?)""",
                m2f_rows,
            )

        # semantic edges
        if payload.semantic_edges:
            cur.executemany(
                """INSERT INTO semantic_edges
                   (graph_id, type, source_id, target_id, confidence, description, inferred_by)
                   VALUES (?,?,?,?,?,?,?)""",
                [
                    (gid, e.type, e.source_id, e.target_id,
                     e.confidence, e.description, e.inferred_by)
                    for e in payload.semantic_edges
                ],
            )


def _load_payload(graph_id: str, include_code: bool) -> Optional[GraphPayload]:
    db = _conn()

    hdr = db.execute(
        "SELECT repo_path, cir_version FROM graphs WHERE graph_id=?",
        (graph_id,),
    ).fetchone()
    if hdr is None:
        return None

    # nodes — omit code_snippet column in light mode
    if include_code:
        node_rows = db.execute(
            "SELECT * FROM nodes WHERE graph_id=?", (graph_id,)
        ).fetchall()
    else:
        node_rows = db.execute(
            """SELECT id, graph_id, name, file_path, type, kind, source_language,
                      qualified_name, lineno, summary, semantics, adapter_metadata,
                      signature, source_span, NULL AS code_snippet
               FROM nodes WHERE graph_id=?""",
            (graph_id,),
        ).fetchall()
    function_nodes = [_row_to_node(r, include_code) for r in node_rows]

    # edges
    edge_rows = db.execute(
        "SELECT * FROM edges WHERE graph_id=?", (graph_id,)
    ).fetchall()
    function_edges = [
        FunctionEdge(
            type=r["type"],
            relationship=r["relationship"],
            source_id=r["source_id"],
            target_id=r["target_id"],
            adapter_metadata=json.loads(r["adapter_meta"]) if r["adapter_meta"] else {},
        )
        for r in edge_rows
    ]

    # module nodes
    mod_rows = db.execute(
        "SELECT * FROM module_nodes WHERE graph_id=?", (graph_id,)
    ).fetchall()
    module_nodes = [
        ModuleNode(
            id=r["id"],
            name=r["name"] or "",
            description=r["description"] or "",
            depth_level=r["depth_level"] or 1,
            linked_function_ids=json.loads(r["linked_fn_ids"]) if r["linked_fn_ids"] else [],
            is_entry_point=bool(r["is_entry_point"]),
            entry_functions=json.loads(r["entry_functions"]) if r["entry_functions"] else [],
            control_flow_groups=json.loads(r["control_flow_groups"]) if r["control_flow_groups"] else {},
            submodule_type=r["submodule_type"],
        )
        for r in mod_rows
    ]

    # module edges
    medge_rows = db.execute(
        "SELECT * FROM module_edges WHERE graph_id=?", (graph_id,)
    ).fetchall()
    module_edges = [
        ModuleEdge(
            source_module_id=r["source_module_id"],
            target_module_id=r["target_module_id"],
            type=r["type"],
        )
        for r in medge_rows
    ]

    # module → functions
    m2f_rows = db.execute(
        "SELECT module_id, function_id FROM module_to_functions WHERE graph_id=?",
        (graph_id,),
    ).fetchall()
    module_to_functions: Dict[str, List[str]] = {}
    for r in m2f_rows:
        module_to_functions.setdefault(r["module_id"], []).append(r["function_id"])

    # semantic edges
    sem_rows = db.execute(
        "SELECT * FROM semantic_edges WHERE graph_id=?", (graph_id,)
    ).fetchall()
    semantic_edges = [
        SemanticEdge(
            type=r["type"],
            source_id=r["source_id"],
            target_id=r["target_id"],
            confidence=r["confidence"] if r["confidence"] is not None else 1.0,
            description=r["description"],
            inferred_by=r["inferred_by"] or "heuristic",
        )
        for r in sem_rows
    ]

    return GraphPayload(
        graph_id=graph_id,
        repo_path=hdr["repo_path"],
        cir_version=hdr["cir_version"] or "cir.v1",
        function_nodes=function_nodes,
        function_edges=function_edges,
        module_nodes=module_nodes,
        module_edges=module_edges,
        module_to_functions=module_to_functions,
        semantic_edges=semantic_edges,
    )


def load(graph_id: str) -> Optional[GraphPayload]:
    """Load a full GraphPayload including code_snippet on every node.

    Use for any path that may re-summarize or re-save the graph
    (pipeline.update, generate_descriptions, update_node_importance).
    """
    return _load_payload(graph_id, include_code=True)


def load_light(graph_id: str) -> Optional[GraphPayload]:
    """Load a GraphPayload with code_snippet=None on all nodes.

    Significantly faster for large graphs. Safe for all read-only paths:
    query, expand, impact analysis, graph display, analytics.
    """
    return _load_payload(graph_id, include_code=False)


def list_graphs() -> list[str]:
    """Return all stored graph IDs, newest first."""
    return [
        r[0] for r in _conn().execute(
            "SELECT graph_id FROM graphs ORDER BY created_at DESC"
        )
    ]


def delete(graph_id: str) -> bool:
    """Delete a graph and all its associated data. Returns True if it existed."""
    with _tx() as cur:
        cur.execute("DELETE FROM graphs WHERE graph_id=?", (graph_id,))
        existed = cur.rowcount > 0
        if existed:
            for tbl in ("nodes", "edges", "module_nodes", "module_edges",
                        "module_to_functions", "semantic_edges", "metadata"):
                cur.execute(f"DELETE FROM {tbl} WHERE graph_id=?", (graph_id,))
    return existed


def store_meta(graph_id: str, key: str, value: dict) -> None:
    with _tx() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO metadata(graph_id, key, value) VALUES (?,?,?)",
            (graph_id, key, json.dumps(value)),
        )


def load_meta(graph_id: str, key: str) -> Optional[dict]:
    row = _conn().execute(
        "SELECT value FROM metadata WHERE graph_id=? AND key=?",
        (graph_id, key),
    ).fetchone()
    return json.loads(row["value"]) if row else None
