# ADR-0026 Library human-review gate

## Status

Accepted

## Context

Wrong symbols, pin mappings, or footprints can corrupt every downstream
electrical and physical check. Deterministic extraction and verification are
necessary, but their escape rate has not yet been measured on a sufficiently
large population. A human review must therefore remain mandatory for each
project-library part; an automated mismatch-only path is not justified yet.

## Decision

- A review packet is built from freshly checked PartSpec, symbol, footprint,
  model, and PDF bytes. Its 16-hex packet ID is SHA-256 over canonical JSON
  containing those input hashes and the verification settings. Any relevant
  byte or setting change creates a different packet identity and invalidates
  prior approval.
- The packet contains a blind question page and a separate review page. Blind
  questions expose cited drawings and crops, but not the expected answers,
  model confidence, or model agreement. The reviewer submits answers and an
  approve/reject decision as a user event whose first line names the packet ID.
- A hook records a pointer to the user event with its full SHA-256. The gate
  rereads that event, checks its source and hash, parses the decision, and
  accepts only a correct, current-packet approval with all blind answers
  correct and no corrections. Invalid or unreadable evidence fails closed.
- Human approval never overrides a fresh deterministic PartSpec or library
  verification failure. Rejections may carry JSON-pointer corrections; those
  are applied only when the current value matches the stated old value and the
  modified PartSpec validates. Successful corrections are recorded
  idempotently in a correction corpus and are checked for regression.
- The library-protection hook blocks OpenHands writes to the agent-canvas event
  store. This is a policy barrier, not a cryptographic guarantee: arbitrary
  code execution with the same account can forge files or events. The event
  hash detects later changes but does not establish an external identity.
- Three-dimensional visual review is not included yet. Packets report this
  explicitly and do not imply that a model was visually assessed.
- Mismatch-only review remains unimplemented until at least 299 accepted parts
  have been measured with zero escapes. Passing that criterion is a necessary
  measurement threshold, not an automatic authorization to remove review.

## Consequences

Review packets are reproducible and approval is tied to the exact artifacts
that passed deterministic checks. The human remains responsible for examining
the blind evidence before seeing agent comparisons. The residual risks include
human error, forged local event files by arbitrary code, and the absence of a
3D visual check; none are hidden by deterministic verification or presented as
cryptographic attestation.
