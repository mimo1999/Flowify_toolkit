"""JSON-based graph store. One file per graph_id."""
from __future__ import annotations
import json
import os
import uuid
from pathlib import Path
from typing import Optional

from .models import GraphPayload


_STORE_DIR = Path(os.environ.get("FLOWIFY_STORE", "_store"))
_STORE_DIR.mkdir(parents=True, exist_ok=True)


def new_graph_id() -> str:
    return uuid.uuid4().hex[:12]


def _path(graph_id: str) -> Path:
    return _STORE_DIR / f"{graph_id}.json"


def save(payload: GraphPayload) -> None:
    _path(payload.graph_id).write_text(payload.model_dump_json(indent=2), encoding="utf-8")


def load(graph_id: str) -> Optional[GraphPayload]:
    p = _path(graph_id)
    if not p.exists():
        return None
    return GraphPayload.model_validate_json(p.read_text(encoding="utf-8"))


def list_graphs() -> list[str]:
    return [p.stem for p in _STORE_DIR.glob("*.json")]


def store_meta(graph_id: str, key: str, value: dict) -> None:
    p = _STORE_DIR / f"{graph_id}.{key}.json"
    p.write_text(json.dumps(value), encoding="utf-8")


def load_meta(graph_id: str, key: str) -> Optional[dict]:
    p = _STORE_DIR / f"{graph_id}.{key}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
