# ADR-0033: Human Requests and Confidential Datasheets

## Status

Accepted

## Context

Datasheet acquisition can fail because a source is unavailable, access is
restricted, or the available document does not match the requested part,
revision, or evidence sections. A user-provided PDF must be bound to the
response that supplied it and checked before it can support PartSpec evidence.
Some datasheets and the resulting review artifacts are confidential and must
not be placed in ordinary project storage or sent to web tools.

## Decision

- Represent acquisition requests with the shared, hash-bound HumanRequest
  protocol. A `provided` response must attach a PDF to the same user message
  and state `confidential: yes` or `confidential: no`. An `unavailable`
  response must include a reason and can be routed to substitute-permission or
  alternative-evidence handling.
- Intake accepts PDF data URLs with `%PDF-` magic. The manifest records
  `origin: user_provided`, the source event hash, the HumanRequest ID when
  present, and the confidentiality flag. Intake remains best-effort and always
  exits successfully so unavailable event stores do not block the session.
- Before use, `datasheet.check_received` checks target MPN and ordering
  evidence in both Poppler and pdfplumber lanes, the requested revision when
  present, and each required section in both lanes. Any mismatch is an error;
  the PDF must not proceed as the requested datasheet.
- Confidential PDFs, their extracted page images, crops, review packets, and
  HumanRequest artifacts are stored below `<project>/.confidential/`. The
  store receives a generated `.gitignore`; verification reports
  `confidential_artifact_outside_store` when a confidential datasheet or
  extraction is outside it. Confidential review HTML and request Markdown
  carry a `CONFIDENTIAL — local only` banner.
- The protection hook blocks terminal Git add/commit/push operations and web
  or fetch tool inputs that reference `.confidential` or a manifest-listed
  confidential path.
- Project-owned library authoring is orchestrated by the `circuit-library`
  agent and its ordered authoring skill. It delegates independent PartSpec
  derivation to lanes A and B, compares only after both lanes commit, and
  requires a fresh deterministic library verification and hash-bound human
  review before completion. The authoring context must not read the golden
  corpus or `.vision-control` sidecars.
- The authoring flow stops and creates a HumanRequest for an unavailable or
  mismatched datasheet, required substitute permission or alternative
  evidence, `model_terminals_unseparable`, or lane disagreement that source
  evidence cannot resolve. Requests preserve hashes, known facts, unknowns,
  assessment, recommendation, and alternatives with risks. A missing, denied,
  mismatched, or stale response does not authorize progress. Substitute
  permission and alternative evidence do not downgrade deterministic
  contradictions.

## Consequences

Acquisition is a human-assisted evidence step, not an approval to use a
mismatched document. Keyword checks are deterministic and fail closed, but do
not replace deeper PartSpec re-derivation or visual review. HumanRequests make
missing evidence and decisions explicit, while the dual authoring and
hash-bound approval gates keep unresolved uncertainty visible. The protection
hook and `.gitignore` are policy safeguards, not a security boundary against
arbitrary code execution under the same account.
