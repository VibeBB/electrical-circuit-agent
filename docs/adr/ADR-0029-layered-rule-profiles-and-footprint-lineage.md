# ADR-0029 Layered rule profiles and footprint lineage

## Status

Accepted

## Context

IPC and manufacturer geometry provide useful starting points, while
organization- and product-level production experience can justify documented
changes to footprints. Those changes must remain reproducible and must not
weaken electrical, pinout, or manufacturability invariants.

## Decision

- Manufacturing rules are resolved through a hash-bound chain:
  `standard < manufacturer < organization < product`. Built-in IPC-7351B and
  KiCad footprint-generator profiles are code-defined roots; user profiles
  cannot shadow them. Each child binds its parent digest, and evidence paths
  are project-relative and checked against their recorded SHA-256 on every
  load.
- A profile overrides only non-null fields from its parent. The effective
  chain and its digest are emitted with generated land patterns. Explicit
  fabrication and placement tolerances take precedence over profile values.
  Profile clearances configure the existing pad and exposed-pad checks.
- Organization and product profiles require a rationale and at least one
  hash-verified evidence reference. Tolerances and clearances must be finite
  and within their permitted ranges.
- A footprint lineage sidecar binds the current footprint, its base artifact,
  rule chain, and evidence. The verifier deterministically recomputes recorded
  pad changes with a `1e-4 mm` numeric tolerance and reports both unrecorded
  and stale changes.
- A fully covered geometry deviation is reported as `intentional_tuning`
  instead of `pad_geometry`, with deltas from both the generated reference
  and the lineage base. Tuning never relaxes pad-set, pinout, pin-1, lead
  containment, clearance, courtyard, or silk invariants.
- Review packets bind lineage and rule-chain hashes and show base-to-current
  changes, rationale, evidence links, and intentional deviations.
- Functional candidate checks share the same non-configurable invariants as
  verification. Candidate ranking is `exact`, `compatible`, `functional`,
  then `near`; within a class, organization artifacts rank above project,
  manufacturer, and official artifacts.
- The rule-profile option is exposed through the existing MCP land-pattern
  and verification tools and the Python API. No new CLI commands are added.

## Consequences

Rule selection, footprint changes, and manufacturing evidence are auditable
without allowing a profile or recorded tuning to suppress functional
failures. Organization knowledge is reusable while product-specific
deviations remain tied to explicit evidence.
