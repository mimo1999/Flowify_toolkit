"""Agent-agnostic LLM provider layer.

Select a provider via the LLM_PROVIDER environment variable:

    LLM_PROVIDER=bob        IBM Bob / watsonx (BOB_API_KEY + BOB_API_URL)
    LLM_PROVIDER=claude     Anthropic Claude  (ANTHROPIC_API_KEY, opt. ANTHROPIC_MODEL)
    LLM_PROVIDER=openai     OpenAI / Codex    (OPENAI_API_KEY, opt. OPENAI_BASE_URL, OPENAI_MODEL)
    LLM_PROVIDER=copilot    GitHub Copilot    (GITHUB_TOKEN, opt. COPILOT_MODEL)
    LLM_PROVIDER=openclaw   OpenClaw          (OPENCLAW_API_KEY + OPENCLAW_API_URL)
    LLM_PROVIDER=heuristic  No LLM (always available, deterministic stubs)

If LLM_PROVIDER is not set, the first provider whose API key is present in the
environment is used. If no key is found, falls back to heuristic stubs.

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


def _cache_path(prompt: str) -> Path:
    h = hashlib.sha256(prompt.encode()).hexdigest()[:32]
    return _CACHE_DIR / f"{h}.json"


def _cache_get(prompt: str) -> Optional[str]:
    p = _cache_path(prompt)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))["response"]
        except Exception:
            return None
    return None


def _cache_put(prompt: str, response: str) -> None:
    _cache_path(prompt).write_text(
        json.dumps({"prompt": prompt[:500], "response": response}),
        encoding="utf-8",
    )


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
        "data_flow_pattern": None,
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
        "data_flow": None, "confidence": 0.4,
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

    def ask(self, prompt: str) -> str:
        """Cached wrapper around `_call`."""
        cached = _cache_get(prompt)
        if cached is not None:
            return cached
        out = self._call(prompt)
        _cache_put(prompt, out)
        return out

    def ask_json(self, prompt: str, fallback: dict) -> dict:
        raw = self.ask(prompt)
        try:
            start, end = raw.index("{"), raw.rindex("}") + 1
            return json.loads(raw[start:end])
        except Exception:
            return fallback

    # -- high-level methods used by the pipeline ------------------------------

    def summarize_function(self, name: str, code: str) -> str:
        return self.ask(textwrap.dedent(f"""
            You are summarizing a code function for a software map.
            Function: {name}

            ```
            {code[:1500]}
            ```

            Provide a concise one-sentence functional description (no implementation detail).
        """).strip()).strip()

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

    def explain_flow(self, query: str, summaries: List[str]) -> str:
        joined = "\n".join(f"{i+1}. {s}" for i, s in enumerate(summaries))
        return self.ask(textwrap.dedent(f"""
            Question: {query}

            The following ordered functions form the relevant execution path:
            {joined}

            Explain the end-to-end flow at an abstract level (no pseudo-code).
            Highlight inputs, transformations, and outputs.
        """).strip()).strip()

    def interpret_query(self, query: str, candidates: List[str]) -> List[str]:
        if not candidates:
            return []
        raw = self.ask(textwrap.dedent(f"""
            User query: {query}
            Candidate symbols (one per line):
            {chr(10).join(candidates[:200])}

            Return up to 10 most relevant symbol names, one per line, no commentary.
        """).strip())
        return [l.strip() for l in raw.splitlines() if l.strip()][:10]

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
                    "data_flow_pattern": result.get("data_flow_pattern"),
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


# ---------------------------------------------------------------------------
# Heuristic-only provider (no LLM)
# ---------------------------------------------------------------------------

class HeuristicProvider(LLMProvider):
    """Pure heuristic stubs — no network calls, always available."""

    def _call(self, prompt: str) -> str:
        head = prompt.strip().splitlines()[0] if prompt.strip() else ""
        return f"(stub) {head[:120]}"

    def interpret_query(self, query: str, candidates: List[str]) -> List[str]:
        q_tokens = {t.lower() for t in query.replace("?", " ").split() if len(t) > 2}
        scored = sorted(
            ((sum(1 for t in q_tokens if t in name.lower()), name) for name in candidates),
            reverse=True,
        )
        return [name for score, name in scored if score][:10]

    def analyze_repository(self, repo_path: str) -> dict:
        return _heuristic_repo_analysis(repo_path)

    def analyze_function_semantics(self, name, code, repo_context, neighbors=None) -> dict:
        return _heuristic_semantic_analysis(name, code, repo_context)


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
        try:
            resp = requests.post(
                self.api_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"prompt": prompt, "max_tokens": 400},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("text") or data.get("output") or HeuristicProvider()._call(prompt)
        except Exception as e:
            return HeuristicProvider()._call(prompt) + f"\n[bob error: {e}]"


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
        try:
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
        except Exception as e:
            return HeuristicProvider()._call(prompt) + f"\n[claude error: {e}]"


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
        try:
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
        except Exception as e:
            return HeuristicProvider()._call(prompt) + f"\n[openai error: {e}]"


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
        try:
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
        except Exception as e:
            return HeuristicProvider()._call(prompt) + f"\n[copilot error: {e}]"


class OpenClawProvider(OpenAIProvider):
    """OpenClaw — OpenAI-compatible REST API with custom endpoint."""

    def __init__(self) -> None:
        super().__init__(
            api_key=os.environ.get("OPENCLAW_API_KEY", ""),
            base_url=os.environ.get("OPENCLAW_API_URL", ""),
            model=os.environ.get("OPENCLAW_MODEL", "openclaw"),
        )


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def get_provider() -> LLMProvider:
    """Instantiate the configured provider.

    Priority:
    1. LLM_PROVIDER env var (explicit choice)
    2. Auto-detect from available API keys
    3. Heuristic fallback
    """
    name = os.environ.get("LLM_PROVIDER", "").lower().strip()

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

    # Auto-detect
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
    global _provider
    if _provider is None:
        _provider = get_provider()
        print(f"[LLM] Using provider: {type(_provider).__name__}")
    return _provider


def ask(prompt: str) -> str:
    return _get().ask(prompt)

def ask_json(prompt: str, fallback: dict) -> dict:
    return _get().ask_json(prompt, fallback)

def summarize_function(name: str, code: str) -> str:
    return _get().summarize_function(name, code)

def summarize_module(name_hint: str, summaries: List[str]) -> dict:
    return _get().summarize_module(name_hint, summaries)

def explain_flow(query: str, summaries: List[str]) -> str:
    return _get().explain_flow(query, summaries)

def interpret_query(query: str, candidates: List[str]) -> List[str]:
    return _get().interpret_query(query, candidates)

def analyze_repository(repo_path: str) -> dict:
    return _get().analyze_repository(repo_path)

def analyze_function_semantics(
    name: str, code: str, repo_context: dict,
    neighbors: Optional[List[str]] = None,
) -> dict:
    return _get().analyze_function_semantics(name, code, repo_context, neighbors)
