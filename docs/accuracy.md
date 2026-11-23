# How accurate is the call graph?

Flowify builds its call graph statically, and static call graphs of Python are incomplete by nature:
dynamic dispatch, decorators and dependency injection hide calls. So instead of asserting accuracy,
this page measures it against what really ran.

## Method

`benchmarks/accuracy/measure.py` runs a repository's own test suite under a call tracer. For every
call between two functions of the project (test code excluded) it records the caller, the callee and
the source line of the call. It also records every source line that made *any* call. The static graph
is then scored against that record.

| Measure | Definition |
|---|---|
| **Recall** | Calls that really happened and are in the graph ÷ calls that really happened |
| **Precision** | Per resolver tier: edges whose call happened ÷ edges the tests could judge |
| **Judged** | Share of the graph's edges the tests could judge at all |

An edge is **verified** if the call happened, **refuted** if its call-site line ran but that callee
was never reached, and **unjudged** if the tests never ran the line. Unjudged edges are left out of
precision, and the *Judged* column says how many that is, so a number is never read as covering more
than it does.

```bash
python benchmarks/accuracy/measure.py <repo> --src src --out result.json
```

The repository's dependencies must be importable, because its tests run in the same process. Raw
results are in `benchmarks/accuracy/results/`.

## Results

Measured on the latest commits at the time of writing: click `06b2a67`, jinja `5ef7011`, flask `d73fa1c`.

| Repo | Functions | Real calls seen | Recall before → now | Precision before → now | Judged |
|---|---|---|---|---|---|
| click | 583 | 654 | 39% → **52%** | 95.2% → **95.8%** | 76% |
| jinja | 758 | 1,372 | 33% → **51%** | 95.5% → **96.7%** | 86% |
| flask | 385 | 386 | 26% → **40%** | 84.6% → **83.2%** | 75% |

"Before" is the resolver as of the first measurement. The gain came from measuring what was missing
and adding two resolver tiers, each checked against these numbers:

- **Constructor calls** — `Foo(...)` resolves to `Foo.__init__` (100% precision on all three repos).
- **Unique method match** — `obj.save()` resolves when exactly one method named `save` exists, except
  for names shared with builtins and the standard library (`get`, `read`, `info`, ...). Precision 96%
  on click, 99% on jinja, 76% on flask.

### Precision by resolver tier (now)

| Tier | click | jinja | flask |
|---|---|---|---|
| self call in the enclosing class | 94% | 95% | 80% |
| same-file function | 97% | 98% | 100% |
| unique repo-wide function | 95% | 89% | 90% |
| unique repo-wide method | 96% | 99% | 76% |
| constructor call | 100% | 100% | 100% |
| module alias / receiver class / attribute type | 100% | 100% | 100% |

Flask's lower numbers come mostly from polymorphism: `self.method()` is resolved to the method of the
class it appears in, while at run time a subclass's override ran. That counts once as a wrong edge and
once as a missed call.

### What the graph still misses

Of the real calls the graph does not contain, part are *known*: the caller made a call with that name
but it matched several functions, so the graph recorded it as ambiguous instead of guessing (click 132,
jinja 195, flask 45). The rest are invisible to a static reader: methods called through receivers whose
type is not known, dunder methods called implicitly by the interpreter (`__enter__`, `__getattr__`),
and callbacks.

### "No static callers" (dead-code candidates)

The Insights list of functions with no resolved caller was checked against the same test runs: a
candidate counts as wrong if project code called it while the tests ran (calls made only from tests
are not recorded, so the rate is a lower bound). The list shows the first 50.

| Repo | Naive `fan_in == 0` (first 50) | Now | Wrong now |
|---|---|---|---|
| click | 66% wrong | 15 candidates | 0 |
| jinja | 76% wrong | 36 candidates | 16 (44%) |
| flask | 16% wrong | 13 candidates | 0 |

The naive rule flagged dunder methods, subclass overrides, callbacks and closures. It now skips
dunders, methods sharing a name with a called function (overrides), names some function called
without the graph being able to pick one target, and names that appear in the code of another
function (passed as `key=`, registered, returned). What remains wrong is mostly functions
registered by string in a module-level table (jinja's `do_*` filters), which no function body
mentions. The UI calls the list "no static callers", not "dead code".

## What this means for use

- **The edges shown are mostly right, and each carries its confidence and the reason it was chosen.**
  Trace steps and impact callers show the confidence when it is below 90%.
- **The graph is not complete.** Roughly half of real calls are absent. Impact analysis therefore also
  lists **possible callers**: functions that made a call matching this one's name that the graph could
  not resolve, so a missing caller shows up as a warning rather than as silence.
- **Impact risk follows callers, not callees.** A function's risk comes from how many functions can
  reach it, and it is raised, never lowered, by unconfirmed callers.
- **Numbers describe code the tests exercised.** Code the tests never run cannot be judged, and
  the suites differ in how much of the library they cover.

## Limits of this measurement

- Only Python is measured; other languages use a simpler resolver with no runtime check.
- A call-site line that ran without reaching the callee counts as a refutation. A short-circuited
  expression on the same line (`a() or b()`) can make a correct edge look refuted, so precision is
  slightly pessimistic.
- Three libraries are a small sample, all well-tested and pure Python. Application code with heavier
  framework use, dependency injection or metaprogramming is likely to score lower.

## Risk score calibration (2026-11)

On 13 vLLM regressions whose culprit change is a function, the old band cut-offs (70/50) put 2 culprits
above "medium"; "score >= 40 or top 3 of the change" puts 10 in "Must check", with at most 12 such items
on a 275-function PR. Details and the table: [change-risk.md](change-risk.md#calibration-2026-11-vllm-heuristic-provider).

## Claude review (2026-11, planted vLLM bugs)

Claude was sent the three must-check chunks of a diff with five planted bugs and found the bug in all
three (7 grounded notes, 0 dropped), including one the static checks miss. The false-note rate is not
measured yet. Details: [change-risk.md](change-risk.md#claudes-notes-ai_reviewpy).

## After the parser fixes (2026-11, Flowify's own tests)

`docs/parser-review.md` lists twelve mistakes found by reading the parser; ten are fixed. Re-measured on
this repository with the same harness:

| | Before | After |
|---|---|---|
| Recall | 90.5% | 97.5% |
| Precision (judged edges) | 91.6% | 95.1% |
| "unique repo-wide" edges refuted by the tests | 8 | 0 |
| Calls the graph knew were ambiguous | 20 | 4 |

The "unique repo-wide method match" tier is still weak (3 of 14 judged edges correct, 11 refuted) and is
the next thing to tighten. One repository, and the harness counted 280 runtime edges after versus 264
before, so part of the recall change may be the harness seeing more, not the graph.

### The unique-method tier

21 of its 28 edges were sqlite `db.execute()` / `cur.execute()` calls sent to an unrelated class's `execute`,
because it was the only method of that name in the repo. The tier now also needs the receiver's name to point at
the class (`circuit_breaker` -> `CircuitBreaker`); `self` / `cls` still qualify. Calls on `_Visitor`-style
(underscore-prefixed) classes are now typed from their own assignment.

| | Before | After |
|---|---|---|
| Precision (judged edges) | 95.1% | 98.9% |
| Recall | 97.5% | 96.8% |
| Unique-method edges refuted | 11 | 0 (6 edges left, none judged) |

The cost is real: calls such as `_get().ask(...)`, where the receiver is a call result, were right and are now
listed as "unconfirmed" instead of linked. Fewer, more trustworthy edges is the intended trade.
