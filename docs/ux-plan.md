# UX plan: "where to look" in a pull request (2026-11)

Product position: for a PR with hundreds of changed lines, Flowify says **where else to look** and
gives evidence a human or an agent can check without trusting the tool. It does not claim to find bugs,
and it never says "safe".

Grounding: the vLLM replay (`benchmarks/regressions/README.md`). Half of real regressions broke inside
the changed function; the useful catches were callers and tests the author did not update; a pointer
buried in a 439-row list is not a catch.

## Step 1a. Who uses it

| | Reviewer (primary) | Author (secondary) | Coding agent (secondary, no UI) |
|---|---|---|---|
| Who | Senior engineer reviewing a PR they did not write, in code they half know | Engineer about to ask for review | An agent writing or reviewing the PR |
| Situation | 200 to 800 changed lines, 15 to 30 minutes, several PRs queued | Just finished the change, wants no surprises in CI or review | Has the diff, limited context budget |
| Question | "What does this affect that the diff does not show me?" | "What did I forget to update?" | "Which files must I read before I approve or edit?" |
| Fear | Approving something that breaks a caller they never opened | A reviewer or CI finding the obvious miss | Reading the whole repo, or missing a dependent |
| Needs | A short ranked list, each item checkable in under a minute | The same list, before pushing | The same list as JSON, with call sites |
| Will not do | Explore a graph, read 400 rows, learn a new tool mid-review | Wait minutes for analysis | Parse prose |

Not a target for now: the newcomer who wants to understand a codebase. Today's default screen (the
graph canvas, Start / Areas / Functions) is built for them. It stays, but stops being the front door.

The buyer (an engineering manager) needs proof it saves review time. That is a report, not a screen, and
is out of scope here.

## Step 1b. The reviewer's journey

| Stage | What they do | What they need from us | Today |
|---|---|---|---|
| 1. Arrive | Open the PR on GitHub; see a Flowify comment | One sentence: how many places outside the diff deserve a look | Comment is a table of counts; no links into the app |
| 2. Triage | Decide if it is worth opening | The top 3 to 5 pointers, most important first, each with a reason | Rows sorted by dependents; no reason, no cap |
| 3. Pick | Click one pointer | Land on that pointer, not on a repo home page | Must ingest the repo, open Review, find the row |
| 4. Verify | Check the claim | The changed lines (before and after) next to the caller's call site, with the rule that linked them | Only the new side, in a 420px panel; caller source is a second click that replaces the first |
| 5. Decide | OK, comment on the PR, or ask for a test | Mark it checked; copy a link or a ready-made comment | No state; nothing to copy but a node link |
| 6. Finish | Know they are done | "5 of 5 checked" and a plain list of what the tool could not check | No progress, caveats scattered per row |

The author's journey is stages 2 to 6 run locally against the working copy. The agent's journey is
stages 2 and 4 as one JSON call.

What a pointer is, in priority order:

1. **Not updated with the change**: a direct caller or a test that reaches a changed function but is not in the diff. (This was the #57053 catch.)
2. **Changed and untested**: no test anywhere in the caller chain.
3. **Reaches an API endpoint**: the change is visible outside the repo.
4. **Could not be checked**: name shared by many functions, or deleted files. Shown separately as limits, never as findings.

## Step 2. UI changes

### Principles

- **The diff is the home screen.** The graph is a supporting view.
- **A short list.** Show at most 7 pointers; the rest sit behind "Show all N", grouped by file.
- **Every pointer carries its evidence**: the call site (`file:line`), the rule that linked it, and the confidence. The graph already stores all three per edge.
- **Limits are stated once, in one place**, in plain words.
- **One workspace.** The four right-hand panels (Query, Trace, Review, Inspector) stop stacking.

### The review workspace

```
+--------------------+---------------------------------------------+
| WHERE TO LOOK  2/5 | Pooler.forward changed          bert.py:641 |
|                    | - token_ids = meta.prompt_token_ids         |
| Not updated (3)    | + ids = meta.get_prompt_token_ids_cpu()     |
|  > test_splade...  +---------------------------------------------+
|    test_pooler.py  | Called from test_splade_pooler_matches...   |
|  > Embedder.run    |   test_splade_sparse_pooler.py:52           |
| Untested (1)       |   pooled = pooler(hidden_states=hs, ...)    |
|  > _pad_ids        | Linked because: `pooler` is a               |
| Reaches API (1)    |   SPLADESparsePooler (assigned line 51)     |
|                    |                                             |
| Could not check    | [Mark checked]  [Copy as PR comment]        |
|  2 names too common| [Show neighbourhood graph]                  |
+--------------------+---------------------------------------------+
```

- **Left, the worklist** (replaces the Review panel's flat list): pointers grouped by the four kinds above, a checked state per item, a progress count, and "Could not check" pinned at the bottom.
- **Right, the evidence** (replaces the Inspector for review): top half is the changed function with a before/after diff; bottom half is the selected caller's call site with the calling line marked and the linking rule in a sentence.
- **Graph on demand**: "Show neighbourhood graph" opens the changed function plus its direct callers only. The full canvas is not shown during review.
- **Keyboard**: `j`/`k` to move through pointers, `x` to mark checked. Reviewers live on the keyboard.

### Entry points

- **PR comment**: one sentence, then the top 5 pointers as links that open the workspace on that pointer. The count table moves under a collapsed "Details".
- **Local**: "Review a change" is the first screen once a repo is loaded, defaulting to uncommitted edits.
- **Agent**: the same pointers as JSON from one `/mcp` call (diff in, pointers with call sites out).

### What moves or goes

| Now | Becomes |
|---|---|
| Graph canvas as home | An "Explore" tab, with Ask, Trace, Hotspots and Summary inside it |
| Four right-hand panels | One evidence pane; Explore keeps its own |
| Risk label per row (Low/High) | The reason in words ("3 callers not in this PR"); the label was hard to act on |
| "+N unconfirmed callers" per row | One "Could not check" section |
| Sidebar tools list | Two tabs at the top: Review, Explore |

### States to design

- **Nothing found**: "No callers or tests outside this PR depend on the changed functions that the graph could resolve," followed by the coverage figure. Never a green tick.
- **Very large PR** (hundreds of changed functions): group by file, lead with "not updated" only, say the list is cut.
- **Waiting**: analysis takes 45 to 280 seconds on a vLLM-sized repo. The comment path needs a cached graph per branch; locally, show the diff at once and fill pointers in as they arrive.
- **No local checkout**: say what is needed; do not show an empty workspace.

### Order of work

Status: steps 1 to 3 are built, local-only: the review queue is the home screen for a local checkout,
with Review and Explore as two modes; a branch review has buckets, static contract checks, optional
Claude notes, re-review by chunk hash, flags and a finish summary; author mode runs reaching tests and
writes `.flowify/review.json`. See [change-risk.md](change-risk.md). Not yet: the neighbourhood graph
(the button re-roots the full canvas instead), pointers for tests more than one call away, the PR-comment
deep links, step 4 (band calibration and the Claude eval in `benchmarks/ai_review/`).

1. **Data**: `/review` returns ranked pointers with kind, call site, rule and before/after hunks. Nothing visible changes yet; the JSON for agents falls out of this.
2. **Workspace**: the two-pane review screen, checked state (kept in the browser), keyboard moves.
3. **Entry**: PR comment with deep links; Review as the first screen; Explore tab.
4. **Measure**: the replay reports the rank of the fix site and list length, so we can tell whether ranking helps.

### Open decisions

- Reviewer as the primary persona (assumed here) or author. It changes the default entry (PR comment vs local working copy), not the workspace.
- Whether checked state should be shared across reviewers. That needs accounts and storage; browser-only is assumed for now.
