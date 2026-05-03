"""CIR graph construction via language-specific structural adapters.

Builds nodes (file/class/function/method) and edges (DEFINES, CALLS, IMPORTS, INHERITS).
The call graph is approximate — Python's dynamism means we resolve names heuristically:
a call like `foo()` matches any function named `foo` in the repo; `obj.bar()` matches
any method named `bar`.  Good enough for a hackathon visualization.
"""
from __future__ import annotations
import ast
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import networkx as nx

from .ingestion import detect_language, iter_source_files
from .models import CIRSignature, CIRSourceSpan, FunctionNode, FunctionEdge


def _node_id(file_path: str, qualname: str) -> str:
    return f"{file_path}::{qualname}"


def _source_span_from_lines(start_line: int, end_line: int | None = None) -> CIRSourceSpan:
    return CIRSourceSpan(start_line=start_line, end_line=end_line or start_line)


def _line_snippet(lines: List[str], start_line: int, end_line: int | None = None) -> str:
    end_line = end_line or start_line
    return "\n".join(lines[start_line - 1:end_line])


def _brace_end_line(lines: List[str], start_index: int) -> int:
    depth = 0
    seen_open = False
    for idx in range(start_index, len(lines)):
        line = re.sub(r"//.*", "", lines[idx])
        depth += line.count("{")
        if "{" in line:
            seen_open = True
        depth -= line.count("}")
        if seen_open and depth <= 0:
            return idx + 1
    return start_index + 1


def _call_names(body: str, declared_name: str) -> List[str]:
    keywords = {
        "if", "for", "while", "switch", "catch", "return", "new", "throw", "typeof",
        "sizeof", "delete", "await", "function", "class", "super",
    }
    calls: List[str] = []
    for match in re.finditer(r"(?:\b|\.)([A-Za-z_][$\w]*)\s*\(", body):
        name = match.group(1)
        if name not in keywords and name != declared_name and name not in calls:
            calls.append(name)
    return calls


def _append_call_edges(
    edges: List[FunctionEdge],
    source_id: str,
    body: str,
    declared_name: str,
    adapter: str,
) -> None:
    for callee in _call_names(body, declared_name):
        edges.append(FunctionEdge(
            type="CALLS", relationship="INVOKES",
            source_id=source_id, target_id=f"<symbol>::{callee}",
            adapter_metadata={"adapter": adapter, "callee": callee},
        ))


class _Visitor(ast.NodeVisitor):
    def __init__(self, file_path: str, source: str):
        self.file_path = file_path
        self.source_lines = source.splitlines()
        self.nodes: List[FunctionNode] = []
        self.edges: List[FunctionEdge] = []
        self.qual_stack: List[str] = []
        self.file_id = _node_id(file_path, "<file>")
        self.nodes.append(FunctionNode(
            id=self.file_id, name=Path(file_path).name,
            file_path=file_path, type="file", kind="file",
            source_language="python", qualified_name="<file>",
            adapter_metadata={"adapter": "python_ast"},
        ))
        self.imports: List[str] = []

    def _qual(self, name: str) -> str:
        return ".".join(self.qual_stack + [name])

    def _snippet(self, node: ast.AST) -> str:
        start = getattr(node, "lineno", 1) - 1
        end = getattr(node, "end_lineno", start + 1)
        return "\n".join(self.source_lines[start:end])

    @staticmethod
    def _expr_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = _Visitor._expr_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        if isinstance(node, ast.Call):
            return _Visitor._expr_name(node.func)
        if isinstance(node, ast.Constant):
            return repr(node.value)
        try:
            return ast.unparse(node)
        except Exception:
            return node.__class__.__name__

    @staticmethod
    def _decorators(node: ast.AST) -> List[str]:
        return [_Visitor._expr_name(d) for d in getattr(node, "decorator_list", [])]

    @staticmethod
    def _source_span(node: ast.AST) -> CIRSourceSpan:
        return CIRSourceSpan(
            start_line=getattr(node, "lineno", None),
            end_line=getattr(node, "end_lineno", None),
            start_column=getattr(node, "col_offset", None),
            end_column=getattr(node, "end_col_offset", None),
        )

    @staticmethod
    def _annotation(node: ast.AST | None) -> str | None:
        if node is None:
            return None
        return _Visitor._expr_name(node)

    @classmethod
    def _signature(cls, node: ast.FunctionDef | ast.AsyncFunctionDef) -> CIRSignature:
        params: List[Dict[str, Any]] = []
        args = list(node.args.posonlyargs) + list(node.args.args)
        if node.args.vararg:
            args.append(node.args.vararg)
        args.extend(node.args.kwonlyargs)
        if node.args.kwarg:
            args.append(node.args.kwarg)
        for arg in args:
            params.append({"name": arg.arg, "type": cls._annotation(arg.annotation)})
        has_annotations = any(p.get("type") for p in params) or node.returns is not None
        return CIRSignature(
            parameters=params,
            returns=cls._annotation(node.returns),
            type_system="explicit" if has_annotations else "dynamic",
        )

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports.append(alias.name)
            self.edges.append(FunctionEdge(
                type="IMPORTS", source_id=self.file_id,
                target_id=f"<module>::{alias.name}",
                relationship="DEPENDS_ON",
                adapter_metadata={
                    "adapter": "python_ast",
                    "dependency_kind": "static_import",
                    "imported": alias.name,
                    "alias": alias.asname,
                },
            ))

    def visit_ImportFrom(self, node: ast.ImportFrom):
        mod = node.module or ""
        for alias in node.names:
            full = f"{mod}.{alias.name}" if mod else alias.name
            self.imports.append(full)
            self.edges.append(FunctionEdge(
                type="IMPORTS", source_id=self.file_id,
                target_id=f"<module>::{full}",
                relationship="DEPENDS_ON",
                adapter_metadata={
                    "adapter": "python_ast",
                    "dependency_kind": "static_import",
                    "imported": full,
                    "module": mod,
                    "alias": alias.asname,
                },
            ))

    def visit_ClassDef(self, node: ast.ClassDef):
        qual = self._qual(node.name)
        nid = _node_id(self.file_path, qual)
        self.nodes.append(FunctionNode(
            id=nid, name=node.name, file_path=self.file_path,
            type="class", kind="type", source_language="python",
            qualified_name=qual, source_span=self._source_span(node),
            code_snippet=self._snippet(node), lineno=node.lineno,
            adapter_metadata={
                "adapter": "python_ast",
                "decorators": self._decorators(node),
                "bases": [self._expr_name(base) for base in node.bases],
            },
        ))
        parent = self.qual_stack[-1] if self.qual_stack else "<file>"
        parent_id = _node_id(self.file_path, parent if parent != "<file>" else "<file>")
        self.edges.append(FunctionEdge(
            type="DEFINES", relationship="CONTAINS",
            source_id=parent_id, target_id=nid,
            adapter_metadata={"adapter": "python_ast"},
        ))
        for base in node.bases:
            base_name = self._expr_name(base)
            if base_name:
                self.edges.append(FunctionEdge(
                    type="INHERITS", relationship="EXTENDS",
                    source_id=nid, target_id=f"<symbol>::{base_name}",
                    adapter_metadata={"adapter": "python_ast", "base": base_name},
                ))
        self.qual_stack.append(node.name)
        self.generic_visit(node)
        self.qual_stack.pop()

    def _visit_func(self, node, kind: str):
        qual = self._qual(node.name)
        nid = _node_id(self.file_path, qual)
        self.nodes.append(FunctionNode(
            id=nid, name=node.name, file_path=self.file_path,
            type=kind, kind="function", source_language="python",
            qualified_name=qual, source_span=self._source_span(node),
            signature=self._signature(node),
            code_snippet=self._snippet(node), lineno=node.lineno,
            adapter_metadata={
                "adapter": "python_ast",
                "decorators": self._decorators(node),
                "async": isinstance(node, ast.AsyncFunctionDef),
            },
        ))
        parent = self.qual_stack[-1] if self.qual_stack else "<file>"
        parent_id = _node_id(self.file_path, parent if parent != "<file>" else "<file>")
        self.edges.append(FunctionEdge(
            type="DEFINES", relationship="CONTAINS",
            source_id=parent_id, target_id=nid,
            adapter_metadata={"adapter": "python_ast"},
        ))

        # collect calls inside this function body
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                dynamic_import = self._dynamic_import_name(sub)
                if dynamic_import:
                    self.edges.append(FunctionEdge(
                        type="IMPORTS", relationship="DEPENDS_ON",
                        source_id=nid, target_id=f"<module>::{dynamic_import}",
                        adapter_metadata={
                            "adapter": "python_ast",
                            "dependency_kind": "dynamic_import",
                            "imported": dynamic_import,
                        },
                    ))
                callee = self._call_name(sub.func)
                if callee:
                    self.edges.append(FunctionEdge(
                        type="CALLS", relationship="INVOKES",
                        source_id=nid, target_id=f"<symbol>::{callee}",
                        adapter_metadata={"adapter": "python_ast", "callee": callee},
                    ))

        self.qual_stack.append(node.name)
        # Don't recurse for nested-call collection (already done via ast.walk)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(child)
        self.qual_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        kind = "method" if self.qual_stack else "function"
        self._visit_func(node, kind)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        kind = "method" if self.qual_stack else "function"
        self._visit_func(node, kind)

    @staticmethod
    def _call_name(func: ast.AST) -> str | None:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    @staticmethod
    def _dynamic_import_name(call: ast.Call) -> str | None:
        callee = _Visitor._expr_name(call.func)
        if callee not in {"__import__", "importlib.import_module"}:
            return None
        if not call.args or not isinstance(call.args[0], ast.Constant):
            return None
        module_name = call.args[0].value
        return module_name if isinstance(module_name, str) else None


def parse_file(file_path: Path, repo_root: Path) -> Tuple[List[FunctionNode], List[FunctionEdge]]:
    language = detect_language(file_path)
    if language == "python":
        return parse_python_file(file_path, repo_root)
    if language in {"javascript", "typescript"}:
        return parse_js_ts_file(file_path, repo_root, language)
    if language == "java":
        return parse_java_file(file_path, repo_root)
    if language in {"c", "cpp"}:
        return parse_c_family_file(file_path, repo_root, language)
    return [], []


def parse_python_file(file_path: Path, repo_root: Path) -> Tuple[List[FunctionNode], List[FunctionEdge]]:
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError):
        return [], []
    rel = str(file_path.relative_to(repo_root)).replace("\\", "/")
    v = _Visitor(rel, source)
    v.visit(tree)
    return v.nodes, v.edges


def _file_node(rel: str, language: str, adapter: str) -> FunctionNode:
    return FunctionNode(
        id=_node_id(rel, "<file>"),
        name=Path(rel).name,
        file_path=rel,
        type="file",
        kind="file",
        source_language=language,
        qualified_name="<file>",
        adapter_metadata={"adapter": adapter},
    )


def parse_js_ts_file(
    file_path: Path,
    repo_root: Path,
    language: str,
) -> Tuple[List[FunctionNode], List[FunctionEdge]]:
    try:
        source = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [], []
    rel = str(file_path.relative_to(repo_root)).replace("\\", "/")
    adapter = "typescript_structural" if language == "typescript" else "javascript_structural"
    lines = source.splitlines()
    file_id = _node_id(rel, "<file>")
    nodes: List[FunctionNode] = [_file_node(rel, language, adapter)]
    edges: List[FunctionEdge] = []
    class_stack: List[Tuple[str, int]] = []

    import_patterns = [
        re.compile(r"^\s*import\s+.+?\s+from\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"^\s*import\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    ]
    class_re = re.compile(r"^\s*(?:export\s+default\s+|export\s+)?class\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([A-Za-z_$][\w$\.]*))?")
    function_re = re.compile(r"^\s*(?:export\s+default\s+|export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")
    arrow_re = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)(?:\s*:\s*[^=]+)?\s*=>")
    method_re = re.compile(r"^\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{")

    for idx, line in enumerate(lines):
        line_no = idx + 1
        while class_stack and line_no > class_stack[-1][1]:
            class_stack.pop()

        for pattern in import_patterns:
            match = pattern.search(line)
            if match:
                imported = match.group(1)
                edges.append(FunctionEdge(
                    type="IMPORTS", relationship="DEPENDS_ON",
                    source_id=file_id, target_id=f"<module>::{imported}",
                    adapter_metadata={"adapter": adapter, "dependency_kind": "static_import", "imported": imported},
                ))

        class_match = class_re.match(line)
        if class_match:
            name = class_match.group(1)
            base = class_match.group(2)
            end_line = _brace_end_line(lines, idx)
            nid = _node_id(rel, name)
            nodes.append(FunctionNode(
                id=nid, name=name, file_path=rel, type="class", kind="type",
                source_language=language, qualified_name=name,
                source_span=_source_span_from_lines(line_no, end_line),
                code_snippet=_line_snippet(lines, line_no, end_line), lineno=line_no,
                adapter_metadata={"adapter": adapter, "bases": [base] if base else []},
            ))
            edges.append(FunctionEdge(type="DEFINES", relationship="CONTAINS", source_id=file_id, target_id=nid, adapter_metadata={"adapter": adapter}))
            if base:
                edges.append(FunctionEdge(type="INHERITS", relationship="EXTENDS", source_id=nid, target_id=f"<symbol>::{base}", adapter_metadata={"adapter": adapter, "base": base}))
            class_stack.append((name, end_line))
            continue

        func_match = function_re.match(line) or arrow_re.match(line)
        method_match = method_re.match(line) if class_stack else None
        if func_match or method_match:
            name = (func_match or method_match).group(1)
            owner = class_stack[-1][0] if method_match else None
            qual = f"{owner}.{name}" if owner else name
            kind = "method" if owner else "function"
            end_line = _brace_end_line(lines, idx)
            nid = _node_id(rel, qual)
            body = _line_snippet(lines, line_no, end_line)
            nodes.append(FunctionNode(
                id=nid, name=name, file_path=rel, type=kind, kind="function",
                source_language=language, qualified_name=qual,
                source_span=_source_span_from_lines(line_no, end_line),
                signature=CIRSignature(type_system="explicit" if language == "typescript" else "dynamic"),
                code_snippet=body, lineno=line_no,
                adapter_metadata={"adapter": adapter},
            ))
            parent_id = _node_id(rel, owner) if owner else file_id
            edges.append(FunctionEdge(type="DEFINES", relationship="CONTAINS", source_id=parent_id, target_id=nid, adapter_metadata={"adapter": adapter}))
            _append_call_edges(edges, nid, body, name, adapter)

    return nodes, edges


def parse_java_file(file_path: Path, repo_root: Path) -> Tuple[List[FunctionNode], List[FunctionEdge]]:
    try:
        source = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [], []
    rel = str(file_path.relative_to(repo_root)).replace("\\", "/")
    adapter = "java_structural"
    lines = source.splitlines()
    file_id = _node_id(rel, "<file>")
    nodes: List[FunctionNode] = [_file_node(rel, "java", adapter)]
    edges: List[FunctionEdge] = []
    class_stack: List[Tuple[str, int]] = []
    class_re = re.compile(r"^\s*(?:public|protected|private|abstract|final|static|\s)*\s*(?:class|interface|enum|record)\s+([A-Za-z_]\w*)(?:\s+extends\s+([A-Za-z_][\w.]*))?")
    import_re = re.compile(r"^\s*import\s+(?:static\s+)?([A-Za-z_][\w.*]*);")
    method_re = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:public|protected|private|static|final|abstract|synchronized|native|\s)+[\w<>\[\].?,\s]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:throws\s+[\w.,\s]+)?\{")

    for idx, line in enumerate(lines):
        line_no = idx + 1
        while class_stack and line_no > class_stack[-1][1]:
            class_stack.pop()
        import_match = import_re.match(line)
        if import_match:
            imported = import_match.group(1)
            edges.append(FunctionEdge(type="IMPORTS", relationship="DEPENDS_ON", source_id=file_id, target_id=f"<module>::{imported}", adapter_metadata={"adapter": adapter, "dependency_kind": "static_import", "imported": imported}))

        class_match = class_re.match(line)
        if class_match:
            name = class_match.group(1)
            base = class_match.group(2)
            end_line = _brace_end_line(lines, idx)
            nid = _node_id(rel, name)
            nodes.append(FunctionNode(id=nid, name=name, file_path=rel, type="class", kind="type", source_language="java", qualified_name=name, source_span=_source_span_from_lines(line_no, end_line), code_snippet=_line_snippet(lines, line_no, end_line), lineno=line_no, adapter_metadata={"adapter": adapter, "bases": [base] if base else []}))
            edges.append(FunctionEdge(type="DEFINES", relationship="CONTAINS", source_id=file_id, target_id=nid, adapter_metadata={"adapter": adapter}))
            if base:
                edges.append(FunctionEdge(type="INHERITS", relationship="EXTENDS", source_id=nid, target_id=f"<symbol>::{base}", adapter_metadata={"adapter": adapter, "base": base}))
            class_stack.append((name, end_line))
            continue

        method_match = method_re.match(line)
        if method_match and class_stack:
            name = method_match.group(1)
            owner = class_stack[-1][0]
            qual = f"{owner}.{name}"
            end_line = _brace_end_line(lines, idx)
            body = _line_snippet(lines, line_no, end_line)
            nid = _node_id(rel, qual)
            nodes.append(FunctionNode(id=nid, name=name, file_path=rel, type="method", kind="function", source_language="java", qualified_name=qual, source_span=_source_span_from_lines(line_no, end_line), signature=CIRSignature(type_system="explicit"), code_snippet=body, lineno=line_no, adapter_metadata={"adapter": adapter}))
            edges.append(FunctionEdge(type="DEFINES", relationship="CONTAINS", source_id=_node_id(rel, owner), target_id=nid, adapter_metadata={"adapter": adapter}))
            _append_call_edges(edges, nid, body, name, adapter)

    return nodes, edges


def parse_c_family_file(
    file_path: Path,
    repo_root: Path,
    language: str,
) -> Tuple[List[FunctionNode], List[FunctionEdge]]:
    try:
        source = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [], []
    rel = str(file_path.relative_to(repo_root)).replace("\\", "/")
    adapter = f"{language}_structural"
    lines = source.splitlines()
    file_id = _node_id(rel, "<file>")
    nodes: List[FunctionNode] = [_file_node(rel, language, adapter)]
    edges: List[FunctionEdge] = []
    include_re = re.compile(r"^\s*#\s*include\s+[<\"]([^>\"]+)[>\"]")
    class_re = re.compile(r"^\s*(?:class|struct)\s+([A-Za-z_]\w*)")
    function_re = re.compile(r"^\s*(?:template\s*<[^>]+>\s*)?(?:[\w:*&<>\[\]\s]+)\s+([A-Za-z_]\w*(?:::[A-Za-z_]\w*)?)\s*\([^;]*\)\s*(?:const\s*)?\{")

    for idx, line in enumerate(lines):
        line_no = idx + 1
        include_match = include_re.match(line)
        if include_match:
            imported = include_match.group(1)
            edges.append(FunctionEdge(type="IMPORTS", relationship="DEPENDS_ON", source_id=file_id, target_id=f"<module>::{imported}", adapter_metadata={"adapter": adapter, "dependency_kind": "include", "imported": imported}))
            continue

        if language == "cpp":
            class_match = class_re.match(line)
            if class_match:
                name = class_match.group(1)
                end_line = _brace_end_line(lines, idx)
                nid = _node_id(rel, name)
                nodes.append(FunctionNode(id=nid, name=name, file_path=rel, type="class", kind="type", source_language=language, qualified_name=name, source_span=_source_span_from_lines(line_no, end_line), code_snippet=_line_snippet(lines, line_no, end_line), lineno=line_no, adapter_metadata={"adapter": adapter}))
                edges.append(FunctionEdge(type="DEFINES", relationship="CONTAINS", source_id=file_id, target_id=nid, adapter_metadata={"adapter": adapter}))
                continue

        function_match = function_re.match(line)
        if function_match:
            raw_name = function_match.group(1)
            name = raw_name.split("::")[-1]
            owner = raw_name.rsplit("::", 1)[0] if "::" in raw_name else None
            node_type = "method" if owner else "function"
            end_line = _brace_end_line(lines, idx)
            body = _line_snippet(lines, line_no, end_line)
            nid = _node_id(rel, raw_name)
            nodes.append(FunctionNode(id=nid, name=name, file_path=rel, type=node_type, kind="function", source_language=language, qualified_name=raw_name, source_span=_source_span_from_lines(line_no, end_line), signature=CIRSignature(type_system="explicit"), code_snippet=body, lineno=line_no, adapter_metadata={"adapter": adapter}))
            parent_id = _node_id(rel, owner) if owner else file_id
            edges.append(FunctionEdge(type="DEFINES", relationship="CONTAINS", source_id=parent_id, target_id=nid, adapter_metadata={"adapter": adapter}))
            _append_call_edges(edges, nid, body, name, adapter)

    return nodes, edges


def build_function_graph(repo_path: str) -> Tuple[nx.DiGraph, List[FunctionNode], List[FunctionEdge]]:
    repo_root = Path(repo_path).resolve()
    all_nodes: List[FunctionNode] = []
    all_edges: List[FunctionEdge] = []
    for fp in iter_source_files(repo_path):
        nodes, edges = parse_file(fp, repo_root)
        all_nodes.extend(nodes)
        all_edges.extend(edges)

    # Resolve <symbol>:: targets to real node ids by short name match.
    name_index: Dict[str, List[str]] = {}
    for n in all_nodes:
        name_index.setdefault(n.name, []).append(n.id)

    resolved: List[FunctionEdge] = []
    external_nodes: Dict[str, FunctionNode] = {}
    for e in all_edges:
        tgt = e.target_id
        if tgt.startswith("<symbol>::"):
            short = tgt.split("::", 1)[1]
            candidates = name_index.get(short, [])
            for cid in candidates:
                if cid != e.source_id:
                    resolved.append(FunctionEdge(
                        type=e.type,
                        relationship=e.relationship,
                        source_id=e.source_id,
                        target_id=cid,
                        adapter_metadata=e.adapter_metadata,
                    ))
        elif tgt.startswith("<module>::"):
            module_name = tgt.split("::", 1)[1]
            external_id = f"external::module::{module_name}"
            external_nodes.setdefault(external_id, FunctionNode(
                id=external_id,
                name=module_name,
                file_path="",
                type="file",
                kind="external",
                source_language="unknown",
                qualified_name=module_name,
                adapter_metadata={"external_kind": "module"},
            ))
            resolved.append(FunctionEdge(
                type=e.type,
                relationship=e.relationship,
                source_id=e.source_id,
                target_id=external_id,
                adapter_metadata=e.adapter_metadata,
            ))
        else:
            resolved.append(e)

    all_nodes.extend(external_nodes.values())

    g = nx.DiGraph()
    for n in all_nodes:
        g.add_node(n.id, **n.model_dump())
    for e in resolved:
        if g.has_node(e.source_id) and g.has_node(e.target_id):
            g.add_edge(e.source_id, e.target_id, type=e.type, relationship=e.relationship)

    return g, all_nodes, [e for e in resolved if not e.target_id.startswith("<")]
