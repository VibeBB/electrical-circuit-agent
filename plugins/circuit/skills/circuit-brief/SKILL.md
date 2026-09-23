---
name: circuit-brief
description: Define and validate a provenance-bound design brief before KiCad authoring.
version: 0.1.0
license: BSD-3-Clause
triggers:
  - design brief
  - 設計ブリーフ
  - requirements intake
  - 要件整理
---

# Circuit design brief and intake

The design-brief sub-agent converts conversation statements into a machine-readable brief
and an intake sidecar. Requirements use `R*` ids, assumptions use `A*` ids, and unresolved
questions use `Q*` ids. Every part and net maps to one or more requirement or assumption
ids. The sidecar binds to the exact brief bytes with `brief_sha256`.

Run the intake gate before authoring. It is `ready` only when the hash matches, all brief
parts and nets are mapped, all source ids exist, and there are no open questions.
Assumption-only mappings are reported for review but do not block authoring.

User-attached images are materialized to `intake/attachments/` by a hook (see
`manifest.jsonl` for sha256 provenance); details read from them belong in `A*`/`Q*`
records, which may declare an `evidence` ref (`kind`, `path`, `sha256`, `note`) —
declared-but-missing or mismatched evidence fails the gate.

Run the library gate before authoring. It parses the installed `.kicad_sym` and `.pretty`
files directly and must be `pass` for every symbol, footprint, and referenced pin. Both
gates are fail-closed and must be recorded as JSON reports.
