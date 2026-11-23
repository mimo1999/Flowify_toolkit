# UX audit and streamlining (2026-11)

## The use case

Someone has to **change code they did not write** (or review a change to it) and needs to know:
what is this function, what does it touch, and what breaks if it changes. Flowify answers that
with a call graph plus a blast-radius ("impact") calculation. The graph and impact numbers are
deterministic. Summaries and Q&A depend on an LLM and are only basic without one.

## What was wrong

| # | Finding | Effect |
|---|---------|--------|
| 1 | Sidebar had ~15 controls at equal weight: three coloured full-width buttons, stats (graph id, "Expanded: 0"), four export buttons, a 20-row legend. | Nothing read as the primary path; users could not tell where to start. |
| 2 | Four overlapping floating panels (impact bottom-left, query results right, trace right, two modals), at 9-11px text with slate-600 on near-black. | Dense, hard to read, low contrast. |
| 3 | The impact panel never showed the **source code**, and listed only callers, not callees. Rows were not clickable. | Useless for code review: you still had to open an editor. |
| 4 | "Find a function" and deep links selected a node that was not on the canvas, so nothing appeared on the graph. | Looked broken. |
| 5 | Each node encoded importance five ways (size, glow, border width, stripe, dots) plus role badge and caller/callee badge. | Visual noise; no single meaning. |
| 6 | "Low/High risk" with no sentence saying why; an API endpoint with no callers read as "no callers, so safe". | Risk label was easy to misread. |
| 7 | Stub (no-LLM) mode was shown as a small badge only; node text like "Returns flow summary" looked like a bug. | Users did not know summaries were placeholders. |
| 8 | An unrelated error (e.g. a failed lookup) re-opened the repo form. | Lost context. |

## What changed

- **Inspector** (`Inspector.jsx`, replaces `ImpactPanel.jsx`): one right-hand panel for the selected function.
  A plain-English verdict first ("Changing this can affect N functions (M call it directly)", with the
  unconfirmed-callers and graph-coverage caveats), then tabs: **Source** (real line numbers), **Callers**,
  **Calls**, **Notes** (docs, TODO/FIXME, dependent areas). Every caller/callee is a button that re-roots the canvas on it.
- **Backend**: `GET /node_source` (one row read, capped at 400 lines) and `GET /focus` (a function in graph-node shape).
- **Sidebar**: repo form collapses to one line after ingest; order is Find a function, View (Start / Areas / Functions),
  Analyse (three equal rows), then collapsed Export and "How to read the graph". Stats block removed.
  Stub mode now says "No AI model connected" and what that means.
- **Canvas**: dropped glow and border-width importance encodings; size and dots remain.
- **Type and contrast**: no text below 11px; slate-600/700 text raised to slate-400.
- **Deep links / search** reveal the function on the canvas (`revealFunction`).

## Second pass: review a change

- **Review a change** (`ReviewPanel.jsx`, `GET /review`): the UI form of `pr_impact.py`. Compares the working copy
  with `HEAD` (uncommitted edits) or a branch, and lists the changed functions most-depended-on first, each with
  its risk, dependents, API endpoints it reaches, unconfirmed callers and whether any test reaches it.
  It syncs the graph first so line numbers are current. Local checkouts only.
- Picking a row opens the Inspector with the **changed lines marked** in the source, and a "Back to the review list" link.
- The canvas now ends where the open right-hand panel begins and refits, so nothing sits hidden under a panel.
- A lone node no longer zooms to fill the canvas. Dead glow/border helpers removed.

## Still recommended (not done)

1. **Merge Query results, Trace, Review and Inspector into one right dock** with tabs. They still stack on the same edge.
2. **Review limits:** files not yet tracked by git are not in the diff; "no test reaches it" only sees tests inside the
   analysed folder; there is no before/after diff view, only the new side with changed lines marked.
3. **Heuristic semantic tags are noisy** without a model (e.g. `side_effects: database` on `_entry_nodes`). They are no
   longer shown in the Inspector, but still feed risk floors.
4. **Mobile/tablet**: the layout is desktop-only (fixed 288px sidebar plus 420px dock).
