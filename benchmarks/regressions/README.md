# Regression replay: would the PR check have pointed at what broke?

`replay.py` rebuilds the graph at a culprit commit, runs the PR check on the culprit's own diff, and
asks where the functions the later *fix* had to edit sit relative to what the check reported.
See the docstring in `replay.py` for the verdict definitions.

## vLLM (2026-11, heuristic provider, `vllm/` + `tests/`)

Pairs: every commit whose subject says it fixes something "after / broken by / introduced by /
caused by #N", minus docs-only fixes. Not selected by outcome. 21 pairs, 18 scorable.

| Verdict | Pairs | Meaning |
|---|---|---|
| dependent | 3 | The fix landed in a caller of a changed function. The check listed it; the diff did not. |
| possible | 2 | The fixed function was only among the "unconfirmed callers". |
| same | 9 | The fix edited a function the culprit itself changed. No credit: the diff shows it. |
| missed | 4 | The check gave no pointer to where it broke. |
| n/a | 3 | Culprit or fix had no Python function change (C++/CUDA kernels, config). |

**Real hits: 3 of 18 (17%).** Best case: #57053 changed the GPU model runner and updated five test
fakes but not `test_mixed_warmup_gate`; the check listed that test as upstream, and #60116 later fixed it.

What the run shows about the product:

1. **The "unconfirmed callers" list is unusable at this scale.** It reached 61,063 entries for one PR and
   33,986 for another (common method names such as `forward`, `load_weights`, `__init__`). The two
   "possible" verdicts are therefore not pointers a reviewer could act on. This is the first thing to fix.
2. **Half of regressions break inside the changed function.** A call graph adds nothing there beyond the
   risk label. Value has to come from the other half.
3. **Misses are mostly tests and config.** Three of four misses were tests that construct fakes or mocks
   of the changed code rather than calling it; the fourth was a changed default value read elsewhere
   (data flow, not a call).
4. **Large PRs drown the signal.** #41184 changed 274 functions with 439 upstream; a hit inside a
   700-row list is not a catch.
5. Cost: 45 to 280 seconds per snapshot (13k to 17k+ functions), single process.

| Culprit | Fix | Verdict | Changed fns | Upstream fns | Unconfirmed |
|---|---|---|---|---|---|
| #41184 | #47031 | same | 274 | 439 | 61063 |
| #41184 | #45037 | same | 274 | 439 | 61063 |
| #41184 | #45054 | dependent | 274 | 439 | 61063 |
| #36487 | #38970 | same | 15 | 0 | 2072 |
| #32993 | #34543 | same | 5 | 242 | 0 |
| #25954 | #33046 | dependent | 48 | 100 | 5409 |
| #29546 | #30267 | same | 3 | 4 | 0 |
| #28549 | #28819 | same | 18 | 3 | 399 |
| #28603 | #28816 | same | 1 | 0 | 1 |
| #24642 | #26123 | same | 2 | 1 | 2 |
| #14274 | #14459 | missed | 8 | 91 | 855 |
| #12405 | #12434 | n/a | 0 | 0 | 0 |
| #32723 | #38576 | n/a | 5 | 1 | 9 |
| #57797 | #61678 | possible | 49 | 24 | 33986 |
| #57053 | #60116 | dependent | 28 | 41 | 3217 |
| #58648 | #59508 | missed | 16 | 20 | 12996 |
| #57405 | #57641 | possible | 15 | 7 | 516 |
| #38139 | #38495 | missed | 12 | 18 | 187 |
| #32810 | #50322 | missed | 10 | 9 | 0 |
| #23791 | #23847 | n/a | 3 | 0 | 0 |
| #16457 | #17511 | same | 3 | 2 | 9 |

Raw output: `results/vllm.json`. Reproduce:

    python benchmarks/regressions/replay.py <vllm checkout> --pairs benchmarks/regressions/vllm_pairs.json --out results.json

Caveats: 18 pairs from one repository; "fix location" is a proxy for "where it broke"; a `dependent`
verdict means the function was in the reported set, not that a reviewer would have noticed it.

## After the unconfirmed-callers cut-off

Finding 1 was acted on: an unresolved call now counts as an unconfirmed caller only when at most 5
functions share the name (`storage.MAX_NAME_CANDIDATES`); commoner names are reported as "name shared
by N" instead. Same 21 pairs re-run, raw results in `results/vllm_after.json`.

| | Before | After |
|---|---|---|
| dependent (real hits) | 3 | 3 |
| possible | 2 | 0 |
| same | 9 | 9 |
| file | 0 | 1 |
| missed | 4 | 5 |
| Largest unconfirmed list for one PR | 61,063 | 193 |
| Unconfirmed, #57797 | 33,986 | 0 |
| Unconfirmed, #25954 | 5,409 | 30 |
| Unconfirmed, #57053 | 3,217 | 6 |

The headline number does not move: 3 real hits of 18 scorable. The two "possible" verdicts are gone
(#57797 -> #61678 is now a miss, #57405 -> #57641 a same-file match), which confirms they were matches
on common names, not pointers. The change removes noise; it does not find more regressions.

## After typing local variables

Four of the five remaining misses were test-side fixes. In #38139 -> #38495 the test does
`pooler = SPLADESparsePooler(...)` then `pooler(...)`, and the graph had no edge from that call to the class.
The resolver now types a local variable assigned from a constructor, so `x.method()` and `x(...)`
(`__call__`/`forward`) resolve to that class. Results in `results/vllm_after_typed.json`.

| | Before | After |
|---|---|---|
| dependent (real hits) | 3 | 4 |
| missed | 5 | 4 |

Real hits are now 4 of 18 scorable (22%). Only #38139 -> #38495 changed verdict; upstream sets moved by
at most 2 functions elsewhere. The other four misses are unchanged: #14274 is a changed config default
(data flow, not a call), and the rest are test fixes that edit mock setup or fixtures, not calls.
One repository, so treat the gain as one case, not a rate.
