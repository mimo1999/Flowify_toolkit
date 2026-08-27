"""Agent-agnostic LLM provider layer.

Select a provider via the LLM_PROVIDER environment variable:

    LLM_PROVIDER=ollama     Ollama (local, no key needed — http://localhost:11434)
    LLM_PROVIDER=bob        IBM Bob / watsonx (BOB_API_KEY + BOB_API_URL)
    LLM_PROVIDER=claude     Anthropic Claude  (ANTHROPIC_API_KEY, opt. ANTHROPIC_MODEL)
    LLM_PROVIDER=openai     OpenAI / Codex    (OPENAI_API_KEY, opt. OPENAI_BASE_URL, OPENAI_MODEL)
    LLM_PROVIDER=copilot    GitHub Copilot    (GITHUB_TOKEN, opt. COPILOT_MODEL)
    LLM_PROVIDER=openclaw   OpenClaw          (OPENCLAW_API_KEY + OPENCLAW_API_URL)
    LLM_PROVIDER=heuristic  No LLM (always available, deterministic stubs)

Ollama-specific env vars (all optional):
    OLLAMA_HOST    Base URL of the Ollama server  (default: http://localhost:11434)
    OLLAMA_MODEL   Model to use                   (default: auto-picked from installed models)

Auto-detection order (when LLM_PROVIDER is not set):
    1. Ollama running on localhost:11434  (no key needed)
    2. BOB_API_KEY present
    3. ANTHROPIC_API_KEY present
    4. OPENAI_API_KEY present
    5. GITHUB_TOKEN present
    6. OPENCLAW_API_KEY present
    7. Heuristic stubs

All providers share the same disk cache (keyed by prompt hash) and expose the
same high-level interface so the rest of the pipeline is completely unaware of
which backend is active.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import textwrap
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

import requests

# ---------------------------------------------------------------------------
# Shared disk cache
# ---------------------------------------------------------------------------

_CACHE_DIR = Path(os.environ.get("FLOWIFY_STORE", "_store")) / "llm_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(prompt: str, namespace: str = "") -> Path:
    # namespace (provider class + model) is mixed into the hash so a BYO-key
    # request using e.g. gpt-4o-mini never reads or writes a cache entry
    # produced by another provider/model for the same prompt text — without
    # this, two users with different providers would silently share (and
    # potentially leak) each other's cached LLM responses.
    h = hashlib.sha256(f"{namespace}\n{prompt}".encode()).hexdigest()[:32]
    return _CACHE_DIR / f"{h}.json"


def _is_stub_response(text: str) -> bool:
    """Return True if *text* is a heuristic stub or provider error — not worth caching."""
    return text.startswith("(stub)") or any(
        marker in text
        for marker in (
            "[llm error:", "[ollama error:", "[bob error:", "[openai error:", "[claude error:",
            "[llm unavailable",
        )
    )


def _cache_get(prompt: str, namespace: str = "") -> Optional[str]:
    p = _cache_path(prompt, namespace)
    if p.exists():
        try:
            val = json.loads(p.read_text(encoding="utf-8"))["response"]
            if val and not _is_stub_response(val):
                return val
            # Stale stub/error entry — evict it so the real LLM is retried next call
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        except Exception:
            pass
    return None


def _cache_put(prompt: str, response: str, namespace: str = "") -> None:
    """Write *response* to the disk cache.  Stubs and errors are never cached."""
    if _is_stub_response(response):
        return
    try:
        _cache_path(prompt, namespace).write_text(
            json.dumps({"prompt": prompt[:500], "response": response}),
            encoding="utf-8",
        )
    except Exception:
        pass  # Cache writes are non-fatal


# ---------------------------------------------------------------------------
# Heuristic helpers (no LLM required)
# ---------------------------------------------------------------------------

_NON_ENTRY_FILE_STEMS = {
    "__init__", "constants", "config", "settings", "data_loader", "loader",
    "trainer", "evaluator", "factory", "utils", "util", "helpers", "helper",
    "models", "model", "dataset", "preprocessor", "preprocessing", "logger",
    "saver", "tracker", "batcher", "cycler",
}


def _rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def _module_to_path(module: str, root: Path) -> Optional[str]:
    c = root / (module.replace(".", "/") + ".py")
    if c.exists():
        return _rel(c, root)
    m = root / module.replace(".", "/") / "__main__.py"
    if m.exists():
        return _rel(m, root)
    return None


def _python_module_invocations(text: str) -> list[str]:
    found = []
    for m in re.finditer(r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+([A-Za-z_][\w.]*)", text):
        name = m.group(1).rstrip(".")
        if name and name not in found:
            found.append(name)
    return found


def _score_entry_file(path: Path, root: Path, command_modules: set) -> int:
    rel = _rel(path, root)
    stem = path.stem.lower()
    score = 0
    if _rel(path, root).replace("/", ".").removesuffix(".py") in command_modules:
        score += 100
    if path.name in {"main.py", "__main__.py"}:
        score += 35
    if stem in {"main", "ml_main", "train", "run", "start", "cli"}:
        score += 25
    if any(p in {"training", "train", "pipeline"} for p in rel.lower().split("/")):
        score += 10
    if stem in _NON_ENTRY_FILE_STEMS:
        score -= 80
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return score
    if 'if __name__' in content and '__main__' in content:
        score += 35
    if "argparse.ArgumentParser" in content and "parse_args" in content:
        score += 25
    if re.search(r"\bdef\s+main\s*\(", content):
        score += 20
    if re.search(r"\bmain\s*\(\s*\)", content) and "__main__" in content:
        score += 20
    return score


def _discover_entry_points(repo_root: Path) -> list[str]:
    command_modules: set[str] = set()
    sources = ["README.md", "README.txt", "README.rst", "README"]
    sources += [p.name for p in repo_root.glob("*.sh")]
    sources += [p.name for p in repo_root.glob("*.bat")]
    sources += [p.name for p in repo_root.glob("*.ps1")]
    for name in dict.fromkeys(sources):
        fp = repo_root / name
        if fp.exists() and fp.is_file():
            try:
                command_modules.update(_python_module_invocations(fp.read_text(encoding="utf-8", errors="ignore")))
            except Exception:
                pass

    scored: list[tuple[int, str, str]] = []
    for mod in sorted(command_modules):
        rel = _module_to_path(mod, repo_root)
        if rel:
            scored.append((200, rel, mod))

    if scored:
        scored.sort(key=lambda t: t[1])
        return [m for _, _, m in scored[:10]]

    for py in repo_root.rglob("*.py"):
        if any(p.startswith(".") or p in {"venv", ".venv", "__pycache__", "node_modules"} for p in py.parts):
            continue
        rel = _rel(py, repo_root)
        mod = rel[:-3].replace("/", ".")
        if any(r == rel for _, r, _ in scored):
            continue
        s = _score_entry_file(py, repo_root, command_modules)
        if s >= 40:
            scored.append((s, rel, mod))

    scored.sort(key=lambda t: (-t[0], t[1]))
    return [m for _, _, m in scored[:10]]


def _heuristic_repo_analysis(repo_path: str) -> dict:
    root = Path(repo_path).resolve()
    project_type, domain, architecture, tech_stack, purpose = "unknown", "general", "unknown", [], ""

    if (root / "requirements.txt").exists():
        tech_stack.append("Python")
        req = (root / "requirements.txt").read_text(encoding="utf-8", errors="ignore").lower()
        if "fastapi" in req:
            project_type, tech_stack = "web_api", tech_stack + ["FastAPI"]
        elif "flask" in req:
            project_type, tech_stack = "web_api", tech_stack + ["Flask"]
        elif "django" in req:
            project_type, tech_stack = "web_api", tech_stack + ["Django"]
        elif "click" in req or "argparse" in req:
            project_type = "cli_tool"
        elif any(k in req for k in ("scikit-learn", "tensorflow", "pytorch")):
            project_type, domain = "ml_model", "machine_learning"
        if "networkx" in req:
            tech_stack.append("NetworkX")
        if "pydantic" in req:
            tech_stack.append("Pydantic")

    if (root / "package.json").exists():
        tech_stack.append("JavaScript/Node.js")
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "react" in deps:
                tech_stack.append("React")
            if "vue" in deps:
                tech_stack.append("Vue")
            if "express" in deps:
                project_type, tech_stack = "web_api", tech_stack + ["Express"]
        except Exception:
            pass

    dirs = [d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if "app" in dirs or "src" in dirs:
        architecture = "modular_pipeline"
    if "api" in dirs and "services" in dirs:
        architecture = "microservices"
    if "models" in dirs and "views" in dirs and "controllers" in dirs:
        architecture = "mvc"
    if "backend" in dirs and "frontend" in dirs:
        architecture = "layered"

    for readme in ["README.md", "README.txt", "README.rst", "README"]:
        rp = root / readme
        if rp.exists():
            try:
                lines = [l.strip() for l in rp.read_text(encoding="utf-8", errors="ignore").split("\n")
                         if l.strip() and not l.startswith("#")]
                if lines:
                    purpose = lines[0][:200]
                break
            except Exception:
                pass

    domain_kws = {
        "code_analysis": ["ast", "parser", "analyzer", "graph", "flow"],
        "web_development": ["api", "server", "client", "http", "rest"],
        "data_processing": ["etl", "pipeline", "transform", "ingest"],
        "machine_learning": ["model", "train", "predict", "dataset"],
        "devops": ["deploy", "docker", "kubernetes", "ci", "cd"],
    }
    repo_text = " ".join(dirs).lower() + " " + purpose.lower()
    scored_domains = {d: sum(1 for k in kws if k in repo_text) for d, kws in domain_kws.items()}
    best = max(scored_domains.items(), key=lambda x: x[1])
    if best[1] > 0:
        domain = best[0]

    return {
        "project_type": project_type,
        "domain": domain,
        "architecture": architecture,
        "tech_stack": tech_stack,
        "purpose": purpose or f"A {project_type} project",
        "key_entry_points": _discover_entry_points(root),
        "critical_modules": dirs[:5],
        "confidence": 0.5,
        "fallback_used": True,
    }


def _heuristic_semantic_analysis(name: str, code: str, repo_context: dict) -> dict:
    nl, cl = name.lower(), code.lower()
    intent = "unknown"
    for kws, label in [
        (["save", "store", "write", "persist", "insert", "update"], "persistence"),
        (["load", "get", "fetch", "read", "retrieve", "find", "query"], "retrieval"),
        (["validate", "check", "verify", "ensure"], "validation"),
        (["transform", "convert", "parse", "process", "build"], "transformation"),
        (["handle", "catch", "error", "exception"], "error_handling"),
        (["config", "setup", "init", "configure"], "configuration"),
        (["orchestrate", "coordinate", "manage", "run", "execute"], "orchestration"),
        (["compute", "calculate", "sum", "count"], "computation"),
        (["render", "display", "show", "format"], "presentation"),
    ]:
        if any(k in nl for k in kws):
            intent = label
            break

    lines = len(code.split("\n"))
    complexity = "low" if lines < 20 else "medium" if lines < 50 else "high" if lines < 100 else "very_high"

    criticality = "medium"
    if any(k in nl for k in ["main", "core", "critical", "essential"]):
        criticality = "high"
    elif any(k in nl for k in ["helper", "util", "aux", "temp"]):
        criticality = "low"

    side_effects = (
        (["open(", "file", "write", "read"], "file_io"),
        (["requests.", "http", "socket", "urllib"], "network"),
        (["db.", "database", "sql", "query", "session"], "database"),
        (["self.", "global ", "nonlocal "], "state_mutation"),
        (["log", "print", "debug"], "logging"),
    )
    effects = [label for kws, label in side_effects if any(k in cl for k in kws)] or ["none"]

    patterns = []
    if "yield " in code:
        patterns.append("generator")
    if "async " in code or "await " in code:
        patterns.append("async")
    if "@" in code and "def " in code:
        patterns.append("decorator")

    return {
        "intent": intent, "complexity": complexity, "criticality": criticality,
        "patterns": patterns, "side_effects": effects,
        "confidence": 0.4,
    }


# ---------------------------------------------------------------------------
# Abstract base provider
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """Abstract provider. Subclasses only need to implement `ask()`."""

    # -- core -----------------------------------------------------------------

    @abstractmethod
    def _call(self, prompt: str) -> str:
        """Make a raw LLM call and return the text response."""

    def _cache_namespace(self) -> str:
        """Provider class + model, mixed into the cache key so different
        providers/models never share a cache entry. Override in subclasses
        that have a `self.model`; defaults to just the class name."""
        return f"{type(self).__name__}:{getattr(self, 'model', '')}"

    def ask(self, prompt: str) -> str:
        """Cached wrapper around `_call`.

        Only real LLM responses are cached — stubs and errors are never written
        to disk so the live LLM is retried on the next call.
        """
        ns = self._cache_namespace()
        cached = _cache_get(prompt, ns)
        if cached is not None:
            return cached
        try:
            out = self._call(prompt)
        except Exception as exc:
            # Provider unavailable — return heuristic stub but do NOT cache so
            # the next call will try the live LLM again. The exception detail
            # (upstream status/URL, occasionally response body) is logged
            # server-side only — it has no business reaching a user-visible
            # query/summary response, so the fallback text stays generic.
            print(f"[llm error] {type(self).__name__}: {exc}")
            return HeuristicProvider()._call(prompt) + "\n[llm unavailable — showing a heuristic answer]"
        _cache_put(prompt, out, ns)  # _cache_put silently skips stubs/errors
        return out

    def ask_json(self, prompt: str, fallback: dict) -> dict:
        raw = self.ask(prompt)
        try:
            start, end = raw.index("{"), raw.rindex("}") + 1
            return json.loads(raw[start:end])
        except Exception:
            return fallback

    # -- high-level methods used by the pipeline ------------------------------

    def summarize_function(self, name: str, code: str, kind: str = "function") -> str:
        """Return exactly one sentence describing what this function/class does."""
        raw = self.ask(textwrap.dedent(f"""
            You are producing a one-line description for a code map.
            {kind.capitalize()}: {name}

            ```
            {code[:1200]}
            ```

            Reply with exactly ONE sentence (≤20 words) starting with a verb.
            Describe what it does, not how. No markdown, no bullet points.
        """).strip()).strip()
        # Keep only the first sentence, strip markdown artefacts
        raw = re.sub(r"^[*`#\-\s]+", "", raw)
        first = re.split(r"(?<=[.!?])\s+", raw)[0]
        return first[:200] if first else raw[:200]

    def summarize_module(self, name_hint: str, summaries: List[str]) -> dict:
        joined = "\n".join(f"- {s}" for s in summaries[:30])
        raw = self.ask(textwrap.dedent(f"""
            Given these function summaries, propose a coherent module abstraction.

            Functions:
            {joined}

            Respond as JSON with keys: name (short label, 2-4 words),
            description (one sentence). Hint: {name_hint}
        """).strip())
        try:
            start, end = raw.index("{"), raw.rindex("}") + 1
            obj = json.loads(raw[start:end])
            return {"name": obj.get("name", name_hint), "description": obj.get("description", "")}
        except Exception:
            return {"name": name_hint, "description": (raw.splitlines()[0][:200] if raw else "")}

    def explain_flow_with_graph(
        self,
        query: str,
        summaries: List[str],
        edge_info: Optional[List[str]] = None,
        prior_turns: Optional[List[dict]] = None,
    ) -> str:
        """Graph-grounded explanation — explicitly cites graph edges and node roles.

        When *prior_turns* is provided (list of ``{"query": str, "summary": str}``),
        the last two turns are prepended so the LLM can maintain follow-up context.
        """
        joined = "\n".join(f"{i+1}. {s}" for i, s in enumerate(summaries))
        edge_block = ""
        if edge_info:
            edge_block = "\n\nGraph edges (call relationships):\n" + "\n".join(edge_info[:12])

        conv_block = ""
        if prior_turns:
            conv_lines = []
            for t in prior_turns[-2:]:
                conv_lines.append(f"Q: {t['query']}")
                conv_lines.append(f"A: {t['summary'][:200]}")
            if conv_lines:
                conv_block = (
                    "\n\nPrior conversation context — use for follow-up continuity:\n"
                    + "\n".join(conv_lines)
                    + "\n"
                )

        return self.ask(textwrap.dedent(f"""
            You are answering a developer question using a repository knowledge graph.
            The answer is grounded in the actual call graph and semantic metadata — not source file text.
            {conv_block}
            Question: {query}

            Repository knowledge graph — relevant nodes (ordered by execution flow):
            {joined}{edge_block}

            Using ONLY the graph data above, explain:
            1. Which component handles the request first and why
            2. The call chain through the codebase (reference node names directly)
            3. Any DB access, event emission, or API exposure visible in the graph

            Be specific about function names. Say "according to the graph" to reinforce grounding.
        """).strip()).strip()

    def interpret_query(
        self,
        query: str,
        candidates: List[str],
        context: Optional[dict] = None,
    ) -> List[str]:
        """Pick the most relevant symbols for *query* from *candidates*.

        *context* is an optional ``{name: summary}`` dict.  When provided, each
        candidate line is formatted as ``name — summary`` so the LLM can match
        on *meaning*, not just token overlap in the identifier.
        """
        if not candidates:
            return []
        # Build prompt lines: "name — one-line summary" when context is available
        if context:
            lines = [
                f"{n} — {context[n][:80]}" if context.get(n) else n
                for n in candidates[:30]
            ]
        else:
            lines = candidates[:30]
        raw = self.ask(textwrap.dedent(f"""
            You are matching a developer query to function names in a codebase.

            Query: {query}

            Candidate functions (name — description):
            {chr(10).join(lines)}

            Return the names of up to 10 functions most relevant to the query.
            Output ONE function name per line — the plain name only, no description,
            no numbering, no commentary.
        """).strip())
        # Strip any trailing " — description" the LLM might echo back
        return [re.sub(r"\s*[—–-].*$", "", l).strip() for l in raw.splitlines() if l.strip()][:10]

    def summarize_repository_flow(self, evidence: dict) -> dict:
        """Produce the prose sections of a repository flow summary.

        *evidence* is the compact module-level pack assembled by the pipeline:
        ``{purpose, project_type, domain, tech_stack, modules: [{module,
        description, is_entry, key_functions: [{name, summary, intent}]}]}``.

        Returns a dict with keys: ``what``, ``usecase``, ``techniques`` (list),
        and ``module_flows`` (``{module_name: description}``).  The deterministic
        sections (architecture diagram, tech stack) are added by the caller.
        """
        mod_lines = []
        for m in evidence.get("modules", []):
            tag = " [entry]" if m.get("is_entry") else ""
            fns = ", ".join(
                f"{f['name']}({f.get('intent') or '?'})" for f in m.get("key_functions", [])[:5]
            )
            mod_lines.append(f"- {m['module']}{tag}: {m.get('description','')}  | key fns: {fns}")
        modules_block = "\n".join(mod_lines) if mod_lines else "(no modules detected)"

        prompt = textwrap.dedent(f"""
            You are documenting a codebase for an engineer who has never seen it.
            Below is module-level evidence extracted from its call graph.

            Purpose hint: {evidence.get('purpose','unknown')}
            Project type: {evidence.get('project_type','unknown')}
            Domain: {evidence.get('domain','general')}
            Tech stack: {', '.join(evidence.get('tech_stack', [])) or 'unknown'}

            Modules (in execution order, entry points first):
            {modules_block}

            Respond as JSON with EXACTLY these keys:
            - "what": 2-3 sentences on what this code does.
            - "usecase": 1-2 sentences on the problem it solves.
            - "techniques": array of strings — model types, API styles (REST/RPC),
              algorithms, notable design patterns. Empty array if none evident.
            - "module_flows": object mapping each module name above to a one-line
              description phrased as "<thing> is performed via A, B, C". Use the
              module's key functions to ground each line.

            Use ONLY the evidence above. Do not invent modules or technologies.
        """).strip()

        return self.ask_json(prompt, fallback={
            "what": evidence.get("purpose", ""),
            "usecase": "",
            "techniques": [],
            "module_flows": {},
        })

    def analyze_repository(self, repo_path: str) -> dict:
        heuristic = _heuristic_repo_analysis(repo_path)
        root = Path(repo_path).resolve()

        context_files: dict[str, str] = {}
        for name in ["README.md", "README.txt", "requirements.txt", "package.json",
                     "pyproject.toml", "environment.yml"]:
            fp = root / name
            if fp.exists():
                try:
                    context_files[name] = fp.read_text(encoding="utf-8", errors="ignore")[:2000]
                except Exception:
                    pass
        for fp in list(root.glob("*.sh")) + list(root.glob("*.bat")) + list(root.glob("*.ps1")):
            try:
                context_files[fp.name] = fp.read_text(encoding="utf-8", errors="ignore")[:2000]
            except Exception:
                pass

        if not context_files:
            return heuristic

        py_files = sorted(
            str(f.relative_to(root)).replace("\\", "/")
            for f in root.rglob("*.py")
            if not any(p in {"venv", ".venv", "__pycache__", "node_modules"} for p in f.parts)
        )
        files_text = "\n\n".join(f"=== {n} ===\n{c}" for n, c in context_files.items())

        prompt = textwrap.dedent(f"""
            Analyze this repository and provide a structured assessment.

            Repository files:
            {files_text}

            Directory structure: {", ".join(heuristic.get("critical_modules", []))}
            Python files: {", ".join(py_files[:80])}
            Heuristic entry point candidates: {", ".join(heuristic.get("key_entry_points", []))}

            Respond as JSON with these keys:
            - project_type: one of [web_api, cli_tool, library, desktop_app, data_pipeline, ml_model, microservices, unknown]
            - domain: business/technical domain
            - architecture: one of [monolith, modular_pipeline, microservices, mvc, layered, event_driven, plugin, unknown]
            - purpose: one-sentence description
            - data_flow_pattern: high-level data flow description
            - key_entry_points: array of 2-5 runnable module names or file paths

            Be concise and specific.
        """).strip()

        try:
            raw = self.ask(prompt)
            m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw, re.DOTALL)
            if m:
                result = json.loads(m.group())
                llm_eps = result.get("key_entry_points", [])
                if isinstance(llm_eps, list) and llm_eps:
                    seen: list[str] = []
                    for ep in heuristic["key_entry_points"] + llm_eps:
                        norm = str(ep).strip().replace("\\", "/")
                        if norm and norm not in seen:
                            seen.append(norm)
                    entry_points = seen[:10]
                else:
                    entry_points = heuristic["key_entry_points"]

                return {
                    "project_type": result.get("project_type", heuristic["project_type"]),
                    "domain": result.get("domain", heuristic["domain"]),
                    "architecture": result.get("architecture", heuristic["architecture"]),
                    "tech_stack": heuristic["tech_stack"],
                    "purpose": result.get("purpose", heuristic["purpose"]),
                    "key_entry_points": entry_points,
                    "critical_modules": heuristic["critical_modules"],
                    "confidence": 0.85,
                    "fallback_used": False,
                }
        except Exception:
            pass

        return heuristic

    def analyze_function_semantics(
        self,
        name: str,
        code: str,
        repo_context: dict,
        neighbors: Optional[List[str]] = None,
    ) -> dict:
        heuristic = _heuristic_semantic_analysis(name, code, repo_context)

        ctx = "\n".join([
            f"Project type: {repo_context.get('project_type', 'unknown')}",
            f"Domain: {repo_context.get('domain', 'general')}",
            f"Architecture: {repo_context.get('architecture', 'unknown')}",
            *(([f"Related functions: {', '.join(neighbors[:5])}"]) if neighbors else []),
        ])

        prompt = textwrap.dedent(f"""
            Analyze this function semantically for a code graph.

            Context:
            {ctx}

            Function: {name}
            ```
            {code[:1000]}
            ```

            Respond as JSON with these keys:
            - intent: one of [orchestration, transformation, validation, persistence, retrieval, configuration, error_handling, computation, presentation, unknown]
            - complexity: one of [low, medium, high, very_high]
            - criticality: one of [low, medium, high, critical]
            - patterns: list of design patterns used
            - side_effects: list from [file_io, network, database, state_mutation, logging, none]
            - semantic_edges: list of {{type, target_name, description}} where type is one of [TRANSFORMS, VALIDATES, ORCHESTRATES, PERSISTS, RETRIEVES, CONFIGURES, HANDLES]
        """).strip()

        try:
            raw = self.ask(prompt)
            m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw, re.DOTALL)
            if m:
                r = json.loads(m.group())
                return {
                    "intent": r.get("intent", heuristic["intent"]),
                    "complexity": r.get("complexity", heuristic["complexity"]),
                    "criticality": r.get("criticality", heuristic["criticality"]),
                    "patterns": r.get("patterns", heuristic["patterns"]),
                    "side_effects": r.get("side_effects", heuristic["side_effects"]),
                    "data_flow": r.get("data_flow"),
                    "semantic_edges": r.get("semantic_edges", []),
                    "confidence": 0.8,
                }
        except Exception:
            pass

        return heuristic

    def analyze_functions_batch(
        self,
        items: List[dict],
        repo_context: dict,
    ) -> List[dict]:
        """Summarize AND semantically analyze a BATCH of functions in one call.

        *items* is a list of ``{name, code, kind, neighbors}`` dicts.  Returns a
        list of analysis dicts aligned to *items* (same length/order), each with
        keys: summary, intent, complexity, criticality, patterns, side_effects,
        semantic_edges, confidence.

        This collapses the old two-call-per-function design (summarize + analyze)
        into a single batched call, the dominant lever for cutting LLM cost.
        Anything the model omits or mangles falls back to per-function heuristics
        so the result always covers every input.
        """
        def _fallback(it: dict) -> dict:
            h = _heuristic_semantic_analysis(it["name"], it.get("code", ""), repo_context)
            code = it.get("code", "")
            h["summary"] = (
                _code_based_description(it["name"], code, it.get("kind", "function"))
                if code.strip() else _heuristic_one_liner(it["name"], it.get("kind", "function"))
            )
            h["semantic_edges"] = []
            return h

        if not items:
            return []

        ctx = "\n".join([
            f"Project type: {repo_context.get('project_type', 'unknown')}",
            f"Domain: {repo_context.get('domain', 'general')}",
            f"Architecture: {repo_context.get('architecture', 'unknown')}",
        ])

        blocks = []
        for i, it in enumerate(items):
            nb = it.get("neighbors") or []
            nb_line = f"\n  neighbors: {', '.join(nb[:5])}" if nb else ""
            blocks.append(
                f"[{i}] {it.get('kind', 'function')} {it['name']}{nb_line}\n"
                f"```\n{it.get('code', '')[:600]}\n```"
            )
        joined = "\n\n".join(blocks)

        prompt = textwrap.dedent(f"""
            You are analyzing functions for a code graph. Context:
            {ctx}

            For EACH numbered function below, produce one analysis object.

            Functions:
            {joined}

            Respond with ONLY a JSON array of objects (no prose), one per function.
            Each object MUST have:
            - id: the function's number (integer, matching the [N] label)
            - summary: ONE sentence (<=20 words) starting with a verb, what it does
            - intent: one of [orchestration, transformation, validation, persistence, retrieval, configuration, error_handling, computation, presentation, unknown]
            - complexity: one of [low, medium, high, very_high]
            - criticality: one of [low, medium, high, critical]
            - patterns: list of design pattern names (may be empty)
            - side_effects: list from [file_io, network, database, state_mutation, logging, none]
            - semantic_edges: list of {{type, target_name, description}} where type is one of [TRANSFORMS, VALIDATES, ORCHESTRATES, PERSISTS, RETRIEVES, CONFIGURES, HANDLES]
        """).strip()

        try:
            raw = self.ask(prompt)
            parsed = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
            if not isinstance(parsed, list):
                raise ValueError("not a JSON array")
        except Exception:
            return [_fallback(it) for it in items]

        # Index parsed objects by their declared id; fall back to positional.
        by_id: Dict[int, dict] = {}
        for pos, obj in enumerate(parsed):
            if not isinstance(obj, dict):
                continue
            try:
                by_id[int(obj.get("id", pos))] = obj
            except (TypeError, ValueError):
                by_id.setdefault(pos, obj)

        results: List[dict] = []
        for i, it in enumerate(items):
            r = by_id.get(i)
            if not isinstance(r, dict):
                results.append(_fallback(it))
                continue
            h = _heuristic_semantic_analysis(it["name"], it.get("code", ""), repo_context)
            summary = re.sub(r"^[*`#\-\s]+", "", str(r.get("summary") or "").strip())
            summary = re.split(r"(?<=[.!?])\s+", summary)[0][:200] if summary else ""
            if not summary:
                summary = _code_based_description(it["name"], it.get("code", ""), it.get("kind", "function"))
            results.append({
                "summary": summary,
                "intent": r.get("intent", h["intent"]),
                "complexity": r.get("complexity", h["complexity"]),
                "criticality": r.get("criticality", h["criticality"]),
                "patterns": r.get("patterns", h["patterns"]),
                "side_effects": r.get("side_effects", h["side_effects"]),
                "semantic_edges": r.get("semantic_edges", []) or [],
                "confidence": 0.8,
            })
        return results


# ---------------------------------------------------------------------------
# Heuristic-only provider (no LLM)
# ---------------------------------------------------------------------------

def _heuristic_one_liner(name: str, kind: str = "function") -> str:
    """Derive a readable 1-line description from a symbol name (name-only fallback)."""
    clean = re.sub(r"^(test_|_)", "", name)
    parts = [p for p in re.split(r"[_.\s]|(?<=[a-z])(?=[A-Z])", clean) if p]
    if not parts:
        return f"{kind.capitalize()} {name}."
    words = [parts[0].capitalize()] + [p.lower() for p in parts[1:]]
    verb = words[0]
    rest = " ".join(words[1:])
    verb_map = {
        "Get": "Returns", "Set": "Sets", "Build": "Builds", "Create": "Creates",
        "Init": "Initializes", "Load": "Loads", "Save": "Saves", "Parse": "Parses",
        "Run": "Runs", "Start": "Starts", "Stop": "Stops", "Handle": "Handles",
        "Process": "Processes", "Generate": "Generates", "Validate": "Validates",
        "Update": "Updates", "Delete": "Deletes", "Find": "Finds", "Check": "Checks",
        "Compute": "Computes", "Analyze": "Analyzes", "Fetch": "Fetches",
        "Expand": "Expands", "Ingest": "Ingests", "Serialize": "Serializes",
        "Send": "Sends", "Read": "Reads", "Write": "Writes", "Format": "Formats",
        "Convert": "Converts", "Extract": "Extracts", "Register": "Registers",
    }
    verb = verb_map.get(verb, verb + "s" if not verb.endswith("s") else verb)
    desc = f"{verb} {rest}." if rest else f"{verb} {kind}."
    return desc[:120]


def _code_based_description(name: str, code: str, kind: str = "function") -> str:
    """Derive a 1-line description by analysing actual code content.

    Priority:
    1. Docstring first sentence (most reliable — written by the author)
    2. AST analysis: return type + notable function calls + yield/raise patterns
    3. Name-only heuristic fallback
    """
    # ── 1. Extract docstring ─────────────────────────────────────────────────
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node)
                if doc:
                    first = re.split(r"(?<=[.!?])\s+|\n", doc.strip())[0].strip()
                    if first and len(first) > 10:
                        return (first if first.endswith((".", "!", "?")) else first + ".")[:200]
                break
    except Exception:
        pass

    # ── 2. AST analysis: return type + notable calls + patterns ─────────────
    _SKIP = {
        "print", "len", "str", "int", "float", "bool", "list", "dict", "set",
        "isinstance", "hasattr", "getattr", "setattr", "range", "enumerate",
        "zip", "map", "filter", "sorted", "reversed", "super", "type",
        "append", "extend", "update", "get", "items", "values", "keys",
        "lower", "upper", "strip", "split", "join", "format", "encode",
        "decode", "open", "close", "read", "write", "logger", "log",
    }
    try:
        tree = ast.parse(code)

        # Return type annotation
        ret_hint = ""
        is_async = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_async = isinstance(node, ast.AsyncFunctionDef)
                if node.returns:
                    try:
                        ret_hint = ast.unparse(node.returns)
                    except Exception:
                        pass
                break

        # Notable calls (skip builtins; de-duplicate; max 3)
        call_names: list[str] = []
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                try:
                    cn = ast.unparse(node.func).split(".")[-1]
                except Exception:
                    continue
                if cn not in _SKIP and cn not in seen and not cn.startswith("_"):
                    seen.add(cn)
                    call_names.append(cn)
            if len(call_names) >= 3:
                break

        has_yield = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(tree))
        has_raise = any(isinstance(n, ast.Raise) for n in ast.walk(tree))

        base = _heuristic_one_liner(name, kind).rstrip(".")

        extras: list[str] = []
        if is_async:
            extras.append("async")
        if has_yield:
            extras.append("yields results lazily")
        elif ret_hint and ret_hint not in ("None", "bool", "Any"):
            extras.append(f"returns {ret_hint}")
        if call_names:
            extras.append("using " + ", ".join(call_names))
        if has_raise:
            extras.append("raises on invalid input")

        if extras:
            return (base + " (" + "; ".join(extras) + ").")[:200]
        return base + "."

    except Exception:
        pass

    # ── 3. Name-only fallback ────────────────────────────────────────────────
    return _heuristic_one_liner(name, kind)


class HeuristicProvider(LLMProvider):
    """Pure heuristic stubs — no network calls, always available."""

    _is_heuristic: bool = True  # sentinel checked by pipeline._summarize_functions

    def _call(self, prompt: str) -> str:
        head = prompt.strip().splitlines()[0] if prompt.strip() else ""
        return f"(stub) {head[:120]}"

    def summarize_function(self, name: str, code: str, kind: str = "function") -> str:  # type: ignore[override]
        if code and code.strip():
            return _code_based_description(name, code, kind)
        return _heuristic_one_liner(name, kind)

    def interpret_query(
        self,
        query: str,
        candidates: List[str],
        context: Optional[dict] = None,
    ) -> List[str]:
        q_tokens = {t.lower() for t in re.split(r"\W+", query) if len(t) > 2}
        scored = []
        for name in candidates:
            name_score = sum(1.5 for t in q_tokens if t in name.lower())
            summary = (context or {}).get(name, "")
            summary_score = sum(1.0 for t in q_tokens if t in summary.lower())
            scored.append((name_score + summary_score, name))
        scored.sort(reverse=True)
        return [name for score, name in scored if score > 0][:10]

    def analyze_repository(self, repo_path: str) -> dict:
        return _heuristic_repo_analysis(repo_path)

    def summarize_repository_flow(self, evidence: dict) -> dict:
        """Degraded structured summary — no LLM, derived straight from evidence."""
        modules = evidence.get("modules", [])
        intents = {
            f.get("intent")
            for m in modules for f in m.get("key_functions", [])
            if f.get("intent") and f.get("intent") != "unknown"
        }
        return {
            "what": evidence.get("purpose")
                    or f"A {evidence.get('project_type','software')} project"
                       f" spanning {len(modules)} modules.",
            "usecase": evidence.get("purpose", ""),
            "techniques": sorted(intents),
            # Reuse each module's existing description verbatim as its flow line
            "module_flows": {m["module"]: m.get("description", "") for m in modules},
        }

    def analyze_function_semantics(self, name, code, repo_context, neighbors=None) -> dict:
        return _heuristic_semantic_analysis(name, code, repo_context)

    def analyze_functions_batch(self, items, repo_context) -> List[dict]:
        out = []
        for it in items:
            code = it.get("code", "")
            h = _heuristic_semantic_analysis(it["name"], code, repo_context)
            h["summary"] = (
                _code_based_description(it["name"], code, it.get("kind", "function"))
                if code.strip() else _heuristic_one_liner(it["name"], it.get("kind", "function"))
            )
            h["semantic_edges"] = []
            out.append(h)
        return out

    def explain_flow_with_graph(
        self,
        query: str,
        summaries: List[str],
        edge_info: Optional[List[str]] = None,
        prior_turns: Optional[List[dict]] = None,
    ) -> str:
        if not summaries:
            return "(stub) No relevant functions found in the graph."
        names = [s.split("(")[0].strip() for s in summaries[:5]]
        chain = " → ".join(names)
        return f"(stub) According to the graph, the execution path for '{query}' is: {chain}"


# ---------------------------------------------------------------------------
# Ollama provider  (local, no API key required)
# ---------------------------------------------------------------------------

# Preferred model names for code tasks, tried in order against installed models
_OLLAMA_CODE_MODELS = [
    "codellama", "codellama:13b", "codellama:7b",
    "deepseek-coder", "deepseek-coder:6.7b",
    "qwen2.5-coder", "qwen2.5-coder:7b",
    "llama3.2", "llama3.2:3b",
    "llama3.1", "llama3",
    "mistral", "phi3", "gemma2",
]


def _ollama_running(host: str, headers: dict | None = None) -> bool:
    """Return True if Ollama is reachable at *host*."""
    try:
        r = requests.get(f"{host}/api/tags", timeout=2, headers=headers)
        return r.status_code == 200
    except Exception:
        return False


def _ollama_best_model(host: str, preferred: str | None = None, headers: dict | None = None) -> str | None:
    """Return the best available code model, or None if Ollama has nothing installed."""
    try:
        data = requests.get(f"{host}/api/tags", timeout=3, headers=headers).json()
        installed = {m["name"].split(":")[0].lower() for m in data.get("models", [])}
        installed_full = [m["name"] for m in data.get("models", [])]
    except Exception:
        return None

    # Honour explicit preference first
    if preferred:
        for m in installed_full:
            if m.lower().startswith(preferred.lower()):
                return m
        return preferred  # trust the user even if we can't confirm

    # Walk priority list
    for candidate in _OLLAMA_CODE_MODELS:
        base = candidate.split(":")[0].lower()
        if base in installed:
            # Return the full tag if we have an exact match, else just the base
            for m in installed_full:
                if m.lower().startswith(base):
                    return m
            return candidate

    # Fallback: whatever is first
    if installed_full:
        return installed_full[0]
    return None


class OllamaProvider(LLMProvider):
    """Ollama — local (no key needed) or Ollama Cloud (ollama.com, needs an API key).

    Calls POST /api/generate with stream=false. Model is auto-selected from
    installed models when no explicit model is given (prefers code-focused
    models) — that auto-pick is skipped whenever an api_key is present,
    since Cloud's model catalog isn't "installed" in the local sense and an
    explicit OLLAMA_MODEL is expected instead.

    Environment variables:
        OLLAMA_HOST     (default: http://localhost:11434; set to
                         https://ollama.com for Cloud)
        OLLAMA_MODEL    (default: auto-picked locally; required for Cloud)
        OLLAMA_API_KEY  (unset for local; required for Cloud — sent as
                         Authorization: Bearer <key>, matching Ollama Cloud's
                         documented auth format)
    """

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.api_key = api_key or os.environ.get("OLLAMA_API_KEY", "")
        self._headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        # Resolve model: explicit arg > env var > auto-pick
        # (.strip() guards against a stray trailing space in OLLAMA_MODEL, which
        # otherwise leaks into the model name and the provider display label.)
        explicit = (model or os.environ.get("OLLAMA_MODEL", "")).strip()
        if explicit:
            self.model = explicit
        else:
            self.model = _ollama_best_model(self.host, headers=self._headers) or "llama3"
        print(f"[Ollama] host={self.host}  model={self.model}  cloud={bool(self.api_key)}")

    def _call(self, prompt: str) -> str:
        """Call Ollama.  Raises on any failure so `ask()` can skip caching."""
        resp = requests.post(
            f"{self.host}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    # Ceiling, not a target — short answers still stop early at EOS.
                    # Sized to fit multi-section structured JSON (e.g. flow summary)
                    # without truncating mid-object, which would break JSON parsing.
                    "num_predict": 2048,  # max tokens
                    "temperature": 0.2,   # low temp for deterministic code answers
                },
            },
            headers=self._headers,
            timeout=300,  # 5 min — models can be slow on first/cold call
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("response", "").strip()
        if not text:
            raise ValueError("Ollama returned an empty response")
        return text


# ---------------------------------------------------------------------------
# IBM Bob / watsonx provider
# ---------------------------------------------------------------------------

class BobProvider(LLMProvider):
    """IBM Bob / watsonx.ai HTTP API."""

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("BOB_API_KEY", "")
        self.api_url = api_url or os.environ.get("BOB_API_URL", "https://bob.ibm.com/api/v1/generate")

    def _call(self, prompt: str) -> str:
        resp = requests.post(
            self.api_url,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"prompt": prompt, "max_tokens": 400},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("text") or data.get("output") or ""
        if not text:
            raise ValueError("Bob returned an empty response")
        return text


# ---------------------------------------------------------------------------
# Anthropic Claude provider
# ---------------------------------------------------------------------------

class AnthropicProvider(LLMProvider):
    """Anthropic Claude via the REST API (no SDK dependency required)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")

    def _call(self, prompt: str) -> str:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


# ---------------------------------------------------------------------------
# OpenAI-compatible provider  (OpenAI, Azure OpenAI, Codex, Copilot, OpenClaw)
# ---------------------------------------------------------------------------

class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions API (also works with any OpenAI-compatible endpoint)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    def _call(self, prompt: str) -> str:
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 512,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class CopilotProvider(OpenAIProvider):
    """GitHub Copilot — OpenAI-compatible endpoint authenticated via GITHUB_TOKEN."""

    def __init__(self) -> None:
        super().__init__(
            api_key=os.environ.get("GITHUB_TOKEN", ""),
            base_url=os.environ.get("COPILOT_API_URL", "https://api.githubcopilot.com"),
            model=os.environ.get("COPILOT_MODEL", "gpt-4o"),
        )

    def _call(self, prompt: str) -> str:
        # Copilot requires an extra header
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Copilot-Integration-Id": "flowify",
            },
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 512,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class OpenClawProvider(OpenAIProvider):
    """OpenClaw — OpenAI-compatible REST API with custom endpoint."""

    def __init__(self) -> None:
        super().__init__(
            api_key=os.environ.get("OPENCLAW_API_KEY", ""),
            base_url=os.environ.get("OPENCLAW_API_URL", ""),
            model=os.environ.get("OPENCLAW_MODEL", "openclaw"),
        )


# ---------------------------------------------------------------------------
# Per-request provider override (BYO API key)
# ---------------------------------------------------------------------------
# The server never holds a paying LLM key (see README's Deployment section):
# a hosted instance defaults to the heuristic provider, and a visitor who
# wants real LLM answers pastes their own key in the browser, which rides
# along as a header on each request. This ContextVar is how that per-request
# choice reaches _get() below without threading a `provider` parameter
# through every call site in pipeline/retrieval/module_abstractor/
# llm_ingestion — those all call the module-level ask()/summarize_function()/
# etc. helpers with no provider argument today.
import contextvars

_provider_override: contextvars.ContextVar[Optional["LLMProvider"]] = contextvars.ContextVar(
    "flowify_llm_provider_override", default=None
)


def provider_from_spec(
    provider: str,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
) -> LLMProvider:
    """Build a provider from an explicit spec (as opposed to get_provider()'s
    environment-variable auto-detect). Used for BYO-key requests.

    Only the providers where "bring your own key" is a coherent idea are
    supported here — Bob is an IBM-internal endpoint and Copilot/OpenClaw are
    fixed to a single env-configured account, so they're intentionally left
    to the env-var path (get_provider()).
    """
    name = provider.lower().strip()
    if name in ("ollama", "local"):
        # api_key is optional here: a local, unauthenticated Ollama host is a
        # valid BYO target too (e.g. someone tunneling their own machine).
        return OllamaProvider(host=base_url or None, model=model or None, api_key=api_key or None)
    if name in ("claude", "anthropic"):
        if not api_key:
            raise ValueError("api_key is required for provider 'anthropic'")
        return AnthropicProvider(api_key=api_key, model=model or None)
    if name in ("openai", "gpt"):
        if not api_key:
            raise ValueError("api_key is required for provider 'openai'")
        return OpenAIProvider(api_key=api_key, base_url=base_url or None, model=model or None)
    raise ValueError(f"unsupported BYO provider: {provider!r} (use ollama, anthropic, or openai)")


class provider_override:
    """Context manager: run a block with a specific provider active,
    regardless of what LLM_PROVIDER/auto-detect would otherwise pick.

    Usage (typically inside a request-handling middleware):
        with llm_provider.provider_override(provider_from_spec(...)):
            ...call ask()/summarize_function()/etc as normal...
    """

    def __init__(self, provider: Optional[LLMProvider]):
        self._provider = provider
        self._token = None

    def __enter__(self):
        self._token = _provider_override.set(self._provider)
        return self._provider

    def __exit__(self, *exc):
        _provider_override.reset(self._token)


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def get_provider() -> LLMProvider:
    """Instantiate the configured provider.

    Priority:
    0. A BYO-key provider_override() active for the current request (checked
       here, not just in _get(), because pipeline.py and main.py both call
       get_provider() directly in a few places rather than going through the
       ask()/summarize_function() module-level helpers).
    1. LLM_PROVIDER env var (explicit choice)
    2. Auto-detect: Ollama on localhost → cloud API keys → heuristic stubs
    """
    override = _provider_override.get()
    if override is not None:
        return override

    name = os.environ.get("LLM_PROVIDER", "").lower().strip()

    # ── Explicit selection ──────────────────────────────────────────────────
    if name in ("ollama", "local"):
        return OllamaProvider()
    if name in ("bob", "watsonx", "ibm"):
        return BobProvider()
    if name in ("claude", "anthropic"):
        return AnthropicProvider()
    if name in ("openai", "codex", "gpt"):
        return OpenAIProvider()
    if name == "copilot":
        return CopilotProvider()
    if name in ("openclaw",):
        return OpenClawProvider()
    if name == "heuristic":
        return HeuristicProvider()

    # ── Auto-detect ─────────────────────────────────────────────────────────
    # 1. Ollama on localhost — preferred because it's free and private
    _ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if _ollama_running(_ollama_host):
        return OllamaProvider(host=_ollama_host)

    # 2. Cloud providers (in priority order)
    if os.environ.get("BOB_API_KEY"):
        return BobProvider()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider()
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIProvider()
    if os.environ.get("GITHUB_TOKEN"):
        return CopilotProvider()
    if os.environ.get("OPENCLAW_API_KEY"):
        return OpenClawProvider()

    return HeuristicProvider()


# ---------------------------------------------------------------------------
# Module-level singleton + public API
# (drop-in replacement for the old bob_client module)
# ---------------------------------------------------------------------------

_provider: LLMProvider | None = None


def _get() -> LLMProvider:
    override = _provider_override.get()
    if override is not None:
        return override
    global _provider
    if _provider is None:
        _provider = get_provider()
        print(f"[LLM] Using provider: {type(_provider).__name__}")
    return _provider


def ask(prompt: str) -> str:
    return _get().ask(prompt)

def ask_json(prompt: str, fallback: dict) -> dict:
    return _get().ask_json(prompt, fallback)

def summarize_function(name: str, code: str, kind: str = "function") -> str:
    return _get().summarize_function(name, code, kind=kind)

def summarize_module(name_hint: str, summaries: List[str]) -> dict:
    return _get().summarize_module(name_hint, summaries)

def explain_flow_with_graph(
    query: str,
    summaries: List[str],
    edge_info: Optional[List[str]] = None,
    prior_turns: Optional[List[dict]] = None,
) -> str:
    return _get().explain_flow_with_graph(query, summaries, edge_info, prior_turns)

def interpret_query(query: str, candidates: List[str], context: Optional[dict] = None) -> List[str]:
    return _get().interpret_query(query, candidates, context)

def summarize_repository_flow(evidence: dict) -> dict:
    return _get().summarize_repository_flow(evidence)

def analyze_repository(repo_path: str) -> dict:
    return _get().analyze_repository(repo_path)

def analyze_function_semantics(
    name: str, code: str, repo_context: dict,
    neighbors: Optional[List[str]] = None,
) -> dict:
    return _get().analyze_function_semantics(name, code, repo_context, neighbors)

def analyze_functions_batch(items: List[dict], repo_context: dict) -> List[dict]:
    return _get().analyze_functions_batch(items, repo_context)


_heuristic_singleton: LLMProvider | None = None


def heuristic_provider() -> LLMProvider:
    """Shared no-LLM provider for the free path (trivial functions)."""
    global _heuristic_singleton
    if _heuristic_singleton is None:
        _heuristic_singleton = HeuristicProvider()
    return _heuristic_singleton
