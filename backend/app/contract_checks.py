"""Contract checks: what a change breaks for code that did not change with it.

Python only. Compares the AST of the merge base with the AST of the head file, no LLM. Every
finding is a claim with the code that proves it:

  breaks  a caller that bound to the old signature cannot bind to the new one; a new definition
          that silently takes over an existing name (same-scope redefinition, a shadowed import)
  check   a default value changed, a parameter is no longer used, a method now overrides one of
          the repo's base classes, a module constant changed value

A call that uses `*args` or `**kwargs` is "cannot tell" and never reported, so a finding is only
made when the arguments are all visible.
"""
from __future__ import annotations

import ast
import subprocess
from collections import Counter

BREAKS, CHECK = "breaks", "check"
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_DEFER = ("setter", "getter", "deleter", "overload", "register")  # same name on purpose


def _git_show(repo: str, rev: str, path: str) -> str | None:
    r = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=repo, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else None


def merge_base(repo: str, base: str) -> str | None:
    r = subprocess.run(["git", "merge-base", base, "HEAD"], cwd=repo, capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


class Sources:
    """Parsed head (working tree) and base (merge-base) files, read once per review."""

    def __init__(self, repo: str, base_rev: str | None):
        self.repo, self.base_rev = repo, base_rev
        self._cache: dict = {}

    def _parse(self, key, text):
        if key not in self._cache:
            try:
                self._cache[key] = (text.splitlines(), ast.parse(text)) if text is not None else ([], None)
            except (SyntaxError, ValueError):
                self._cache[key] = (text.splitlines(), None)
        return self._cache[key]

    def head(self, path: str):
        if ("h", path) not in self._cache:
            try:
                with open(f"{self.repo}/{path}", encoding="utf-8-sig") as f:
                    text = f.read()
            except OSError:
                text = None
            return self._parse(("h", path), text)
        return self._cache[("h", path)]

    def base(self, path: str):
        if ("b", path) not in self._cache:
            text = _git_show(self.repo, self.base_rev, path) if self.base_rev else None
            return self._parse(("b", path), text.lstrip("﻿") if text else text)
        return self._cache[("b", path)]


def _defs(tree) -> dict:
    """qualname -> [def nodes in source order], qualnames built the way graph_builder builds them."""
    out: dict = {}

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (*_DEFS, ast.ClassDef)):
                q = prefix + ch.name
                if isinstance(ch, _DEFS):
                    out.setdefault(q, []).append(ch)
                walk(ch, q + ".")
            else:
                walk(ch, prefix)

    if tree is not None:
        walk(tree, "")
    return out


def _parents(tree) -> dict:
    return {id(ch): p for p in ast.walk(tree) for ch in ast.iter_child_nodes(p)}


def _sig(fn) -> dict:
    a = fn.args
    pos = [x.arg for x in a.posonlyargs + a.args]
    defaults = {p: ast.unparse(d) for p, d in zip(pos[len(pos) - len(a.defaults):], a.defaults)}
    kwonly = [x.arg for x in a.kwonlyargs]
    defaults.update({k: ast.unparse(d) for k, d in zip(kwonly, a.kw_defaults) if d is not None})
    return {"pos": pos, "posonly": len(a.posonlyargs), "kwonly": kwonly, "defaults": defaults,
            "var": a.vararg is not None, "varkw": a.kwarg is not None}


def _bind(sig: dict, npos: int, kws: list, bound: bool) -> str | None:
    """None when a call with `npos` positional args and keywords `kws` binds, else the reason."""
    pos, posonly = (sig["pos"][1:], max(0, sig["posonly"] - 1)) if bound else (sig["pos"], sig["posonly"])
    if npos > len(pos) and not sig["var"]:
        return f"passes {npos} positional argument{'s' if npos != 1 else ''}; it now takes {len(pos)}"
    for k in kws:
        if k in pos[:npos]:
            return f"passes `{k}` both by position and by name"
        if k not in pos[posonly:] and k not in sig["kwonly"] and not sig["varkw"]:
            return f"passes `{k}=`, which it no longer accepts"
    missing = [p for p in pos[npos:] if p not in sig["defaults"] and p not in kws]
    missing += [k for k in sig["kwonly"] if k not in sig["defaults"] and k not in kws]
    return f"does not pass `{missing[0]}`, which is now required" if missing else None


def _is_bound(fn, is_method: bool) -> bool:
    deco = {d.id if isinstance(d, ast.Name) else getattr(d, "attr", "") for d in fn.decorator_list}
    return is_method and "staticmethod" not in deco


def _call_at(tree, line: int, short: str):
    """The innermost call on `line` whose callee's last name part is `short`."""
    best = None
    for c in ast.walk(tree):
        if isinstance(c, ast.Call) and c.lineno <= line <= (c.end_lineno or c.lineno):
            f = c.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name == short and (best is None or c.lineno >= best.lineno):
                best = c
    return best


def _code(lines: list, line: int) -> str:
    return lines[line - 1].strip() if 0 < line <= len(lines) else ""


def _evidence(file: str, line: int, lines: list) -> dict:
    return {"file": file, "line": line, "code": _code(lines, line)}


def _pick(defs: list, line: int | None):
    return next((d for d in defs if d.lineno == line), defs[-1] if defs else None)


def check_function(src: Sources, node, callers: list, class_bases: dict, node_ids: set,
                   incoming: Counter) -> list:
    """Findings for one changed Python function. `callers` are its direct callers with the line of
    each call (pr_impact's `direct` rows); `class_bases` maps a class id to the repo classes it
    extends; `incoming` counts call edges per node id."""
    path, qual = node.file_path, node.qualified_name or node.name
    if not path.endswith(".py"):
        return []
    lines, tree = src.head(path)
    head = _pick(_defs(tree).get(qual, []), node.lineno)
    if head is None:
        return []
    is_method = node.type == "method"
    bound = _is_bound(head, is_method)
    blines, btree = src.base(path)
    bdefs = _defs(btree).get(qual, []) if btree is not None else []
    i = _defs(tree)[qual].index(head)  # the i-th definition of this name; a new extra one has no base
    base = bdefs[i] if i < len(bdefs) else None
    out = []
    if base is None:
        out += _new_definition(head, qual, path, lines, tree, node, class_bases, node_ids, incoming)
        return out

    new, old = _sig(head), _sig(base)
    if new != old:
        short = qual.rsplit(".", 1)[-1]
        if short == "__init__" and "." in qual:
            short = qual.rsplit(".", 2)[-2]  # constructed as `Cls(...)`
        relying: dict = {}
        for c in callers:
            if not c.get("line") or not c.get("file", "").endswith(".py"):
                continue
            clines, ctree = src.head(c["file"])
            call = _call_at(ctree, c["line"], short) if ctree is not None else None
            if call is None or any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
                continue  # cannot tell
            kws = [k.arg for k in call.keywords]
            b = bound and (isinstance(call.func, ast.Attribute) or short != qual.rsplit(".", 1)[-1])
            was, now = _bind(old, len(call.args), kws, b), _bind(new, len(call.args), kws, b)
            if now and not was:
                out.append({"kind": "signature", "severity": BREAKS,
                            "message": f"`{c['name']}` {now}",
                            "evidence": [_evidence(c["file"], c["line"], clines), _evidence(path, head.lineno, lines)]})
            pos = new["pos"][1:] if b else new["pos"]
            for p in new["defaults"]:
                if p in old["defaults"] and old["defaults"][p] != new["defaults"][p] and p not in kws and p not in pos[:len(call.args)]:
                    relying.setdefault(p, []).append(_evidence(c["file"], c["line"], clines))
        for p, ev in relying.items():
            out.append({"kind": "default", "severity": CHECK,
                        "message": f"`{p}` default changed from `{old['defaults'][p]}` to `{new['defaults'][p]}`; "
                                   f"{len(ev)} caller{'s' if len(ev) != 1 else ''} rely on it",
                        "evidence": [_evidence(path, head.lineno, lines)] + ev[:3]})
        for p in sorted(set(old["defaults"]) & set(new["defaults"]) - set(relying)):
            if old["defaults"][p] != new["defaults"][p]:
                out.append({"kind": "default", "severity": CHECK,
                            "message": f"`{p}` default changed from `{old['defaults'][p]}` to `{new['defaults'][p]}`",
                            "evidence": [_evidence(path, head.lineno, lines)]})

    used = lambda fn: {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}  # noqa: E731
    params = [p for p in new["pos"] + new["kwonly"] if p not in ("self", "cls") and not p.startswith("_")]
    if not _trivial(head):
        for p in params:
            if p in used(base) and p not in used(head) and p in old["pos"] + old["kwonly"]:
                out.append({"kind": "unused_param", "severity": CHECK,
                            "message": f"`{p}` is still accepted but no longer used",
                            "evidence": [_evidence(path, head.lineno, lines)]})
    return out


def _trivial(fn) -> bool:
    body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    return all(isinstance(s, (ast.Pass, ast.Raise)) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
               for s in body)


def _new_definition(head, qual, path, lines, tree, node, class_bases, node_ids, incoming) -> list:
    out = []
    name = head.name
    deco = [ast.unparse(d) for d in head.decorator_list]
    parent = _parents(tree).get(id(head))
    # only the statement list that holds this def: an if/else pair of defs is deliberate
    block = next((lst for f in ("body", "orelse", "finalbody") if head in (lst := getattr(parent, f, None) or [])), [])
    siblings = [s for s in block if isinstance(s, _DEFS) and s.name == name and s is not head]
    if siblings and not any(d.endswith(_DEFER) for s in [head, *siblings] for d in (ast.unparse(x) for x in s.decorator_list)):
        later = [s for s in siblings if s.lineno > head.lineno]
        if later:
            out.append({"kind": "redefinition", "severity": CHECK,
                        "message": f"`{name}` is defined again at line {later[-1].lineno}; this new definition is never called",
                        "evidence": [_evidence(path, head.lineno, lines), _evidence(path, later[-1].lineno, lines)]})
        else:
            earlier = max(siblings, key=lambda s: s.lineno)
            out.append({"kind": "redefinition", "severity": BREAKS,
                        "message": f"redefines `{name}` from line {earlier.lineno}: every call now goes to this one",
                        "evidence": [_evidence(path, earlier.lineno, lines), _evidence(path, head.lineno, lines)]})

    if "." not in qual and isinstance(parent, ast.Module):  # a module-level function can shadow an import
        for st in parent.body:
            if isinstance(st, (ast.Import, ast.ImportFrom)) and st.lineno < head.lineno:
                for a in st.names:
                    if (a.asname or a.name.split(".")[0]) == name:
                        calls = sum(1 for c in ast.walk(tree) if isinstance(c, ast.Call)
                                    and isinstance(c.func, ast.Name) and c.func.id == name)
                        out.append({"kind": "shadows_import", "severity": BREAKS,
                                    "message": f"shadows `{name}` imported at line {st.lineno}; "
                                               f"{calls} call{'s' if calls != 1 else ''} in this file now go here",
                                    "evidence": [_evidence(path, st.lineno, lines), _evidence(path, head.lineno, lines)]})

    if node.type == "method" and not deco:
        cls_id = node.id.split("#")[0].rsplit(".", 1)[0]
        seen, todo = set(), list(class_bases.get(cls_id, []))
        while todo:
            b = todo.pop(0)
            if b in seen:
                continue
            seen.add(b)
            mid = f"{b}.{name}"
            if mid in node_ids:
                n = incoming.get(mid, 0)
                out.append({"kind": "override", "severity": CHECK,
                            "message": f"overrides `{mid.split('::', 1)[1]}` ({mid.split('::', 1)[0]}), which has "
                                       f"{n} caller{'s' if n != 1 else ''}; on `{cls_id.split('::', 1)[1]}` objects those calls now run this one",
                            "evidence": [_evidence(path, head.lineno, lines)]})
                break
            todo += class_bases.get(b, [])
    return out


def check_module(src: Sources, chunk: dict) -> list:
    """Module-level names whose value changed (a constant, a config table)."""
    path = chunk["file"]
    lines, tree = src.head(path)
    _, btree = src.base(path)
    if tree is None or btree is None:
        return []

    def values(t):
        out = {}
        for st in t.body:
            if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None:
                for tgt in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                    if isinstance(tgt, ast.Name):
                        out[tgt.id] = (ast.unparse(st.value), st.lineno)
        return out

    old, new = values(btree), values(tree)
    users = len(chunk.get("importers", [])) + len(chunk.get("module_importers", []))
    out = []
    for name in chunk.get("names", []):
        if name in old and name in new and old[name][0] != new[name][0]:
            short = lambda v: v if len(v) <= 60 else v[:57] + "..."  # noqa: E731
            out.append({"kind": "constant", "severity": CHECK,
                        "message": f"`{name}` changed from `{short(old[name][0])}` to `{short(new[name][0])}`"
                                   + (f"; {users} file{'s' if users != 1 else ''} import it" if users else ""),
                        "evidence": [_evidence(path, new[name][1], lines)]})
    return out


def class_bases(payload) -> dict:
    """class id -> ids of the repo classes it extends (INHERITS edges, already resolved at ingest)."""
    out: dict = {}
    for e in payload.function_edges:
        if e.type == "INHERITS" and not e.target_id.startswith("<"):
            out.setdefault(e.source_id, []).append(e.target_id)
    return out
