# Your codebase already knows the answer. It just can't talk.

Every non-trivial codebase contains a complete, accurate model of how the system actually behaves — which function calls which, what touches the database, what happens when a request comes in. That model isn't in the README. It isn't in the architecture diagram someone drew eighteen months ago and never updated. It's scattered across thousands of files, encoded in the one place that can never lie about behavior: the code itself.

The problem is that nobody can hold that model in their head, and neither can an LLM you casually point at a repo. Ask an AI assistant "what happens when a payment fails?" and it will confidently generate a plausible-sounding answer — stitched together from patterns it's seen in *other* codebases, not proof of what *this* one does. That's the gap Flowify is built to close.

## What it actually does

Flowify reads a repository once, parses it into a call graph — real AST parsing for Python, structural analysis for JS/TS, Java, and C/C++ — and turns that graph into three things at once:

1. **A dual-layer, explorable map.** Every function is a node; every call is an edge. Zoom out and you see modules, discovered automatically by clustering the call graph (the same family of algorithm used for community detection in social networks, pointed at your code instead). Zoom in and you see individual functions, colored by role — this one hits a database, that one emits an event, this one is a public API surface.

`[A DIAGRAM HERE: a zoomed-out module-level graph on the left morphing/zooming into an individual function-level graph on the right, showing the "depth 1 → depth 3" drill-down concept]`

2. **A queryable knowledge base.** Ask "what happens when a payment fails?" in plain English, and Flowify doesn't guess — it retrieves the actual subgraph of functions involved, walks the real call chain up to two hops out, and asks an LLM to explain *that specific evidence*, node by node, edge by edge. The answer comes back with the exact functions and files it's grounded in, so you can verify it in ten seconds instead of trusting it blindly.

3. **A living second brain for the codebase.** Every query is remembered. Every piece of feedback ("this was helpful" / "this missed the mark") shifts what gets retrieved next time. The system builds its own terminology map over time — learning that when *your* team says "auth," they mean these fourteen specific functions, not a generic dictionary definition of authentication.

## Why this is a different bet than "AI that reads your code"

There's no shortage of tools that let you chat with a codebase. Almost all of them work the same way: chunk the source into text, embed it, do similarity search, stuff the top-k chunks into a prompt, and hope. That approach answers "what text looks similar to this question," which is a proxy for "what code is actually involved" — a proxy that quietly breaks down the moment the real answer requires understanding *relationships*: call chains, blast radius, control flow.

Flowify skips the proxy. It doesn't search text — it walks a graph. When you ask what depends on a function, it doesn't guess from vector similarity; it does a graph traversal and returns the literal callers and callees, annotated with *why* each edge exists and how confident the system is in that evidence — AST-derived (100% confidence), regex-inferred (70%), or LLM-inferred (whatever the model says). Every claim Flowify makes about your code carries its receipts.

`[A DIAGRAM HERE: side-by-side comparison — "Chat-with-your-code (RAG over text chunks)" showing fuzzy dotted lines between disconnected code snippets, vs "Flowify (GraphRAG)" showing a clean traversable graph with solid, labeled edges]`

That single architectural choice is what unlocks everything else that text-chunk tools structurally can't do well:

- **Impact analysis that's actually correct.** "If I change this function, what breaks?" isn't a similarity search — it's graph reachability. Flowify BFS-walks downstream from any node and tells you, concretely: these are your direct callers, this is what gets touched transitively, here's whether any of it hits a database, here's a computed risk level.
- **Architectural X-rays for free.** Because the whole repo is one graph, standard graph theory just falls out: PageRank finds your most load-bearing functions without anyone nominating them. Articulation points reveal the single files that, if broken, disconnect entire subsystems — your real architectural bottlenecks, discovered mathematically instead of by folklore. Cycle detection surfaces circular dependencies nobody remembers introducing.
- **Dead code and developer archaeology.** Zero-inbound-edge functions become dead-code candidates. Every `TODO` / `FIXME` / `HACK` comment gets extracted and attached to the exact function it lives inside, so "why is this written this way" has an answer that isn't "ask someone who left the company."

`[A DIAGRAM HERE: a small illustrative call graph with a few nodes highlighted — one glowing large as a "god node" (high PageRank), one marked with a red X as a "bridge/articulation point," one greyed out as "dead code"]`

## The part that should feel a little uncanny

Point Flowify at itself, and it will draw its own architecture, in seconds, from nothing but the code — no diagram anyone hand-maintained, no wiki page slowly rotting out of date. That's the actual promise: not a document *about* the system, but a live reflection *of* the system, that's structurally incapable of drifting from the truth, because it's regenerated from source every time you ask.

And when the code changes? Flowify diffs against the last-seen git commit and updates only what moved — it doesn't re-read the whole repository from scratch every time, so the map stays current without becoming a tax on every commit.

## Why you'd actually reach for this

- **Onboarding a new engineer** used to mean a week of "let me walk you through the codebase" meetings. Now it means: open the map, ask it questions, get grounded answers with the actual code attached.
- **Reviewing a risky PR** used to mean trusting that the author understood the blast radius. Now it means: click the function, see the real downstream impact, see the risk score, see whether it touches a database — before you approve.
- **Debugging a system you didn't write** used to mean grepping for a function name and hoping you found every caller. Now it means: ask what happens on this path, and get the actual path, not a plausible-sounding fiction.
- **AI coding assistants** used to reason about your code with nothing but whatever fits in a context window. Point one at Flowify through its MCP server and it can ask real, structured questions — *shortest path between these two functions*, *what's the blast radius of this change*, *find every function like this one* — and get graph-grounded answers back, not vibes.

## The honest caveat

Flowify doesn't pretend to be a perfect compiler. Call resolution for non-Python languages currently leans on structural pattern-matching rather than a full parser, so it's closer to "very good approximation" than "provably exact" outside Python — and it says so: every edge in the graph is tagged with its own confidence and provenance, so you always know whether you're looking at ground truth or an educated inference. That honesty is deliberate. A map that admits its own uncertainty is more trustworthy than one that doesn't, and it's a lot more useful than a black box that answers every question with the same unearned confidence.

## The bet, in one sentence

The best documentation isn't written once and left to rot — it's derived, live, from the only source that can't lie about your system: the code itself. Flowify is a bet that once you can *ask* your codebase questions and get answers with receipts, you stop needing to trust anyone's mental model of the system — including your own.

`[A DIAGRAM HERE: a simple before/after — "before" showing a developer surrounded by scattered docs, Slack threads, and outdated diagrams with question marks; "after" showing the same developer with one graph and a chat box, both pulled directly from the live codebase]`
