---
name: circuit-brief-rules
description: Path rule — design-brief and intake contract reminders injected whenever a *.brief.json or *.intake.json file is touched.
version: 0.1.0
license: BSD-3-Clause
paths:
  - "**/*.brief.json"
  - "**/*.intake.json"
---

# Circuit brief file rules

- `*.brief.json` follows the `DesignBrief` schema in `src/circuit/brief.py`
  and `*.intake.json` follows `Intake`/`IntakeReport` in
  `src/circuit/intake.py`; check the contract summary in
  `plugins/circuit/skills/circuit-brief/SKILL.md` before editing.
- Every claim in the matching `*.intake.json` binds to a provenance source:
  `R*` user-stated requirement, `A*` assumption with rationale, `Q*` open
  question, `I*` imported source. Details read off images are observations —
  `A*` or `Q*` with an `evidence` ref (`kind`, `path`, `sha256`, `note`),
  never `R*`.
- Generated artifacts (schematics, netlists, ERC/DRC reports, renders,
  exports, design report) are never hand-edited — change the brief and
  re-run the deterministic pipeline; gates stay fail-closed.
