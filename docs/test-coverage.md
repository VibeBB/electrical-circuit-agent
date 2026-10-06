# Test coverage and test design

Every VibeBB repository measures the same structural coverage criteria and
uses the same test-design techniques. `scripts/structural_coverage.py` is
family-canonical (byte-identical in all 11 repositories; its sha256 is pinned
by `tests/test_structural_coverage.py`).

## Criteria

| Criterion | Covered when | Measured by | Gated |
| --- | --- | --- | --- |
| C0 statement | every executable statement ran | coverage.py | floor |
| C1 branch | every `if`/`while`/`for`/`try`/`match` arc was taken both ways | coverage.py `branch = true` | floor |
| Decision | every decision evaluated both true and false, including ternaries, comprehension filters, `assert`, `match` guards and `and`/`or` value expressions that coverage.py has no arcs for | structural_coverage | floor |
| C2 condition | every atomic condition (operand of `and`/`or`/`not`) was true once and false once | structural_coverage | floor |
| MCC multiple condition | every feasible short-circuit evaluation vector of a decision was observed | structural_coverage | reported only (grows as 2^n; no standard requires it) |
| MC/DC | for every condition, two observed evaluations differ only in that condition (short-circuited conditions are don't-care) and produce different decision outcomes | structural_coverage | floor |
| 3-value boundary | every numeric `<`, `<=`, `>`, `>=` condition saw operands below, on and above its boundary; integer operands need exactly -1, 0 and +1 | structural_coverage | floor |

C2 does not imply C1 (`a and not b` with `(T,T)` and `(F,F)` covers every
condition both ways but the decision is never true); MC/DC implies decision
and condition coverage. The MC/DC definition is unique-cause with
short-circuit don't-cares, the variant GCC (`-fcondition-coverage`) and Clang
(`-fcoverage-mcdc`) implement for C. Conditions that cannot affect their
decision (e.g. `x or True`) are listed as infeasible and leave the
denominator; rewrite such code instead of testing around it.

Constant tests, `if TYPE_CHECKING:`, `if __name__ == "__main__":` and lines
marked `# pragma: no cover` are not decisions. Code run only in subprocesses
(hook scripts started with `python path/to/hook.py`) is not observed, the
same as coverage.py without subprocess support.

### Relation to safety standards

DO-178C asks statement coverage for Level C, decision coverage for Level B
and MC/DC for Level A software. ISO 26262-6 Table 12 recommends statement
coverage for ASIL A-B, branch coverage for ASIL B-D and MC/DC for ASIL D.
IEC 61508-3 Table B.2 recommends statement, branch and MC/DC coverage with
rising SIL. VibeBB uses these criteria as engineering evidence for its own
deterministic gates; it does not claim certification under any standard.

## Running

```bash
uv run python scripts/structural_coverage.py run --json out/structural-coverage.json
uv run python scripts/structural_coverage.py run -- tests/test_gates.py -n 0
```

`run` wraps pytest (extra pytest arguments follow `--`), measures every
criterion in the same pytest run, prints the table, appends it to
`$GITHUB_STEP_SUMMARY` in CI and exits 1 when any gated criterion is below its
floor or was not measured at all (`unknown` fails). The JSON report lists
per-file tallies and every gap: for an MC/DC gap the evaluation that already
exists (`have`) and the one to add (`add`); for a boundary gap the missing
points. Instrumentation happens in memory (no `.pyc` is written) and keeps
evaluation order, short-circuiting and the values of `and`/`or` expressions.

## Floors

Floors live in `[tool.vibebb-coverage]` in `pyproject.toml` (`c0`, `c1`,
`decision`, `c2`, `mcdc`, `boundary`). They start at the value measured on
`main` minus one point (interpreter-version variance) and only move up: when
a change raises a criterion, raise its floor in the same PR. Never lower a
floor or add `# pragma: no cover` to make a gate pass. `[tool.coverage.report]
fail_under` is the combined statement+branch percentage coverage.py reports
with `branch = true`.

## Test-design techniques

Coverage shows what the tests executed, not whether they check the right
results. Design tests with these techniques first, then use the gap report to
find what they missed.

- **Equivalence partitioning**: split each input into classes that the code
  must treat alike (valid, each invalid kind, empty, missing) and test one
  representative per class.
- **Boundary value analysis**: for each limit test the boundary and its
  neighbours. 2-value BVA tests the boundary and the closest value on the
  other side; 3-value BVA tests both neighbours. Use 3-value BVA for every
  limit a deterministic gate enforces (ratings, clearances, lengths, counts),
  with exact neighbours for integers and the limit itself plus values just
  inside and outside for floats. Name the limit constant in the test instead
  of repeating the literal.
- **Decision tables**: when a verdict depends on several conditions, list the
  combinations and expected verdicts in a `pytest.mark.parametrize` table; an
  MC/DC set needs n+1 rows for n conditions instead of 2^n.
- **State transition testing**: for lifecycles (record chains, liaison
  requests, insights, releases) test every valid transition, every invalid
  transition from every state, and the terminal states.
- **Pairwise testing**: for option matrices too large to enumerate, cover
  every pair of parameter values (`itertools.product` over the pairs that
  matter, or a hand-built orthogonal array).
- **Property-based testing**: for pure functions (parsers, normalizers,
  serializers, hashing) state properties such as round-trip, idempotence and
  monotonicity. Where Hypothesis is a dependency, add boundary values with
  `@example` because random generation rarely hits them.
- **Negative tests**: corrupt the judged input (missing file, malformed JSON,
  wrong hash, unknown tool output) and assert the gate fails closed.
- **Mutation testing**: a mutant (a flipped comparison, a removed condition)
  that survives the suite marks an assertion that is missing even though the
  line is covered. Mutation runs are advisory evidence, not a CI gate.

## Reference suite

`tests/test_gate_boundaries.py` applies these techniques to the planar
geometry kernel that `src/circuit/libverify.py` uses for courtyard
enclosure, keep-out and clearance findings, following the family pattern
set by wire-agent:

- 3-value boundaries for box containment on each side and at the 1e-6 mm
  tolerance, the 1e-9 collinearity tolerance of `_on_segment`, endpoint
  touching in `_segments_intersect`, and the gap between two polygons;
- properties checked on deterministic grids: containment is reflexive and
  transitive, segment intersection is symmetric under argument and
  endpoint swaps, polygon distance is symmetric, non-negative and
  translation invariant, and point-to-segment distance is a lower bound of
  densely sampled points;
- equivalence classes for point-in-polygon (inside, outside, on an edge,
  convex and concave outlines).
