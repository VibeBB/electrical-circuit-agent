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

When the library report fails with `authoring_requests`, do not substitute a similar
library item. Return each request in the intake as a `Q*` open question, retaining its
references, requested identifiers, and reason.

## Wire harness contract fields

Parts with `connector: true` become harness connectors for wire-agent. They can
carry `housing` plus `rated_current_a`/`rated_voltage_v` (omitted values fall
back to wire's defaults of 3 A / 250 V). Nets can declare `signal_class`
(power|ground|signal|analog|data|highspeed|shield — inferred from the name when
absent), `voltage_v`, and `current_a`. Emit the contract with
`circuit_connectivity_export` (or
`python3 "$CIRCUIT_PLUGIN/scripts/circuit_launcher.py" connectivity
--brief <brief.json> --out <name>.connectivity-source.json`); it writes the
wire `ConnectivitySource` schema and fails closed when a connector has no pins
in the brief (or in `--netlist <path>` when given).

## Drawing title-block fields

The optional `drawing` block carries the ISO 7200 title-block data a brief
cannot derive (ADR-0035): `legal_owner`, `identification_prefix`, `revision`,
`responsible_dept`, `technical_reference`, `created_by`, `approved_by`,
`date_of_issue`, `supplementary_title`, `classification`, `language`. Fill in
only what the user stated. The legal owner is the user's organisation, never
VibeBB. Never invent an approver or an issue date: `date_of_issue` without
`approved_by` is rejected, and the printed document status (`In preparation`,
`In approval`, `Released`) is derived from those two fields.
