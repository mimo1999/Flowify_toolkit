# Change risk score and impacted-services catalogue

`GET /review` (and `pr_impact.review`) rates every **change chunk** for a human reviewer and lists what
the chunk can reach. A chunk is a changed function, or a **module-level edit** (a constant, a config
table, a class attribute) which has no function to attach to. Code: `backend/app/change_risk.py`,
wired in `backend/app/pr_impact.py`.

## Risk score (0 to 100)

Every point belongs to a named factor and carries a plain-language reason, so a reviewer can see why a
chunk ranks where it does.

| Factor | Max | Points |
|---|---|---|
| reach | 30 | log-scaled count of functions that depend on it (200 or more = full) |
| exposure | 20 | reached from API endpoints: `6 + 4 x endpoints`; itself an endpoint: 20; database or event role: 8 |
| coverage | 15 | no test in its caller chain (only when the repo has tests at all; test code is never "untested") |
| kind | 15 | `def` line changed 15, body edited 5, new function 3 (all added, nothing replaced), module-level edit 8 |
| size | 8 | changed lines, full at 40 |
| centrality | 6 | top 5% of the repo's nodes 6, top 20% 3 (needs the stored analytics) |
| crossings | 6 | 2 per other service that depends on it |
| contract | 10 | a static check found code this breaks 10; only things to look at 4 (see below) |
| history | 8 | the file was touched by revert/hotfix/rollback/incident commits in 12 months: 1 = 4, 2+ = 8 |

The maxima add up past 100; the score is clamped at 100. The score ranks **blast radius** (how far a
mistake can travel), not the probability of a bug.

Bands: **60 critical, 40 high, 30 medium**, below that low. **Must check** = critical or high, any
`breaks` finding, or one of the change's **3 highest-scoring chunks** (`pr_impact.mark_top`).

### Calibration (2026-11, vLLM, heuristic provider)

The regression replay (`benchmarks/regressions/replay.py`, 21 culprit/fix pairs) now scores every
changed function of the culprit commit and records where the *culprit chunk* lands: the changed
function the fix edited, or the one whose dependent the fix edited. 13 pairs have one. The replay has
no git checkout, so these scores carry no `contract` or `history` points.

| Rule for Must check | Culprits caught | Must-check items on PRs with 15+ changes |
|---|---|---|
| score >= 50 (the old high band) | 2 / 13 | 5, 5, 5, 0, 0, 0, 0 |
| score >= 40 | 3 / 13 | 12, 12, 12, 0, 3, 0, 0 |
| **score >= 40 or top 3 of the change** | **10 / 13** | 12, 12, 12, 3, 3, 3, 3 |
| score >= 40 or top 5 | 11 / 13 | 12, 12, 12, 5, 5, 5, 5 |

What it says:

- **The absolute score separates poorly.** Culprits scored 29 to 61; across all 1,060 changed functions
  only 4% reach 40. Many sit at exactly 30, the uncertainty floor, because vLLM's common method names
  make confidence low. A fixed cut-off either catches almost nothing or flags most of a PR.
- **Rank within the change works.** The culprit was in its change's top 3 ten times in 13, which is why
  the top 3 are always "Must check". On a small change (1 to 3 functions) that is everything.
- **Misses**: in #41184 (275 changed functions) the culprit ranked 105th; in #28549 8th of 18; in
  #25954 4th of 48.
- **Small sample**: 13 culprits from one repository. Re-run after any factor change:
  `python benchmarks/regressions/replay.py <vllm> --pairs benchmarks/regressions/vllm_pairs.json --out r.json`.

Planted bugs (`benchmarks/ai_review/run.py`, five subtle breaks in one vLLM diff): the static checks
flagged 4 of 5 (two redefinitions as `breaks`, a changed constant, a parameter no longer used); the
torch-version string comparison has no finding. With five changes in the diff the top-3 rule makes three
of them "Must check", so this set measures the checks, not the bands.

## Static contract checks (`contract_checks.py`)

Python only: the AST of the merge base against the head file, no LLM. Each finding carries the code
that proves it.

| Check | Severity | Example |
|---|---|---|
| a caller that bound to the old signature cannot bind to the new one | breaks | "`use` passes 2 positional arguments; it now takes 1" |
| a new definition takes over an existing name in the same block | breaks | "redefines `cdiv` from line 12: every call now goes to this one" |
| a new module-level function shadows an import | breaks | "shadows `join` imported at line 1; 3 calls in this file now go here" |
| a default value changed | check | "`timeout` default changed from `30` to `5`; 4 callers rely on it" |
| a parameter is still accepted but no longer used | check | "`max_len` is still accepted but no longer used" |
| a new method overrides one of the repo's base classes | check | "overrides `Base.run`, which has 12 callers" |
| a module constant changed value | check | "`LIMIT` changed from `10` to `99`; 6 files import it" |

A call with `*args`/`**kwargs` is "cannot tell" and never reported. Return-shape changes are not checked.

## Incident memory (`history.py`)

One `git log --since=12.months --name-only` pass, cached per HEAD. A commit counts when its subject
says revert, hotfix, rollback, incident or postmortem; a revert touches the files of the commit it
reverts, so those count without a second lookup. File-level only (per-function history is too slow).

## Confidence is separate from risk

`confidence.level` is `high`, `medium` or `low`, with `notes` saying why: unconfirmed callers, a name
shared by many functions, or (always, for module-level edits) file-level evidence only. Low confidence
never lowers a score. It raises a floor of 30, so thin evidence cannot make a chunk read as "low".

## Impacted-services catalogue

Each row's `impact` groups its dependents by service, with a count and a few examples, and lists API
endpoints, database access, events and tests it reaches (`count` is exact, `examples` is capped).
`crosses` is the number of services other than its own.

**What a service is.** Put an optional `flowify.services.json` at the repository root:

```json
{"services": [
  {"name": "scheduler", "owner": "@infra", "paths": ["vllm/v1/core/**"]},
  {"name": "api",       "owner": "@web",   "paths": ["vllm/entrypoints/**"]}
]}
```

Globs are matched against repo-relative paths (`**` crosses directories); the first matching service wins. A
bad file is ignored and reported in `services_config.error`. Without a file, a service is the
directory prefix at the smallest depth (up to 3) where the non-test code splits into two or more groups.

## Module-level chunks

`module_chunks` lists changed lines outside every function (Python only, it needs the AST): `names` are
what the edit defines, `importers` are files importing one of those names, `module_importers` files
importing the whole module. A class attribute change counts as a change to the class.

## Claude's notes (`ai_review.py`)

Off unless `ANTHROPIC_API_KEY` is set (in the environment, or in the repo-root `.env`, which is read for this feature only and never exported, so ingest summaries do not switch to paid Claude calls); runs only when a person clicks (the must-check bucket, at most 8
chunks a run, 4 at a time). Model `claude-haiku-5-5` by default (set `FLOWIFY_REVIEW_MODEL=claude-opus-5-5` for sharper notes at about 35 times the cost), effort `medium`
(`FLOWIFY_REVIEW_EFFORT`), with the server-side refusal fallback on.

- **Context per chunk**: the changed code with line numbers, removed lines, the static findings, each
  caller's call site (plus or minus 3 lines), the signatures it calls, the tests that reach it; capped
  at 40,000 characters, farthest callers dropped first and the model told.
- **Grounding gate**: every note must quote a line that was in the context, within 2 lines of where it
  says; otherwise it is dropped and the UI says how many were dropped.
- **Never moves a score or a bucket.** Notes are a separate section and a separate count.
- **Prompt injection**: repository text is wrapped as data and the system prompt says never to follow
  instructions in it; the model has no tools and a fixed JSON schema.
- **Cost**: results cached on disk by content (`_store/llm_cache/review/`), so an unchanged chunk is
  never sent twice; token use is shown after each run and logged (`flowify.ai_review`).
- **Fixes** are shown to the author only, never to the reviewer.
- **First measurement (2026-11-24, `claude-opus-5-5`, effort medium)**: on the planted-bug diff the
  three must-check chunks were sent; Claude found the planted bug in all three, with grounded notes
  naming the failure (`cdiv(16, 16) == 2`; `"2.9.0" >= "2.12.0.dev"` is True; loggers lose
  `warning_once`) and real call sites. That includes the torch-version string comparison, which the
  static checks and an earlier blind agent test both missed. 7 notes, 0 dropped by the grounding gate,
  37,352 input and 2,362 output tokens (about $0.20 at list price). The two bugs in "Worth a glance"
  were not sent. **Still unmeasured**: the false-note rate, since every chunk sent held a bug; the
  feature stays labelled experimental until clean chunks are scored too.
- **Same set on `claude-haiku-5-5`** (effort medium): also found all three, 5 notes, 0 dropped, 37,352
  input and 3,560 output tokens (about $0.006, roughly 35 times cheaper). Weaker in one place: on the
  logger it asked the author to confirm the old definition did nothing important, where Opus named what
  breaks (loggers lose `warning_once`). It also skipped two secondary points Opus made (negative numerators
  in `cdiv`, the dead version fallback). Three chunks is a small sample; Haiku 5.5 is the default for cost.

## Review queue and author mode (`review_queue.py`)

- `GET /queue`: branches ahead of the base with a commit in the last 30 days (the UI can widen this to
  a year), each scored from the *loaded* graph by mapping the base-side hunks onto it. An estimate,
  labelled as one; added functions are counted, not scored.
- `POST /queue/open`: a detached `git worktree` of the branch under `_store/.worktrees/` (a dot folder,
  so ingestion never walks into it), ingested once and updated incrementally after. At most 5;
  the least recently used is removed with its graph. `DELETE /queue/worktree` removes one.
- `POST /review/tests`: runs the pytest tests that reach one chunk (ids from the graph, list argv, 300 s
  timeout, the repo's own venv when it has pytest).
- `PUT /review/notes`: writes `.flowify/review.json` (per chunk: hash, note, test outcomes, head). The
  author commits it; the reviewer sees it as self-reported, and as stale when the chunk hash moved.
- `parts`: changed functions grouped by a call between them or a shared direct caller; two or more
  groups of 30+ lines are shown as "could be split".

## Where a reviewer sees it

- **Review screen** (`QueuePanel.jsx`, `ReviewPanel.jsx`): Review is the home screen for a local
  checkout. The queue lists branches by their worst chunk, plus "My working copy" (author mode). A
  branch review groups chunks into **Must check** (the change's top 3, critical or high, or any `breaks` finding), **Worth a
  glance** (medium) and **Skim** (low, collapsed, one click marks all skimmed), under a verdict strip
  (must-check count, breaks, untested, could be split, Claude notes). The evidence pane leads with the
  author's note, static findings and Claude's notes, then the diff, the callers not in the change,
  and folded "What it can reach" and "Why N". Checked state is stored per browser by chunk hash, so an
  edited chunk comes back as "changed since you checked", and a re-review shows only those by
  default. `f` flags a chunk as needing changes; **Finish** copies a Markdown summary for the PR.
- **PR comment**: `python backend/app/pr_impact.py --repo . --base origin/main` prints a table ranked by
  score (change, reach, test in chain) plus a collapsed "Why these scores" with every factor. Low
  confidence is marked with a warning sign.
- **Agents**: the same data is the JSON of `GET /review` (`functions`, `module_chunks`, `pointers`).

## Limits

- The catalogue follows **in-process calls** and the endpoint, database and event tags (heuristic,
  noisy). HTTP, gRPC and queue links between deployed services are mostly invisible to a static call graph.
- Uses of a changed constant inside its own file are not found; imports are matched on the dotted
  module's tail, so two modules with the same short name can be confused.
- A change to a decorator line, or the deletion of a function inside a surviving file, is not a chunk.
- Scores for JavaScript, TypeScript, Java and C/C++ rest on the regex parsers, which resolve calls by name and
  are noisier (a common function name can collect dependents it does not have); treat those scores as hints.
- Module-level chunks need a defined name (a constant, a config table, a class attribute). A changed import,
  docstring or bare module-level call is not a chunk, and test files never produce them.
- `/impact` (the Inspector) still uses its own coarser risk label; the review score is not shown there.
- Queue scores are estimates from the loaded graph; opening a branch takes a full ingest the first time.
- Checked and flagged state is per browser; `.flowify/review.json` is the only shared channel.
