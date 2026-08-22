"""CIR graph construction via language-specific structural adapters.

Builds nodes (file/class/function/method) and edges (DEFINES, CALLS, IMPORTS, INHERITS).
The call graph is approximate — Python's dynamism means we resolve names heuristically:
a call like `foo()` matches any function named `foo` in the repo; `obj.bar()` matches
any method named `bar`.  Good enough for a hackathon visualization.
"""
from __future__ import annotations
import ast
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import networkx as nx

from .ingestion import detect_language, iter_source_files
from .models import CIRSignature, CIRSourceSpan, FunctionNode, FunctionEdge

logger = logging.getLogger(__name__)


def _node_id(file_path: str, qualname: str) -> str:
    return f"{file_path}::{qualname}"


def _dedupe_node_ids(nodes: "List[FunctionNode]") -> None:
    """Disambiguate node ids that collide within one ingest, in place.

    `_node_id()` is just `file::qualname`, but a single qualname can be
    defined more than once in the same file — most commonly `@overload`
    stubs (typing's legitimate way to give one function multiple type
    signatures), but also things like conditionally-redefined functions.
    Two nodes sharing an id crash storage.save()'s SQL insert (id+graph_id
    is a primary key), so every id beyond the first occurrence gets a
    `#<line>` suffix keyed on its start line, which is unique by
    construction (two definitions can't start on the same line).
    """
    seen: Dict[str, int] = {}
    for n in nodes:
        count = seen.get(n.id, 0)
        seen[n.id] = count + 1
        if count > 0:
            n.id = f"{n.id}#{n.lineno or count}"


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


def _detect_semantic_kind(name: str, code: str, decorators: list) -> str:
    """Detect the semantic role of a function from its name, code, and decorators.

    Returns one of: EXPOSES_API | USES_DB | EMITS_EVENT | CONSUMES_EVENT | CALLS
    """
    nl = name.lower()
    cl = code.lower()
    dec_str = " ".join(str(d) for d in decorators).lower()

    # API endpoint — decorator-based (Flask/FastAPI/Django/Express)
    api_dec_keywords = {"route", "get", "post", "put", "delete", "patch", "api_view",
                        "action", "endpoint", "resource", "view", "handler"}
    api_dec_prefixes = ("app.", "router.", "bp.", "blueprint.", "api.")
    if (any(k in dec_str for k in api_dec_keywords) or
            any(d.lower().startswith(p) for d in decorators for p in api_dec_prefixes)):
        return "EXPOSES_API"

    # DB interaction
    db_patterns = [
        "db.", ".session.", "session.query", ".execute(", "cursor.",
        ".save(", ".create(", ".filter(", ".objects.", ".commit(",
        "sqlalchemy", "select ", "insert into", "update set", "delete from",
        "repository.", "dao.", ".find_by", ".find_all", "knex.", "mongoose.",
    ]
    if any(p in cl for p in db_patterns):
        return "USES_DB"

    # Event emission
    emit_patterns = [
        ".emit(", ".publish(", ".dispatch(", "send_event(", "fire_event(",
        ".trigger(", "broadcast(", "event_bus.", "pubsub.", "kafka.produce",
        "rabbitmq.", ".send_message(", "sns.", "sqs.", "produce(",
    ]
    if any(p in cl for p in emit_patterns):
        return "EMITS_EVENT"

    # Event consumer — decorator or naming convention
    consumer_dec = {"on_", "listener", "subscriber", "consumer", "event_handler", "@on", "celery"}
    consumer_name_starts = ("on_", "handle_", "when_", "receive_", "process_event", "consume_")
    consumer_name_ends = ("_handler", "_listener", "_consumer", "_receiver", "_subscriber")
    if (any(k in dec_str for k in consumer_dec) or
            any(nl.startswith(p) for p in consumer_name_starts) or
            any(nl.endswith(s) for s in consumer_name_ends)):
        return "CONSUMES_EVENT"

    return "CALLS"


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
        # local name -> full imported path, e.g. "np" -> "numpy",
        # "ingest" -> "app.pipeline.ingest". Populated by visit_Import/
        # visit_ImportFrom, consumed by the call-edge resolver in
        # build_function_graph() (previously collected and never read).
        self.import_map: Dict[str, str] = {}
        # class qualname -> {attr_name: TypeName}, from `self.x = Foo(...)`
        # assignments seen while visiting __init__. Lets the resolver turn
        # `self._preprocessor.fit()` into a specific class's fit(), instead
        # of every fit() in the repo.
        self.class_attr_types: Dict[str, Dict[str, str]] = {}

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
            local = alias.asname or alias.name.split(".", 1)[0]
            self.import_map[local] = alias.name
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
            local = alias.asname or alias.name
            self.import_map[local] = full
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
        decorators = self._decorators(node)
        snippet = self._snippet(node)
        semantic_kind = _detect_semantic_kind(node.name, snippet, decorators)
        self.nodes.append(FunctionNode(
            id=nid, name=node.name, file_path=self.file_path,
            type=kind, kind="function", source_language="python",
            qualified_name=qual, source_span=self._source_span(node),
            signature=self._signature(node),
            code_snippet=snippet, lineno=node.lineno,
            adapter_metadata={
                "adapter": "python_ast",
                "decorators": decorators,
                "async": isinstance(node, ast.AsyncFunctionDef),
                "semantic_kind": semantic_kind,
            },
        ))
        parent = self.qual_stack[-1] if self.qual_stack else "<file>"
        parent_id = _node_id(self.file_path, parent if parent != "<file>" else "<file>")
        self.edges.append(FunctionEdge(
            type="DEFINES", relationship="CONTAINS",
            source_id=parent_id, target_id=nid,
            adapter_metadata={"adapter": "python_ast"},
        ))

        # enclosing_class is the FULL dotted qualname of whatever class(es)
        # this function/method is nested in, e.g. "Outer.Inner" — qual_stack
        # hasn't been pushed with this function's own name yet at this point,
        # so it holds exactly the enclosing chain. Used by the call-edge
        # resolver in build_function_graph() to turn `self.foo()` into the
        # one specific `EnclosingClass.foo`, instead of every `foo` in the repo.
        enclosing_class = ".".join(self.qual_stack) if self.qual_stack else None

        if node.name == "__init__" and enclosing_class:
            self._scan_self_attr_types(node, enclosing_class)

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
                    # receiver is the expression the call was made on, e.g.
                    # "self" for self.foo(), "self._preprocessor" for
                    # self._preprocessor.fit(), "np" for np.array(), or None
                    # for a bare foo(). This — plus enclosing_class and
                    # import_map/class_attr_types stashed on the file node —
                    # is what lets the resolver disambiguate instead of
                    # fanning out to every same-named function in the repo.
                    receiver = (
                        self._expr_name(sub.func.value)
                        if isinstance(sub.func, ast.Attribute) else None
                    )
                    self.edges.append(FunctionEdge(
                        type="CALLS", relationship="INVOKES",
                        source_id=nid, target_id=f"<symbol>::{callee}",
                        adapter_metadata={
                            "adapter": "python_ast", "callee": callee,
                            "receiver": receiver, "enclosing_class": enclosing_class,
                            "source_file": self.file_path,
                        },
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

    def _scan_self_attr_types(self, init_node, enclosing_class: str) -> None:
        """Record `self.x = SomeClass(...)` assignments from an __init__ body
        into self.class_attr_types[enclosing_class][x] = "SomeClass".

        This is what lets the resolver turn `self._preprocessor.fit()` into
        the one specific class's fit() instead of every fit() in the repo —
        by far the most common form of otherwise-unresolvable attribute
        access in typical OOP composition (sklearn-style pipelines, etc).
        Deliberately shallow: only direct `self.attr = Call(...)` at any
        depth in __init__ (covers the common case; doesn't attempt control
        flow, so a conditionally-reassigned attribute just keeps whichever
        assignment is walked last).
        """
        attr_types = self.class_attr_types.setdefault(enclosing_class, {})
        for sub in ast.walk(init_node):
            if not isinstance(sub, ast.Assign):
                continue
            if not (isinstance(sub.value, ast.Call) and isinstance(sub.value.func, (ast.Name, ast.Attribute))):
                continue
            type_name = self._call_name(sub.value.func)
            if not type_name or not type_name[:1].isupper():
                continue  # heuristic: only treat calls to Capitalized names as constructors
            for target in sub.targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                    attr_types[target.attr] = type_name


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
    # Stash per-file resolver context on the file node (v.nodes[0], appended
    # in __init__) rather than widening parse_file()'s return signature —
    # every language adapter shares that signature, and adapter_metadata is
    # already the free-form place for adapter-specific extras. Consumed by
    # the call-edge resolver in build_function_graph().
    v.nodes[0].adapter_metadata["import_map"] = v.import_map
    v.nodes[0].adapter_metadata["class_attr_types"] = v.class_attr_types
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
            # collect decorators from previous lines (simple JS annotation patterns)
            js_decorators = []
            for prev_idx in range(max(0, idx - 3), idx):
                prev = lines[prev_idx].strip()
                if prev.startswith("@"):
                    js_decorators.append(prev[1:])
            semantic_kind = _detect_semantic_kind(name, body, js_decorators)
            nodes.append(FunctionNode(
                id=nid, name=name, file_path=rel, type=kind, kind="function",
                source_language=language, qualified_name=qual,
                source_span=_source_span_from_lines(line_no, end_line),
                signature=CIRSignature(type_system="explicit" if language == "typescript" else "dynamic"),
                code_snippet=body, lineno=line_no,
                adapter_metadata={"adapter": adapter, "semantic_kind": semantic_kind},
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
            semantic_kind = _detect_semantic_kind(name, body, [])
            nodes.append(FunctionNode(id=nid, name=name, file_path=rel, type="method", kind="function", source_language="java", qualified_name=qual, source_span=_source_span_from_lines(line_no, end_line), signature=CIRSignature(type_system="explicit"), code_snippet=body, lineno=line_no, adapter_metadata={"adapter": adapter, "semantic_kind": semantic_kind}))
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


def _edges_from_pyan(
    repo_root: Path,
    python_files: List[str],
    node_index: Dict[Tuple[str, str], str],
) -> List[FunctionEdge]:
    """Return call edges derived from pyan's static analysis.

    *node_index* maps ``(repo-relative file path, function name)`` → CIR node ID.
    Any edge whose source or target can't be resolved in the index is silently
    dropped — pyan picks up more external-library calls than the AST pass, most
    of which don't have a CIR node.

    Falls back to an empty list on import error or any analysis failure so
    ingestion can never be broken by pyan.
    """
    try:
        from pyan.analyzer import CallGraphVisitor
        from pyan.node import Flavor
    except ImportError:
        logger.warning("pyan3 not installed — falling back to AST-only edges")
        return []

    if not python_files:
        return []

    func_flavors = {Flavor.FUNCTION, Flavor.METHOD, Flavor.CLASSMETHOD, Flavor.STATICMETHOD}

    try:
        visitor = CallGraphVisitor(python_files, root=str(repo_root))
    except Exception as exc:
        logger.warning("pyan analysis failed (%s) — falling back to AST edges", exc)
        return []

    edges: List[FunctionEdge] = []
    seen: set = set()

    for caller_node, callee_set in visitor.graph.uses_edges.items():
        if caller_node.flavor not in func_flavors:
            continue
        if not caller_node.defined or not caller_node.filename:
            continue
        try:
            caller_rel = Path(caller_node.filename).relative_to(repo_root).as_posix()
        except ValueError:
            continue
        src_id = node_index.get((caller_rel, caller_node.name))
        if not src_id:
            continue

        for callee_node in callee_set:
            if callee_node.flavor not in func_flavors:
                continue
            if not callee_node.defined or not callee_node.filename:
                continue
            try:
                callee_rel = Path(callee_node.filename).relative_to(repo_root).as_posix()
            except ValueError:
                continue
            tgt_id = node_index.get((callee_rel, callee_node.name))
            if not tgt_id or tgt_id == src_id:
                continue

            key = (src_id, tgt_id)
            if key not in seen:
                seen.add(key)
                edges.append(FunctionEdge(
                    type="CALLS",
                    relationship="INVOKES",
                    source_id=src_id,
                    target_id=tgt_id,
                    adapter_metadata={"source": "pyan"},
                ))

    logger.info("pyan contributed %d call edges", len(edges))
    return edges


def _qualname_suffix_match(name_index: Dict[str, List[str]], short: str, class_name: str) -> str | None:
    """Among nodes named *short*, return the one id whose qualname is
    exactly "<class_name>.<short>" — regardless of which file it's in
    (handles a class attribute typed from an imported class). Returns None
    if there isn't exactly one such match, so an ambiguous case (e.g. two
    unrelated classes in the repo happening to share a name) falls through
    to a lower tier or gets dropped, rather than guessing."""
    suffix = f"::{class_name}.{short}"
    matches = [nid for nid in name_index.get(short, []) if nid.endswith(suffix)]
    return matches[0] if len(matches) == 1 else None


def _resolve_call_target(
    e: FunctionEdge,
    short: str,
    name_index: Dict[str, List[str]],
    node_id_set: set,
    file_functions_index: Dict[str, Dict[str, str]],
    function_type_by_id: Dict[str, str],
    class_attr_types_by_file: Dict[str, Dict[str, Dict[str, str]]],
    file_import_maps: Dict[str, Dict[str, str]],
    file_stem_index: Dict[str, List[str]],
) -> Tuple[str, float, str] | None:
    """Resolve one `<symbol>::short` call target to at most one node id.

    Returns (target_id, confidence, reasoning) or None if unresolvable.
    Deliberately returns at most one id — never a list — so a caller with
    an ambiguous target gets either its one real callee or no edge at all,
    not an edge to every function that happens to share a name (which is
    what this replaced; see build_function_graph's comment on why that
    mattered in practice, not just in theory).

    Tiers, checked in order, first match wins:
      1. self.foo() inside a known enclosing class -> that class's foo
      2. self.attr.foo() where __init__ recorded attr's type -> TypeName.foo
      3. Receiver.foo() where Receiver is a class name in scope -> Receiver.foo
      4. module_alias.foo() where module_alias is an import (e.g. this
         codebase's own `from . import pipeline, storage, ...` style,
         `pipeline.ingest(...)`) -> foo in the file that alias points at
      5. bare foo() -> a function (not method) defined in the same file
      6. bare foo() -> a function (not method) uniquely named repo-wide
    Anything not caught by one of these is dropped rather than guessed at.
    Non-Python edges (no receiver/enclosing_class captured — see
    graph_builder.py's module docstring on the regex-based adapters) only
    ever reach tiers 5-6, at reduced confidence, since there's no receiver
    information to support the class/module-aware tiers at all.
    """
    meta = e.adapter_metadata or {}
    is_python = meta.get("adapter") == "python_ast"
    receiver = meta.get("receiver")
    enclosing_class = meta.get("enclosing_class")
    source_file = meta.get("source_file") or e.source_id.split("::", 1)[0]

    if is_python and receiver == "self" and enclosing_class:
        cid = _node_id(source_file, f"{enclosing_class}.{short}")
        if cid in node_id_set:
            return cid, 0.95, "self-call resolved within the enclosing class"

    if is_python and receiver and receiver.startswith("self.") and enclosing_class:
        attr = receiver.split(".", 1)[1]
        attr_type = class_attr_types_by_file.get(source_file, {}).get(enclosing_class, {}).get(attr)
        if attr_type:
            cid = _qualname_suffix_match(name_index, short, attr_type)
            if cid:
                return cid, 0.90, f"resolved via self.{attr}'s recorded type ({attr_type})"

    if is_python and receiver and "." not in receiver and receiver not in ("self", "cls"):
        cid = _qualname_suffix_match(name_index, short, receiver)
        if cid:
            return cid, 0.85, f"resolved via receiver class {receiver}"

        imported = file_import_maps.get(source_file, {}).get(receiver)
        if imported:
            stem = imported.rsplit(".", 1)[-1]
            candidate_files = file_stem_index.get(stem, [])
            matches = {
                file_functions_index[fp][short]
                for fp in candidate_files
                if short in file_functions_index.get(fp, {})
            }
            if len(matches) == 1:
                return next(iter(matches)), 0.90, f"resolved via imported module alias {receiver} ({imported})"

    if receiver is None:
        same_file = file_functions_index.get(source_file, {}).get(short)
        if same_file:
            return same_file, 0.80 if is_python else 0.65, "same-file function definition"

    # Last resort, tried whether or not there's a receiver: if exactly one
    # *function* (not method) in the whole repo has this name, that's a
    # strong enough signal to use even when the receiver couldn't be traced
    # to its defining file — e.g. `bob_client.ask_json(...)` where
    # bob_client.py is a pass-through shim that imports and re-exports
    # ask_json rather than defining it, so the module-alias tier above
    # can't find it there, but it's still uniquely named across the repo.
    # Still gated on true uniqueness, so this can't reintroduce fan-out.
    candidates = [nid for nid in name_index.get(short, []) if function_type_by_id.get(nid) == "function"]
    if len(candidates) == 1:
        conf = (0.65 if is_python else 0.50) if receiver is None else 0.55
        return candidates[0], conf, "unique repo-wide function match"

    return None


def build_function_graph(repo_path: str) -> Tuple[nx.DiGraph, List[FunctionNode], List[FunctionEdge]]:
    repo_root = Path(repo_path).resolve()
    all_nodes: List[FunctionNode] = []
    all_edges: List[FunctionEdge] = []
    for fp in iter_source_files(repo_path):
        nodes, edges = parse_file(fp, repo_root)
        all_nodes.extend(nodes)
        all_edges.extend(edges)

    _dedupe_node_ids(all_nodes)

    # Resolve <symbol>:: targets to real node ids. See _resolve_call_target's
    # docstring for the tiered strategy — the key property is that each edge
    # resolves to at most ONE target (or is dropped), never a fan-out to
    # every same-named candidate. That fan-out used to be the behavior here
    # and it was bad enough to distort the product's own "most important
    # functions" ranking: on a real repo, every same-named __init__ ended up
    # "calling" every other __init__, making them the top-ranked nodes by
    # out-degree purely from the artifact.
    name_index: Dict[str, List[str]] = {}
    file_functions_index: Dict[str, Dict[str, str]] = {}
    function_type_by_id: Dict[str, str] = {}
    class_attr_types_by_file: Dict[str, Dict[str, Dict[str, str]]] = {}
    file_import_maps: Dict[str, Dict[str, str]] = {}
    node_id_set: set = set()
    for n in all_nodes:
        name_index.setdefault(n.name, []).append(n.id)
        node_id_set.add(n.id)
        function_type_by_id[n.id] = n.type
        if n.type == "function":
            file_functions_index.setdefault(n.file_path, {})[n.name] = n.id
        if n.kind == "file":
            meta = n.adapter_metadata or {}
            if meta.get("class_attr_types"):
                class_attr_types_by_file[n.file_path] = meta["class_attr_types"]
            if meta.get("import_map"):
                file_import_maps[n.file_path] = meta["import_map"]
    # file path stem (no dir, no extension) -> that file's path. Used to turn
    # a module-alias call like `pipeline.ingest(...)` (very common in this
    # codebase's own style: `from . import pipeline, storage, ...`) into
    # "look for `ingest` specifically in a file named pipeline.*" rather than
    # searching the whole repo by short name alone.
    file_stem_index: Dict[str, List[str]] = {}
    for fp in file_functions_index:
        file_stem_index.setdefault(Path(fp).stem, []).append(fp)

    resolved: List[FunctionEdge] = []
    external_nodes: Dict[str, FunctionNode] = {}
    for e in all_edges:
        tgt = e.target_id
        if tgt.startswith("<symbol>::"):
            short = tgt.split("::", 1)[1]
            hit = _resolve_call_target(
                e, short, name_index, node_id_set, file_functions_index,
                function_type_by_id, class_attr_types_by_file,
                file_import_maps, file_stem_index,
            )
            # Drop self-loops (matches the previous resolver's explicit
            # `cid != e.source_id` guard). The tiered resolver is precise
            # enough that a self-loop here would usually mean genuine
            # recursion (e.g. a static helper calling itself via its own
            # class name) rather than a resolution artifact, but the graph
            # model/UI/analytics weren't built with self-loops in mind, so
            # this keeps that existing invariant rather than changing it
            # as a side effect of this fix.
            if hit is not None and hit[0] == e.source_id:
                hit = None
            if hit is not None:
                cid, confidence, reasoning = hit
                resolved.append(FunctionEdge(
                    type=e.type,
                    relationship=e.relationship,
                    source_id=e.source_id,
                    target_id=cid,
                    adapter_metadata={
                        **(e.adapter_metadata or {}),
                        "resolution_confidence": confidence,
                        "resolution_reasoning": reasoning,
                    },
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

    # Optional pyan augmentation — enabled by FLOWIFY_PYTHON_EDGE_BACKEND=pyan
    if os.environ.get("FLOWIFY_PYTHON_EDGE_BACKEND", "ast").lower() == "pyan":
        python_files = [
            str(fp) for fp in iter_source_files(repo_path)
            if detect_language(fp) == "python"
        ]
        pyan_index: Dict[Tuple[str, str], str] = {
            (n.file_path, n.name): n.id
            for n in all_nodes
            if n.source_language == "python" and n.kind in ("function", "callable")
        }
        existing_pairs = {(e.source_id, e.target_id) for e in resolved}
        for e in _edges_from_pyan(repo_root, python_files, pyan_index):
            if (e.source_id, e.target_id) not in existing_pairs:
                resolved.append(e)
                existing_pairs.add((e.source_id, e.target_id))

    # Deduplicate edges: the regex extractor fires once per call-site occurrence,
    # so a function that calls foo() in two branches produces two identical edges.
    seen_edge_keys: set = set()
    deduped: List[FunctionEdge] = []
    for e in resolved:
        key = (e.source_id, e.target_id, e.type)
        if key not in seen_edge_keys:
            seen_edge_keys.add(key)
            deduped.append(e)
    resolved = deduped

    g = nx.DiGraph()
    for n in all_nodes:
        g.add_node(n.id, **n.model_dump())
    for e in resolved:
        if g.has_node(e.source_id) and g.has_node(e.target_id):
            g.add_edge(e.source_id, e.target_id, type=e.type, relationship=e.relationship)

    return g, all_nodes, [e for e in resolved if not e.target_id.startswith("<")]
