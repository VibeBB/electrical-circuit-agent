# ADR-0035: ISO 7200 drawing sheet from brief data

## Status

Accepted

## Context

Generated schematics used KiCad's default drawing sheet with the title
block fields `title`, `date` (the run date) and `rev` (`1`). The sheet
could not name the approver, the document status, or the brief it was
generated from, and the run date read as an issue date for drawings that
were never approved. wire-agent and mechanical-agent moved their title
blocks to ISO 7200:2004; the family keeps one policy across drawing
producers.

## Decision

- `DesignBrief.drawing` (`DrawingInfo`) carries the ISO 7200 fields a
  brief cannot derive. The document status is derived (`Released`,
  `In approval`, `In preparation`); `date_of_issue` requires
  `approved_by`.
- `circuit.drawing_sheet` emits `<name>.kicad_wks` from a cell table: the
  KiCad default zone frame plus a 180 mm ISO 7200 title block whose
  identification zone (number, revision, issue date, language, sheet) is
  the bottom-right row. The paper size prints in the frame's right strip,
  not the title block.
- Values come from `VIBEBB_*` project text variables in `<name>.kicad_pro`
  (plus KiCad's `TITLE`, `REVISION`, `FILENAME`, `SHEETPATH`, `#`/`##`),
  so the `.kicad_sch` keeps its single writer and a re-run only rewrites
  the project. Only `page_layout_descr_file` and `VIBEBB_*` variables are
  touched; other project settings survive.
- The schematic's own `title_block` keeps `title`, `rev`, `company`
  (legal owner) and `date` (issue date, empty until released) so tools
  that ignore the custom sheet still read the same facts.
- No producer logo or VibeBB mark is drawn; the `Generator` cell names
  the tool.

## Consequences

`title_block_incomplete` no longer requires a date. Exports and renders
pick up the sheet through the project file; schematics opened without
their project fall back to KiCad's default sheet.
