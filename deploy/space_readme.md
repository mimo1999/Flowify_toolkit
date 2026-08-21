---
title: Flowify
emoji: 🕸️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Paste a public GitHub URL, get an interactive code-flow graph.
---

# Flowify

Paste a public GitHub/GitLab/Bitbucket/Codeberg URL and Flowify clones it,
builds a function- and module-level call graph, and lets you explore it
interactively — or export it (JSON, Mermaid, or a ready-to-paste Markdown
report) to use in any LLM chat.

This Space runs with no server-side LLM key: graphs are built with
Flowify's deterministic AST-based analysis, and the "Copy for LLM" export
gives you a full architecture report to paste into your own ChatGPT/Claude/
whatever conversation for free. Graphs are scoped to your browser session
and expire after 24 hours — nothing is kept beyond that.

Full source, local install (`docker run`), and the underlying architecture:
https://github.com/mimo1999/Flowify_toolkit
