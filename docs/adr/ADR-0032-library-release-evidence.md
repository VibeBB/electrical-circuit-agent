# ADR-0032: Golden-Corpus, Mutation, and Escape-Rate Release Evidence

## Status

Accepted

## Context

Package geometry, pin numbering, view orientation, and 3D-model alignment are
independent facts that can be wrong in library artifacts. A passing example or
a synthetic verifier callback does not establish that the production
verification path detects such defects. Review relaxation therefore needs
auditable corpus results, real deterministic-oracle mutation evidence, and an
escape-rate bound tied to the inputs that produced it.

The PartSpec schema has no unit field. It cannot express a unit-conversion
metamorphic relation without inventing unsupported state.

## Decision

### Sealed golden corpus

- Keep the versioned corpus manifest and truth files under `library/corpus/`;
  a project-local `library/corpus/corpus.json` is preferred when present.
- Bind truth to exact datasheet hashes and require a separate, hash-bound human
  approval before treating truth as confirmed.
- Keep unconfirmed entries useful for availability and artifact-integrity
  checks, but never count them as correctness passes.
- Prevent authoring lanes from accessing corpus truth. The scorer reports
  missing PDFs, hash mismatches, and unconfirmed truth as unavailable or
  failing evidence rather than passing them.

### Seeded critical mutations

- Apply deterministic, seeded mutations to symbols, footprints, PartSpecs,
  and generated STEP models and run them through the actual verification
  stack.
- A critical mutation must be detected by at least two independent counting
  oracle families: evidence, pin bijection, orientation, land geometry, export,
  model geometry, or rule profile.
- The report's `counting_family_count` includes only those seven families.
- Report vision and integrity findings separately, but never count either
  family toward the two-family minimum. Integrity findings identify changed or
  missing hashes, seals, manifests, approvals, lineage, or stale records; they
  do not establish that consistently bound content agrees with its evidence.
- Preserve `single_oracle` as a failing status and report it in the mutation
  summary. No review, metric, or release option can weaken the two-family
  requirement.
- For 3D changes, verify mutated STEP against the correct footprint; for
  footprint changes, verify against a model generated from the correct
  PartSpec. Model geometry and KiCad STEP export are independent families
  wherever each applies.

### Metamorphic relations

Keep deterministic checks for PDF DPI invariance, rotations, bottom-view
mirroring, and export invariants. Skip unit conversion explicitly: PartSpec
does not contain a unit field, so there is no valid unit mutation to apply.
Adding such a relation requires a schema-level unit representation and its
own semantics first.

### Independent pin sources

Class A is the PartSpec pin table bound to extracted drawing evidence. Class B
is an independent machine-readable source, such as IBIS `[Pin]` or BSDL
`PIN_MAP_STRING`. Compare the sources and report disagreement; do not silently
merge or promote either source. A missing Class B remains an explicit
single-source condition.

### Escape-rate release criterion

An escape is a critical human correction to pin map, pin-1, view, pad geometry,
body/pitch, or model orientation on a part whose automated PartSpec and
library-verification gates had both passed before correction. Count accepted
human-confirmed parts as trials and corrected accepted parts as escapes.
Compute the exact one-sided 95% Clopper–Pearson upper bound by bisection on
the binomial CDF in pure Python.

Human review may be relaxed only when all of these conditions hold:

1. At least 299 accepted human-confirmed parts are in the sample.
2. The one-sided 95% upper bound is strictly below 0.01.
3. The bound mutation report contains no critical `single_oracle` result.
4. The stored metrics snapshot is bound to the current corpus manifest and
   mutation report SHA-256 values and matches a fresh recomputation.

Until every condition holds, a relaxed review scope is rejected with
`review_relaxation_not_supported_by_metrics`; full review remains the default.
The metrics include sample and escape counts, the confidence bound, family
detection rates, per-operator results, hashes, and fail-closed findings.

## Consequences

- Corpus, mutation, pin-source, and escape-rate evidence become explicit
  artifacts rather than informal claims.
- Synthetic callback tests can validate the gate mechanics but are not
  empirical evidence for operator-to-family coverage.
- A real mutation matrix must be run against deterministic verification before
  claiming the two-family criterion is met. Operators with fewer than two
  counting families remain visible as failures.
- Human review cannot be relaxed on a small sample, a stale metrics file, a
  missing corpus or mutation report, or a critical single-oracle mutation.
