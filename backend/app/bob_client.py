"""IBM Bob client wrapper.

If BOB_API_KEY is set, calls the Bob HTTP API (configurable via BOB_API_URL).
Otherwise falls back to deterministic stub summaries so the rest of the
pipeline keeps working without credentials.

Responses are cached on disk per-prompt-hash to avoid repeated calls.
"""
from __future__ import annotations
import os
import json
import hashlib
import re
import textwrap
from pathlib import Path
from typing import List, Optional

import requests


_CACHE_DIR = Path(os.environ.get("FLOWIFY_STORE", "_store")) / "bob_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(prompt: str) -> Path:
    h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:32]
    return _CACHE_DIR / f"{h}.json"


def _cached(prompt: str) -> Optional[str]:
    p = _cache_key(prompt)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))["response"]
        except Exception:
            return None
    return None


def _store(prompt: str, response: str):
    _cache_key(prompt).write_text(
        json.dumps({"prompt": prompt[:500], "response": response}),
        encoding="utf-8",
    )


def _bob_call(prompt: str) -> str:
    api_key = os.environ.get("BOB_API_KEY")
    if not api_key:
        return _stub(prompt)
    url = os.environ.get("BOB_API_URL", "https://bob.ibm.com/api/v1/generate")
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"prompt": prompt, "max_tokens": 400},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("text") or data.get("output") or _stub(prompt)
    except Exception as e:
        return _stub(prompt) + f"\n[bob error fallback: {e}]"


def _stub(prompt: str) -> str:
    """Heuristic stand-in when Bob is unavailable."""
    head = prompt.strip().splitlines()[0] if prompt.strip() else ""
    return f"(stub) {head[:120]}"


def ask(prompt: str) -> str:
    cached = _cached(prompt)
    if cached is not None:
        return cached
    out = _bob_call(prompt)
    _store(prompt, out)
    return out


def ask_json(prompt: str, fallback: dict) -> dict:
    """Ask Bob for JSON and fall back safely when parsing fails."""
    raw = ask(prompt)
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except Exception:
        return fallback


def summarize_function(name: str, code: str) -> str:
    prompt = textwrap.dedent(f"""
        You are summarizing a code function for a software map.
        Function: {name}

        ```
        {code[:1500]}
        ```

        Provide a concise one-sentence functional description (no implementation detail).
    """).strip()
    return ask(prompt).strip()


def summarize_module(name_hint: str, function_summaries: List[str]) -> dict:
    joined = "\n".join(f"- {s}" for s in function_summaries[:30])
    prompt = textwrap.dedent(f"""
        Given these function summaries, propose a coherent module abstraction.

        Functions:
        {joined}

        Respond as JSON with keys: name (short label, 2-4 words),
        description (one sentence). Hint: {name_hint}
    """).strip()
    raw = ask(prompt)
    # Try JSON parse, else fall back.
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        obj = json.loads(raw[start:end])
        return {"name": obj.get("name", name_hint), "description": obj.get("description", "")}
    except Exception:
        return {"name": name_hint, "description": raw.splitlines()[0][:200] if raw else ""}


def explain_flow(query: str, ordered_summaries: List[str]) -> str:
    joined = "\n".join(f"{i+1}. {s}" for i, s in enumerate(ordered_summaries))
    prompt = textwrap.dedent(f"""
        Question: {query}

        The following ordered functions form the relevant execution path:
        {joined}

        Explain the end-to-end flow at an abstract level (no pseudo-code).
        Highlight inputs, transformations, and outputs.
    """).strip()
    return ask(prompt).strip()


def interpret_query(query: str, candidate_names: List[str]) -> List[str]:
    """Return a ranked subset of candidate function/module names relevant to the query.

    Without a real LLM, falls back to keyword matching."""
    api_key = os.environ.get("BOB_API_KEY")
    if not api_key:
        q_tokens = {t.lower() for t in query.replace("?", " ").split() if len(t) > 2}
        scored = []
        for name in candidate_names:
            n = name.lower()
            score = sum(1 for t in q_tokens if t in n)
            if score:
                scored.append((score, name))
        scored.sort(reverse=True)
        return [n for _, n in scored[:10]]
    prompt = textwrap.dedent(f"""
        User query: {query}
        Candidate symbols (one per line):
        {chr(10).join(candidate_names[:200])}

        Return up to 10 most relevant symbol names, one per line, no commentary.
    """).strip()
    raw = ask(prompt)
    return [line.strip() for line in raw.splitlines() if line.strip()][:10]


# Phase 1: Repository Intelligence Layer

_NON_ENTRY_FILE_STEMS = {
    "__init__", "constants", "config", "settings", "data_loader", "loader",
    "trainer", "evaluator", "factory", "utils", "util", "helpers", "helper",
    "models", "model", "dataset", "preprocessor", "preprocessing", "logger",
    "saver", "tracker", "batcher", "cycler",
}


def _rel_path(path: Path, repo_root: Path) -> str:
    return str(path.relative_to(repo_root)).replace("\\", "/")


def _module_to_path(module_name: str, repo_root: Path) -> str | None:
    candidate = repo_root / (module_name.replace(".", "/") + ".py")
    if candidate.exists():
        return _rel_path(candidate, repo_root)
    package_main = repo_root / module_name.replace(".", "/") / "__main__.py"
    if package_main.exists():
        return _rel_path(package_main, repo_root)
    return None


def _path_to_module(path: str) -> str:
    return path[:-3].replace("/", ".") if path.endswith(".py") else path.replace("/", ".")


def _python_module_invocations(text: str) -> list[str]:
    modules = []
    for match in re.finditer(r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+([A-Za-z_][\w.]*)", text):
        module_name = match.group(1).rstrip(".")
        if module_name and module_name not in modules:
            modules.append(module_name)
    return modules


def _score_entry_file(path: Path, repo_root: Path, command_modules: set[str]) -> int:
    rel = _rel_path(path, repo_root)
    module_name = _path_to_module(rel)
    stem = path.stem.lower()
    score = 0

    if module_name in command_modules:
        score += 100
    if path.name in {"main.py", "__main__.py"}:
        score += 35
    if stem in {"main", "ml_main", "train", "run", "start", "cli"}:
        score += 25
    if any(part in {"training", "train", "pipeline"} for part in rel.lower().replace("\\", "/").split("/")):
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
    if "EnvManager(args)" in content or ".train_full()" in content:
        score += 15

    return score


def _discover_entry_points(repo_root: Path) -> list[str]:
    """Find runnable Python module/file entry points from commands and code."""
    command_modules: set[str] = set()
    command_sources = ["README.md", "README.txt", "README.rst", "README"]
    command_sources.extend(p.name for p in repo_root.glob("*.sh"))
    command_sources.extend(p.name for p in repo_root.glob("*.bat"))
    command_sources.extend(p.name for p in repo_root.glob("*.ps1"))

    for source_name in dict.fromkeys(command_sources):
        source_path = repo_root / source_name
        if not source_path.exists() or not source_path.is_file():
            continue
        try:
            content = source_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        command_modules.update(_python_module_invocations(content))

    scored: list[tuple[int, str, str]] = []
    for module_name in sorted(command_modules):
        rel = _module_to_path(module_name, repo_root)
        if rel:
            scored.append((200, rel, module_name))

    if scored:
        scored.sort(key=lambda item: item[1])
        return [module_name for _, _, module_name in scored[:10]]

    for py_file in repo_root.rglob("*.py"):
        if any(part.startswith(".") or part in {"venv", ".venv", "__pycache__", "node_modules"} for part in py_file.parts):
            continue
        rel = _rel_path(py_file, repo_root)
        module_name = _path_to_module(rel)
        if any(existing_rel == rel for _, existing_rel, _ in scored):
            continue
        score = _score_entry_file(py_file, repo_root, command_modules)
        if score >= 40:
            scored.append((score, rel, module_name))

    scored.sort(key=lambda item: (-item[0], item[1]))
    # Store dotted modules when available; /entry_points can resolve either form.
    return [module_name for _, _, module_name in scored[:10]]

def _heuristic_repo_analysis(repo_path: str) -> dict:
    """Fallback: Analyze repository structure without LLM.
    
    Uses file patterns and directory structure to infer project characteristics.
    """
    from pathlib import Path
    
    repo_root = Path(repo_path).resolve()
    
    # Detect project type from key files
    project_type = "unknown"
    tech_stack = []
    key_entry_points = []
    architecture = "unknown"
    domain = "general"
    purpose = ""
    
    # Check for common configuration files
    if (repo_root / "requirements.txt").exists():
        tech_stack.append("Python")
        req_content = (repo_root / "requirements.txt").read_text(encoding="utf-8", errors="ignore").lower()
        
        if "fastapi" in req_content or "flask" in req_content or "django" in req_content:
            project_type = "web_api"
            tech_stack.append("FastAPI" if "fastapi" in req_content else "Flask" if "flask" in req_content else "Django")
        elif "click" in req_content or "argparse" in req_content:
            project_type = "cli_tool"
        elif "scikit-learn" in req_content or "tensorflow" in req_content or "pytorch" in req_content:
            project_type = "ml_model"
            domain = "machine_learning"
        
        if "networkx" in req_content:
            tech_stack.append("NetworkX")
        if "pydantic" in req_content:
            tech_stack.append("Pydantic")
    
    if (repo_root / "package.json").exists():
        tech_stack.append("JavaScript/Node.js")
        try:
            import json
            pkg = json.loads((repo_root / "package.json").read_text(encoding="utf-8"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            
            if "react" in deps:
                tech_stack.append("React")
            if "vue" in deps:
                tech_stack.append("Vue")
            if "express" in deps:
                project_type = "web_api"
                tech_stack.append("Express")
        except Exception:
            pass
    
    if (repo_root / "pyproject.toml").exists():
        tech_stack.append("Python")
    
    # Detect architecture from directory structure
    dirs = [d.name for d in repo_root.iterdir() if d.is_dir() and not d.name.startswith(".")]
    
    if "app" in dirs or "src" in dirs:
        architecture = "modular_pipeline"
    if "api" in dirs and "services" in dirs:
        architecture = "microservices"
    if "models" in dirs and "views" in dirs and "controllers" in dirs:
        architecture = "mvc"
    if "backend" in dirs and "frontend" in dirs:
        architecture = "layered"
    
    key_entry_points = _discover_entry_points(repo_root)
    
    # Try to read README for purpose
    for readme_name in ["README.md", "README.txt", "README.rst", "README"]:
        readme_path = repo_root / readme_name
        if readme_path.exists():
            try:
                readme_content = readme_path.read_text(encoding="utf-8", errors="ignore")
                # Extract first paragraph or first 200 chars
                lines = [l.strip() for l in readme_content.split("\n") if l.strip() and not l.startswith("#")]
                if lines:
                    purpose = lines[0][:200]
                break
            except Exception:
                pass
    
    # Infer domain from directory names and file content
    domain_keywords = {
        "code_analysis": ["ast", "parser", "analyzer", "graph", "flow"],
        "web_development": ["api", "server", "client", "http", "rest"],
        "data_processing": ["etl", "pipeline", "transform", "ingest"],
        "machine_learning": ["model", "train", "predict", "dataset"],
        "devops": ["deploy", "docker", "kubernetes", "ci", "cd"],
    }
    
    repo_text = " ".join(dirs).lower() + " " + purpose.lower()
    domain_scores = {}
    for domain_name, keywords in domain_keywords.items():
        score = sum(1 for kw in keywords if kw in repo_text)
        if score > 0:
            domain_scores[domain_name] = score
    
    if domain_scores:
        domain = max(domain_scores.items(), key=lambda x: x[1])[0]
    
    return {
        "project_type": project_type,
        "domain": domain,
        "architecture": architecture,
        "tech_stack": tech_stack,
        "purpose": purpose or f"A {project_type} project",
        "key_entry_points": key_entry_points,
        "critical_modules": dirs[:5],  # Top-level directories as critical modules
        "data_flow_pattern": None,
        "confidence": 0.5,  # Heuristic analysis has medium confidence
        "fallback_used": True,
    }


def analyze_repository(repo_path: str) -> dict:
    """Analyze repository structure and purpose before ingestion.
    
    Uses Bob API if available, falls back to heuristic analysis.
    Returns RepositoryContext-compatible dict.
    """
    api_key = os.environ.get("BOB_API_KEY")
    
    # Always try heuristic first to get baseline
    heuristic_result = _heuristic_repo_analysis(repo_path)
    
    if not api_key:
        return heuristic_result
    
    # Enhance with Bob analysis
    from pathlib import Path
    repo_root = Path(repo_path).resolve()
    
    # Gather context files for Bob
    context_files = {}
    for filename in ["README.md", "README.txt", "requirements.txt", "package.json", "pyproject.toml", "environment.yml"]:
        file_path = repo_root / filename
        if file_path.exists():
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                context_files[filename] = content[:2000]  # Limit to 2000 chars per file
            except Exception:
                pass
    for script_path in list(repo_root.glob("*.sh")) + list(repo_root.glob("*.bat")) + list(repo_root.glob("*.ps1")):
        try:
            context_files[script_path.name] = script_path.read_text(encoding="utf-8", errors="ignore")[:2000]
        except Exception:
            pass
    
    if not context_files:
        # No context files found, use heuristic only
        return heuristic_result
    
    # Build prompt for Bob with entry point detection
    files_text = "\n\n".join(f"=== {name} ===\n{content}" for name, content in context_files.items())
    
    # Get list of Python files for Bob to analyze
    python_files = sorted([
        str(f.relative_to(repo_root)).replace("\\", "/")
        for f in repo_root.rglob("*.py")
        if not any(part in {"venv", ".venv", "__pycache__", "node_modules"} for part in f.parts)
    ])
    
    # Include heuristic candidates as hints
    heuristic_entry_points = heuristic_result.get("key_entry_points", [])
    
    prompt = textwrap.dedent(f"""
        Analyze this repository and provide a structured assessment.
        
        Repository files:
        {files_text}
        
        Directory structure: {", ".join(heuristic_result.get("critical_modules", []))}
        
        Python files: {", ".join(python_files[:80])}
        
        Heuristic entry point candidates: {", ".join(heuristic_entry_points)}
        
        Respond as JSON with these keys:
        - project_type: one of [web_api, cli_tool, library, desktop_app, data_pipeline, ml_model, microservices, unknown]
        - domain: business/technical domain (e.g., "code_analysis", "e-commerce", "data_processing")
        - architecture: one of [monolith, modular_pipeline, microservices, mvc, layered, event_driven, plugin, unknown]
        - purpose: one-sentence description of what the project does
        - data_flow_pattern: high-level description of how data flows through the system
        - key_entry_points: array of runnable Python module names or file paths that serve as the true user-facing start points. Prefer dotted modules from commands such as `python -m package.module` (for example `package.module`) over helper files.
          * First inspect shell scripts and README usage commands.
          * Prefer modules invoked by `python -m ...`.
          * Prefer parser/CLI modules that create ArgumentParser and execute a pipeline.
          * Exclude utility files, constants/config, data loaders, trainers, evaluators, factories, model definitions, and libraries unless directly invoked by users.
        
        Be concise and specific. For key_entry_points, list 2-5 most important entry points.
    """).strip()
    
    try:
        raw = ask(prompt)
        # Try to parse JSON response
        import json
        import re
        
        # Find JSON in response
        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw, re.DOTALL)
        if json_match:
            bob_result = json.loads(json_match.group())
            
            # Use Bob's entry points as enrichment, but keep command-derived
            # runnable modules first because they are concrete invocation facts.
            bob_entry_points = bob_result.get("key_entry_points", [])
            if isinstance(bob_entry_points, list) and bob_entry_points:
                entry_points = []
                for ep in heuristic_result["key_entry_points"] + bob_entry_points:
                    if not isinstance(ep, str):
                        continue
                    normalized = ep.strip().replace("\\", "/")
                    if normalized and normalized not in entry_points:
                        entry_points.append(normalized)
                entry_points = entry_points[:10]
                entry_point_source = "bob"
            else:
                entry_points = heuristic_result["key_entry_points"]
                entry_point_source = "heuristic"
            
            # Merge Bob results with heuristic baseline
            result = {
                "project_type": bob_result.get("project_type", heuristic_result["project_type"]),
                "domain": bob_result.get("domain", heuristic_result["domain"]),
                "architecture": bob_result.get("architecture", heuristic_result["architecture"]),
                "tech_stack": heuristic_result["tech_stack"],  # Keep heuristic tech stack
                "purpose": bob_result.get("purpose", heuristic_result["purpose"]),
                "key_entry_points": entry_points,
                "critical_modules": heuristic_result["critical_modules"],
                "data_flow_pattern": bob_result.get("data_flow_pattern"),
                "confidence": 0.85,  # High confidence with Bob
                "fallback_used": False,
            }
            
            # Log which source was used for entry points
            print(f"  - Entry points source: {entry_point_source} ({len(entry_points)} found)")
            
            return result
    except Exception as e:
        # Bob call failed, use heuristic
        heuristic_result["confidence"] = 0.5
        heuristic_result["fallback_used"] = True
        return heuristic_result
    
    # If we get here, Bob response wasn't parseable
    return heuristic_result


# Phase 2: Semantic Function Analysis

def _heuristic_semantic_analysis(name: str, code: str, repo_context: dict) -> dict:
    """Fallback: Basic semantic analysis without LLM.
    
    Uses function name patterns and code structure to infer semantics.
    """
    name_lower = name.lower()
    code_lower = code.lower()
    
    # Infer intent from function name patterns
    intent = "unknown"
    if any(kw in name_lower for kw in ["save", "store", "write", "persist", "insert", "update"]):
        intent = "persistence"
    elif any(kw in name_lower for kw in ["load", "get", "fetch", "read", "retrieve", "find", "query"]):
        intent = "retrieval"
    elif any(kw in name_lower for kw in ["validate", "check", "verify", "ensure"]):
        intent = "validation"
    elif any(kw in name_lower for kw in ["transform", "convert", "parse", "process", "build"]):
        intent = "transformation"
    elif any(kw in name_lower for kw in ["handle", "catch", "error", "exception"]):
        intent = "error_handling"
    elif any(kw in name_lower for kw in ["config", "setup", "init", "configure"]):
        intent = "configuration"
    elif any(kw in name_lower for kw in ["orchestrate", "coordinate", "manage", "run", "execute"]):
        intent = "orchestration"
    elif any(kw in name_lower for kw in ["compute", "calculate", "sum", "count"]):
        intent = "computation"
    elif any(kw in name_lower for kw in ["render", "display", "show", "format"]):
        intent = "presentation"
    
    # Infer complexity from code length and structure
    lines = len(code.split('\n'))
    complexity = "low" if lines < 20 else "medium" if lines < 50 else "high" if lines < 100 else "very_high"
    
    # Infer criticality from context and name
    criticality = "medium"
    if repo_context.get("key_entry_points") and any(ep in code for ep in repo_context.get("key_entry_points", [])):
        criticality = "high"
    elif any(kw in name_lower for kw in ["main", "core", "critical", "essential"]):
        criticality = "high"
    elif any(kw in name_lower for kw in ["helper", "util", "aux", "temp"]):
        criticality = "low"
    
    # Detect side effects from code patterns
    side_effects = []
    if any(kw in code_lower for kw in ["open(", "file", "write", "read"]):
        side_effects.append("file_io")
    if any(kw in code_lower for kw in ["requests.", "http", "socket", "urllib"]):
        side_effects.append("network")
    if any(kw in code_lower for kw in ["db.", "database", "sql", "query", "session"]):
        side_effects.append("database")
    if any(kw in code_lower for kw in ["self.", "global ", "nonlocal "]):
        side_effects.append("state_mutation")
    if any(kw in code_lower for kw in ["log", "print", "debug"]):
        side_effects.append("logging")
    if not side_effects:
        side_effects.append("none")
    
    # Detect common patterns from code structure
    patterns = []
    if "class " in code and "__init__" in code:
        patterns.append("constructor")
    if "yield " in code:
        patterns.append("generator")
    if "async " in code or "await " in code:
        patterns.append("async")
    if "@" in code and "def " in code:
        patterns.append("decorator")
    
    return {
        "intent": intent,
        "complexity": complexity,
        "criticality": criticality,
        "patterns": patterns,
        "side_effects": side_effects,
        "data_flow": None,
        "confidence": 0.4,  # Low confidence for heuristics
    }


def analyze_function_semantics(
    name: str,
    code: str,
    repo_context: dict,
    neighbors: Optional[List[str]] = None
) -> dict:
    """Deep semantic analysis of function with full context.
    
    Args:
        name: Function name
        code: Function source code
        repo_context: Repository context from Phase 1
        neighbors: List of function names this calls or is called by
    
    Returns:
        Dict with semantic metadata including intent, patterns, semantic edges
    """
    api_key = os.environ.get("BOB_API_KEY")
    
    # Always get heuristic baseline
    heuristic_result = _heuristic_semantic_analysis(name, code, repo_context)
    
    if not api_key:
        return heuristic_result
    
    # Build context for Bob
    context_parts = [
        f"Project type: {repo_context.get('project_type', 'unknown')}",
        f"Domain: {repo_context.get('domain', 'general')}",
        f"Architecture: {repo_context.get('architecture', 'unknown')}",
    ]
    
    if neighbors:
        context_parts.append(f"Related functions: {', '.join(neighbors[:5])}")
    
    context_str = "\n".join(context_parts)
    
    prompt = textwrap.dedent(f"""
        Analyze this function semantically for a code graph.
        
        Context:
        {context_str}
        
        Function: {name}
        ```
        {code[:1000]}
        ```
        
        Respond as JSON with these keys:
        - intent: one of [orchestration, transformation, validation, persistence, retrieval, configuration, error_handling, computation, presentation, unknown]
        - complexity: one of [low, medium, high, very_high]
        - criticality: one of [low, medium, high, critical]
        - patterns: list of design patterns used (e.g., ["factory", "singleton"])
        - side_effects: list from [file_io, network, database, state_mutation, logging, none]
        - semantic_edges: list of dicts with {{type, target_name, description}} where type is one of [TRANSFORMS, VALIDATES, ORCHESTRATES, PERSISTS, RETRIEVES, CONFIGURES, HANDLES]
        
        Be concise and specific.
    """).strip()
    
    try:
        raw = ask(prompt)
        # Try to parse JSON response
        import re
        
        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw, re.DOTALL)
        if json_match:
            bob_result = json.loads(json_match.group())
            
            # Merge Bob results with heuristic baseline
            return {
                "intent": bob_result.get("intent", heuristic_result["intent"]),
                "complexity": bob_result.get("complexity", heuristic_result["complexity"]),
                "criticality": bob_result.get("criticality", heuristic_result["criticality"]),
                "patterns": bob_result.get("patterns", heuristic_result["patterns"]),
                "side_effects": bob_result.get("side_effects", heuristic_result["side_effects"]),
                "data_flow": bob_result.get("data_flow"),
                "semantic_edges": bob_result.get("semantic_edges", []),
                "confidence": 0.8,  # High confidence with Bob
            }
    except Exception:
        # Bob call failed, use heuristic
        return heuristic_result
    
    # If we get here, Bob response wasn't parseable
    return heuristic_result
