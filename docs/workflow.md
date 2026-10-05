# Workflow

The `/circuit:design` command orchestrates the stages below through SDK
`TaskToolSet` delegation. `circuit_ux_inbox` is checked at session start and at
every stage boundary; JSON verdicts — never sub-agent opinion — decide
pass/fail.

## Intake & brief (circuit-brief)

- **What happens**: conversational requirements become `<name>.brief.json`
  (parts, nets, board, optional placement) and `<name>.intake.json`
  (per-requirement provenance; unspoken items become `A*` assumptions or `Q*`
  open questions). User-attached images are materialized to
  `intake/attachments/` by the intake-attachments hook.
- **Tools**: `circuit_brief_validate`, `circuit_brief_intake_check`,
  `circuit_brief_library_check`; Konnect `get_symbol_info` / library search.
- **Gates**: a blocked intake or failed library report stops delegation;
  `authoring_requests` become `Q*` questions, never substitutions.
- **Records left**: decisions for part/package choices and power/net topology;
  a stage impression bound to brief + intake files; vision reviews for any
  intake photos/sketches looked at (checklist `intake_image`).
- **Vision points**: attached photos and sketches via inline image or
  `inspect_image_with_vision`, then `circuit_record_vision_review` and a
  `review-record` advisory.
- **Liaison**: `circuit_ux_inbox` at start; `circuit_ux_respond`
  accepted/in_progress immediately, done/needs_info/rejected/deferred at end.

## Library (circuit-library + part-author lanes a/b)

- **What happens**: datasheet-backed PartSpecs are authored by two blind lanes
  (`CIRCUIT_AUTHORING_LANE=a|b`, sealed via `circuit_part_author_commit`),
  revealed by `circuit_part_author_compare`, verified deterministically, and
  packaged as a hash-bound review packet for human approval.
- **Tools**: `circuit_datasheet_extract`, `circuit_vision_read`,
  `circuit_vision_compare`, `circuit_part_spec_check`, `circuit_land_pattern`,
  `circuit_footprint_write`, `circuit_symbol_write`, `circuit_model_generate`,
  `circuit_model_inspect`, `circuit_library_verify`, `circuit_library_review_*`,
  `circuit_human_request_create`.
- **Gates**: deterministic library verification plus human approval of the
  review packet; missing/mismatched evidence stops for a HumanRequest.
- **Records left**: comparison decision + vision reviews recorded by the
  orchestrating library agent; lane authors must NOT record (blind isolation).
- **Vision points**: symbol/footprint/3D renders, datasheet crops, lane
  comparison renders.

## Schematic (circuit-schematic)

- **What happens**: the brief becomes a `.kicad_sch` via Konnect — register
  libraries, look up every symbol, place symbols, wire like a hand-drawn
  schematic (wires + junctions; `batch_connect_to_net` reserved for power
  rails).
- **Tools**: Konnect authoring ops; `circuit_sch_lint`, `circuit_fit_sheet`,
  `circuit_netlist_export`, `circuit_connectivity_check`, `circuit_erc`,
  `circuit_render` (page plots).
- **Gates**: `circuit_sch_lint` readability, connectivity check against the
  brief, ERC JSON from `kicad-cli`.
- **Records left**: decisions for net naming and label strategy; stage
  impression bound to the schematic; vision review of the post-lint page plot.

## Layout (circuit-layout)

- **What happens**: board update, placement, and routing through the
  api-server/`kicad-cli` IPC; stackup and connector placement verified.
- **Tools**: `circuit_drc`, `circuit_stackup`, `circuit_render` (top/bottom/
  side/isometric, layers), `circuit_connector_placement_check`.
- **Gates**: DRC JSON from `kicad-cli` — fail-closed.
- **Records left**: decisions for stackup/rule profile and placement/routing
  strategy; DRC waiver refusals recorded explicitly; stage impression bound
  to the board.
- **Vision points**: top/bottom/side/isometric views, layer plots, the
  stackup PNG after DRC.

## Review (circuit-review)

- **What happens**: independent reading of connectivity/ERC/DRC JSON plus
  `circuit_diff` change evidence; advisory Konnect audits recorded but never
  promoted to a verdict.
- **Tools**: `circuit_diff` (json/png/svg), `circuit_render`, advisory Konnect
  audits (`run_design_review`, `audit_power_rails`, `audit_decoupling`,
  `audit_manufacturing`, `validate_for_manufacturing`, `estimate_cost`).
- **Gates**: none of its own — quotes the deterministic verdicts verbatim and
  flags unexecuted checks as fail-closed.
- **Records left**: vision reviews for `circuit_diff` PNGs; stage impression.
- **Vision points**: `circuit_diff` PNGs (checklist `diff`).

## Manufacturing export (circuit-export)

- **What happens**: `circuit_export`/`circuit_jobset_run` produce Gerbers,
  drill, BOM, position files, STEP/PDF; exports are parsed back for
  verification and rendered for human review.
- **Tools**: `circuit_export`, `circuit_jobset_run`, `circuit_design_report`,
  `circuit_rasterize` (fab PDF), `circuit_render` (gerber layer plots).
- **Gates**: jobset-vs-direct ERC/DRC equivalence check; fail-closed export
  parsers.
- **Records left**: stage impression bound to the export tree; vision reviews
  for gerber layer plots and rasterized fab PDF pages.
