# Parser review (2026-11)

Scope: `backend/app/graph_builder.py` (1,193 lines) and the file walk in `ingestion.py`.
Every mistake below was reproduced on a few-line repo unless marked "by reading".

## How it works

1. **Walk** (`ingestion.iter_source_files`): every file with a known extension, minus ignored dirs.
2. **Parse, one file at a time** (`parse_file`), into nodes (file / class / function / method) and edges.
   - **Python** uses the real `ast` (`_Visitor`). For each function it records the node, a `DEFINES`
     edge from its parent, and one unresolved call edge per call site: target `<symbol>::name`, plus the
     receiver text (`self`, `self.repo`, `np`, or none), the enclosing class, the line and whether the
     call sits in a branch / loop / try. It also keeps three per-file hints: the import map, the types
     of `self.x = Cls(...)` set in `__init__`, and the types of local `x = Cls(...)`.
   - **JS/TS, Java, C/C++** use line regexes: a line that looks like a class or function header opens a
     node, braces are counted to find its end, and every `name(` in the body becomes a call.
3. **Give duplicate ids a suffix** (`_dedupe_node_ids`).
4. **Resolve** each `<symbol>::name` to at most one node (`_resolve_call_target`), first match wins:
   local variable's class, `self.foo`, `self.attr.foo`, `Receiver.foo`, `module_alias.foo`, same-file
   function, the only function of that name in the repo, constructor, the only method of that name.
   Anything else is dropped and remembered on the caller as an "unresolved call".
5. **De-duplicate edges** and build the NetworkX graph.

Everything downstream (impact, review pointers, severity, hotspots, dead code) reads this graph.

## Mistakes that create wrong links (false positives)

| # | Mistake | Effect | Where |
|---|---|---|---|
| 1 | A method call is matched to a top-level function of the same name. The "only function with this name" rule runs even when the call has a receiver, and has no common-name guard. | `expanded.update(...)` (a set) links to `pipeline.update`. These are today's false "Critical" pointers. Same for any `.run()`, `.save()`, `.load()`, `.add()` when one such function exists. | resolver, unique-function tier |
| 2 | Calls inside a nested function are also attributed to the outer function. `_ordered_calls` walks into nested defs, then the nested def is visited again. | `outer -> target` although only `outer.inner` calls it. Inflates callers. | `_visit_func` |
| 3 | Regex adapters treat control statements as functions. | JS: `if (x) {` and `for (...) {` inside a class become methods `A.if`, `A.for`. Java and C: `else if (...) {` becomes a method/function named `if`. Their bodies' calls are attributed to these fake nodes. | `method_re` (JS, Java), `function_re` (C) |
| 4 | An `INHERITS` edge is resolved like a call, so it lands on the base class's `__init__`, not the class. | `Child EXTENDS Base.__init__`. Inheritance is also counted in the call-resolution statistics. | resolver loop |
| 5 | Semantic tags use substring matches on lowercased source, docstrings included. | `torch.save(` or the word "select " in a docstring gives `USES_DB`; a decorator containing "get", "action" or "view" (`@budget`, `@transaction.atomic`) gives `EXPOSES_API`. In this repo `_dedupe_node_ids` and `_detect_semantic_kind` are tagged database code. These tags set severity floors and the "reaches API" pointers. | `_detect_semantic_kind` |

## Mistakes that lose real links (false negatives)

| # | Mistake | Effect | Where |
|---|---|---|---|
| 6 | Two definitions with the same qualified name share one id. The node is renamed afterwards; its edges are not. | A property setter (`@x.setter`) gets no `DEFINES` edge and its calls are attributed to the getter. Same for `@overload` and `if/else` definitions. | `_dedupe_node_ids` |
| 7 | Module-level and class-body calls are never recorded. | `main()` under `if __name__ == "__main__":`, `app = create_app()`, `register(handler)` produce no edge, so entry functions look uncalled (and can be reported as dead code). | `_Visitor` |
| 8 | A nested function is typed "method", and the "enclosing class" includes function names. | `helper()` calling a nested `def helper` is unresolved; `self.other()` inside a closure only resolves through the weakest rule, or not at all. | `visit_FunctionDef`, `_visit_func` |
| 9 | `from a import run` is ignored for bare calls. The import map is only used for `alias.name(...)`. | If two files define `run`, the call is dropped as ambiguous although the import says exactly which one. Same for constructors of same-named classes. | resolver, bare-call tiers |
| 10 | Inheritance is not followed. | `self.shared()` where `shared` lives on a base class is unresolved, or goes to an unrelated class if that is the only `shared` elsewhere. | resolver, `self` tier |
| 11 | Non-Python method calls can never resolve. Regex adapters record no receiver, and only the function tiers accept them. | Java has only methods, so a Java repo gets essentially no call edges. Same for `this.foo()` in JS/TS and C++ members. | resolver |
| 12 | Files that fail to parse are dropped silently. UTF-8 files with a BOM fail (`utf-8` instead of `utf-8-sig`). | The file's functions vanish with no warning; a change to them maps to nothing. | `parse_python_file` |

## Smaller issues (by reading)

- Module alias resolution uses only the file stem, so two `utils.py` files make `utils.helper()` ambiguous, and only files with a top-level function are indexed.
- Brace counting and call extraction in the regex adapters read strings and block comments as code.
- `x = Cls(...)` typing ignores scope (it scans nested defs) and parameter annotations (`def f(p: Pooler)`).
- Function references that are not calls (`Thread(target=f)`, `Depends(f)`, decorators) create no edge. A limit, not a bug.

## Status

Fixed, each with a test in `tests/test_resolver_tiers.py`: #1, #2, #4, #5, #6, #7, #8, #9, #10, #12, and the
file-stem ambiguity from the smaller issues. Not fixed: #3 and #11 (the regex parsers for JS/TS, Java, C/C++).

Known side effects: a module-level call makes the *file* node a caller, which the review screen lists
without source; nested functions are now typed "function" (they were "method").

## Suggested order

1. **#1**: a few lines, removes the false "Critical" pointers seen in the review screen.
2. **#2, #6, #8**: one pass over `_visit_func` (skip nested defs when collecting calls, type by the real parent, unique id before edges are built).
3. **#9, #10**: use the import map and base classes in the resolver. Likely the largest gain in real links.
4. **#7**: record module-level calls on the file node.
5. **#4, #12**: small and isolated.
6. **#5**: match decorators by segment, strip docstrings and comments before the DB/event patterns.
7. **#3, #11**: only if non-Python repos matter now. The honest alternative is to label those languages as structure-only.

`benchmarks/accuracy` and the vLLM replay can measure each step.
