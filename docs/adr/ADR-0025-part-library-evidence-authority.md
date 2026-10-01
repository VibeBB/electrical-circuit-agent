# ADR-0025: Part library evidence authority

- Status: Accepted
- Date: 2026-10-01

## Context

The PartSpec gate previously trusted agent-writable extraction JSON and could
accept a token found anywhere on a page. Pin numbers and names were not bound
to table cells, package variants could be mixed across drawings, and a user
confirmation could downgrade deterministic contradictions. These conditions
made a passing report insufficient evidence that the authored component
matched its datasheet.

## Decision

Evidence authority follows this lattice:

1. A deterministic contradiction blocks acceptance and is never overridden.
2. Deterministic support from bound, reproducible evidence is the only
   acceptance basis.
3. Blind human entry may be accepted only through the separate hash-bound
   record planned for PR-B.
4. Diverse model reads may cause abstention, but may not establish acceptance.
5. Critic findings may trigger re-checks, but may not establish acceptance or
   override a contradiction.

Every PartSpec check re-derives cited pages from the datasheet PDF bytes into a
fresh temporary extraction directory. Stored extraction artifacts are evidence
for provenance checks only: cited PNG hashes are compared with the fresh
render, and missing or mismatched stored evidence is reported. Re-derivation
failures fail closed. Mechanical support must be present in both independent
text lanes, Poppler and pdfplumber, inside the bound bbox or table cell.
Supporting words must also have sufficient ink in the re-derived page image;
invisible text does not count. Dimension readings use tight bboxes and exact
numeric-token multisets, with stacked-limit ordering checked explicitly.

Where a datasheet supplies tables, readings bind to cells. Pin-table checks
validate the number/name bijection, exposed-pad row, numbering, and column
identity. Orderable rows bind exact MPN, package designator, and pin count.
Drawing dimensions, pin-1 readings, and land patterns bind to a visible drawing
identifier and, when supplied, revision. Project-library verification records
its inputs and is re-run from those inputs by the library gate; a stored pass
verdict alone is never trusted.

The mechanical PDF lanes are Poppler and pdfplumber, with pdfplumber
(`MIT`) adopted for table geometry, words, and vector counts. Docling is not
used as a drawing lane because it drops dimensions rendered as images and has
approximately 1.6 GB RAM requirements; it remains a possible future table
lane. PyMuPDF is not imported because it is AGPL-licensed; if needed, it may
only be invoked as a separate `mutool` subprocess. pypdfium2 remains a possible
future second vector lane.

## Consequences

PartSpec evidence must be reproducible from the referenced PDF, and extraction
JSON edits cannot change mechanical acceptance. Authoring requires explicit
cell bindings and package drawing identifiers. Missing, ambiguous, invisible,
or one-lane-only evidence fails closed or produces a documented abstention
finding rather than a pass.
