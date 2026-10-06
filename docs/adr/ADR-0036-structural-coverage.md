# ADR-0036: Structural coverage gate (C0, C1, C2, MC/DC, boundaries)

Status: Accepted

## Context

CI measured statement coverage only (coverage.py `branch` disabled).
Statement and branch coverage cannot show whether each condition of a gate
decision was exercised independently or whether numeric limits were tested
at their boundaries, which is where the deterministic gates decide pass or
fail. Python has no mature tool for condition, MC/DC or boundary coverage:
`pymcdc` and `mcdc-coverage` have little use and the latter is not
published on PyPI.

## Decision

- Turn on coverage.py branch measurement; `fail_under` becomes the combined
  statement+branch percentage measured on main minus one point.
- Add the family-canonical `scripts/structural_coverage.py` (stdlib only, no
  new dependency). It instruments the source AST in memory during the normal
  pytest run and measures decision, C2, MCC, MC/DC (unique-cause with
  short-circuit don't-cares, as GCC and Clang define it for C) and 3-value
  boundary coverage, and reads C0/C1 from coverage.py's JSON.
- Gate six criteria against ratchet floors in `[tool.vibebb-coverage]`;
  MCC is reported only. An unmeasured criterion fails.
- CI's pytest step runs through the script, so the required `verify` checks
  enforce the floors.

See [test-coverage.md](../test-coverage.md).

## Consequences

- Gaps are listed with the evaluation that would close them, which turns
  coverage into concrete test cases.
- pytest still runs once; instrumentation adds a few seconds.
- Code executed only in subprocesses is not observed (same as coverage.py).
- The script and its test are byte-identical across the family; change all
  11 copies together and update the pinned sha256.
