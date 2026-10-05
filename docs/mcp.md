# MCP tools

The `circuit` MCP server (`python3 -m circuit.mcp_server`, launched through
`plugins/circuit/scripts/circuit_launcher.py`) exposes 60 tools. Inputs come
from each tool's JSON schema (`*` = required). `ro`/`rw` reflects the
`readOnlyHint` annotation; `IMG` means the tool can return inline
`ImageContent` (member of `IMAGE_TOOLS`, mirrored by the
record-image-observation hook). Errors are returned as tool errors
(`isError`) — missing files, failed validation, and failed subprocesses are
fail-closed.


## Environment & lifecycle

| Tool | Access | Inline image | Purpose | Inputs |
|---|---|---|---|---|
| `circuit_api_server_start` | rw | no | Start KiCad API server | board_path* |
| `circuit_api_server_status` | ro | no | Get API server status |  |
| `circuit_api_server_stop` | rw | no | Stop KiCad API server |  |
| `circuit_doctor` | ro | no | Report circuit environment diagnostics |  |
| `circuit_kicad_version` | ro | no | Get KiCad version |  |
| `circuit_konnect_call` | rw | no | Invoke Konnect operations through a managed stdio session when dynamically loaded toolsets are not visible to the harness; pass ops to run several calls (including load_toolset) in one session | tool, arguments, socket, ops |

## Brief & intake

| Tool | Access | Inline image | Purpose | Inputs |
|---|---|---|---|---|
| `circuit_brief_validate` | ro | no | Validate a machine-readable design brief | brief_path* |
| `circuit_brief_intake_check` | rw | no | Check design brief conversation provenance | brief_path*, intake_path*, output_path |
| `circuit_brief_library_check` | rw | no | Resolve design brief libraries and pins | brief_path*, output_path |

## Authoring gates & checks

| Tool | Access | Inline image | Purpose | Inputs |
|---|---|---|---|---|
| `circuit_netlist_export` | rw | no | Export a KiCad schematic netlist | schematic_path*, output_path |
| `circuit_connectivity_check` | rw | no | Check authoritative schematic connectivity against a design brief | brief_path*, schematic_path*, output_path |
| `circuit_sch_lint` | rw | no | Lint schematic readability (label placement, bounds) | schematic_path*, output_path |
| `circuit_fit_sheet` | rw | no | Clamp out-of-bounds schematic labels back inside the sheet | schematic_path*, margin |
| `circuit_erc` | rw | no | Run KiCad ERC | schematic_path*, output_path |
| `circuit_drc` | rw | no | Run KiCad DRC | board_path*, output_path |
| `circuit_jobset_run` | rw | no | Run the declarative KiCad jobset | project_path*, output_dir*, jobset_path |
| `circuit_connector_placement_check` | rw | no | Check connector board-edge alignment and mating clearance on a PCB | pcb_path*, part_specs*, output_path |
| `circuit_design_report` | rw | no | Build a fail-closed design report from gate reports, exports, renders, jobset and diffs | brief_path*, schematic_path*, board_path*, output_path |

## Interchange

| Tool | Access | Inline image | Purpose | Inputs |
|---|---|---|---|---|
| `circuit_connectivity_export` | rw | no | Emit the wire-agent ConnectivitySource contract (*.connectivity.json) | brief_path*, netlist_path, output_path |
| `circuit_firmware_export` | rw | no | Emit MCU pin connectivity for firmware-agent (*.firmware.json) | brief_path*, netlist_path, output_path |
| `circuit_firmware_check` | rw | no | Check a firmware-agent pin map (*.fw-pinmap.json) against the circuit | brief_path*, pinmap_path*, netlist_path, output_path |

## Import & export

| Tool | Access | Inline image | Purpose | Inputs |
|---|---|---|---|---|
| `circuit_import` | rw | no | Import a non-KiCad schematic or PCB (Altium/Eagle/CADSTAR/EasyEDA(+Pro)/LTspice/PADS/DipTrace/PCAD/OrCAD schematics; PADS/Altium/Eagle/CADSTAR/Fabmaster/PCAD/SolidWorks boards); writes the converted KiCad file plus the importer's JSON report next to it as <output>.import.json | kind*, source_path*, output_path*, format |
| `circuit_export` | rw | no | Export KiCad artifacts | kind*, source_path*, symbol_name, output_dir* |
| `circuit_stackup` | rw | yes | Export the board stackup and write a deterministic section-diagram SVG plus the stackup JSON into output_dir | board_path*, output_dir* |

## Render, diff & rasterize

| Tool | Access | Inline image | Purpose | Inputs |
|---|---|---|---|---|
| `circuit_render` | rw | yes | Render board or schematic views for visual review; board3d writes one PNG/JPEG to output_path, schematic and layers kinds plot PNG pages into output_dir and attach up to 4 images inline | kind, board_path, schematic_path, output_path, output_dir, side, width, height, rotate, zoom, pan, pivot, perspective, floor, background, quality, pages, black_and_white, exclude_drawing_sheet, dpi, layers, common_layers, mirror, scale, sketch_pads_on_fab_layers, sketch_pad_numbers, include_border_title, theme |
| `circuit_diff` | rw | yes | Compare two KiCad schematic or PCB files; png/svg formats produce a visual diff artifact | kind*, left_path*, right_path*, output_path*, format |
| `circuit_rasterize` | rw | yes | Rasterize a .pdf (pdftoppm, one PNG per page) or .svg (rsvg-convert) intake file to PNG for visual review; attaches up to 4 images inline | source_path*, output_dir*, dpi |

## Datasheet & vision

| Tool | Access | Inline image | Purpose | Inputs |
|---|---|---|---|---|
| `circuit_datasheet_extract` | rw | no | Extract PDF datasheets through Poppler, pdfplumber, and OCR as needed | pdf_path*, output_dir, pages, dpi |
| `circuit_datasheet_check_received` | rw | no | Check a received datasheet against a datasheet acquisition request | pdf_path*, request_path* |
| `circuit_datasheet_revision_check` | rw | no | Compare a PartSpec datasheet binding with the current manufacturer source | part_spec_path*, output_path |
| `circuit_vision_read` | rw | yes | Create datasheet image crops for visual reading; every image must receive an answer and a multi-sentence impression describing appearance, legibility, ambiguity, and anything surprising. Use som_tokens to reference numbered mechanical word tokens. | extraction_path*, requests*, out_dir |
| `circuit_vision_answer` | rw | no | Record answers to a tool-managed vision-read batch | batch_path*, answers* |
| `circuit_vision_compare` | rw | yes | Build a hash-bound datasheet/KiCad comparison panel. Every image requires an answer and a multi-sentence impression describing appearance, legibility, ambiguity, and anything surprising. | part_spec_path*, kind*, symbol_lib_path*, symbol_name*, footprint_path*, density, out_dir |
| `circuit_model_compare` | rw | yes | Create a datasheet/model comparison using the existing vision read and answer flow | part_spec_path*, footprint_path*, model_path*, out_dir |

## Library authoring & review

| Tool | Access | Inline image | Purpose | Inputs |
|---|---|---|---|---|
| `circuit_part_author_commit` | rw | no | Seal this lane's PartSpec with a required overall datasheet impression. | run_dir*, part_spec_path*, impression* |
| `circuit_part_author_compare` | rw | no | Re-derive and reveal both sealed authoring lanes; unavailable to lane authors. | run_dir* |
| `circuit_part_spec_check` | rw | no | Cross-check an authored PartSpec against datasheet extraction and evidence | part_spec_path*, output_path |
| `circuit_land_pattern` | rw | no | Compute a land pattern using an optional project rule profile | part_spec_path*, library_dir, rule_profile, density, output_path |
| `circuit_footprint_write` | rw | no | Write a deterministic footprint from a checked PartSpec | part_spec_path*, output_path*, density, rules_path, model_path, ep_paste_margin_mm, name, spec_check_path |
| `circuit_symbol_write` | rw | no | Write a deterministic symbol from a checked PartSpec | part_spec_path*, library_path*, footprint_id*, symbol_name, spec_check_path |
| `circuit_model_generate` | rw | no | Generate a deterministic STEP model and provenance manifest from a PartSpec and footprint | part_spec_path*, footprint_path*, output_dir, output_path |
| `circuit_model_inspect` | rw | no | Inspect a STEP model against its PartSpec and footprint, returning facts and findings | part_spec_path*, footprint_path*, model_path*, tolerance_mm, output_path |
| `circuit_library_candidates` | rw | no | Search installed and project libraries for reusable items, including product-tuned candidates | part_spec_path*, product, density, output_path |
| `circuit_library_import` | rw | no | Import KiCad library items with immutable source copies and provenance | source_path*, library_dir*, nickname*, origin*, vendor*, url, retrieved_at, license*, symbol_names, members, replace |
| `circuit_library_record` | rw | no | Record a generated or derived library artifact and its transformations | library_dir*, artifact_path*, artifact*, name*, transformation*, origin, vendor, url, license, part_spec_path, derived_from |
| `circuit_library_verify` | rw | no | Verify an authored library part against its PartSpec, checks, and provenance | part_spec_path*, symbol_lib_path*, symbol_name*, footprint_path*, library_dir, density, tolerance_mm, model_required, test_board, pin_source_path, pin_sources, rule_profile, output_path |
| `circuit_library_review_packet` | rw | no | Build a fresh, hash-bound human review packet for a library part | part_spec_path*, symbol_lib_path*, symbol_name*, footprint_path*, library_dir*, density, tolerance_mm, model_required, pin_source_path, pin_sources, out_dir, output_path |
| `circuit_library_review_status` | rw | no | Recompute the current packet identity and report its human review state | part_spec_path*, symbol_lib_path*, symbol_name*, footprint_path*, library_dir*, density, tolerance_mm, model_required, pin_source_path, pin_sources, review_scope, output_path |
| `circuit_library_review_apply` | rw | no | Apply corrections from a validated reject event; never creates decision events | part_spec_path*, library_dir*, packet_id*, event_sha12*, output_path |
| `circuit_library_metrics` | rw | no | Compute hash-bound human review escape-rate and mutation metrics for a project | project_path*, output_path |
| `circuit_mutation_report` | rw | no | Run the seeded mutation suite against the real library verification stack | project_path*, spec_path*, spec_check_path, symbol_lib*, symbol_name*, footprint_path*, model_path*, run_export_oracle, density, seed, output_path |
| `circuit_corpus_score` | ro | no | Score a PartSpec, symbol, footprint, and model against the sealed golden corpus | entry_id*, part_spec_path*, symbol_lib_path*, symbol_name*, footprint_path*, model_path*, corpus_root |

## Human requests

| Tool | Access | Inline image | Purpose | Inputs |
|---|---|---|---|---|
| `circuit_human_request_create` | rw | no | Create an immutable, hash-bound HumanRequest and Markdown packet | project_path*, request*, confidential |
| `circuit_human_request_status` | ro | no | Load a HumanRequest and report its trusted user responses | project_path*, request_id* |

## Records (VRP) & liaison (SLP)

| Tool | Access | Inline image | Purpose | Inputs |
|---|---|---|---|---|
| `circuit_record_decision` | rw | no | Record a design decision (VibeBB Record Protocol): first principles, at least two options with pros/cons, the chosen option, a rationale of 200+ chars, evidence paths (hashed) or references, assumptions, unknowns, risks, revisit trigger. Record one for every non-trivial choice without being asked. | id*, stage*, question*, principles*, options*, chosen*, rationale*, assumptions, unknowns, risks*, revisit_when*, decided_by, evidence* |
| `circuit_record_impression` | rw | no | Record the long-form impression that closes a stage (400+ chars, 3+ sentences): what you noticed, what works, what worries you, how a maker or user would read it, what to do next. Binds the stage artifacts by sha256; record it after the final regeneration. | stage*, artifacts*, impression* |
| `circuit_record_vision_review` | rw | no | Record what you thought after looking at an image (400+ char impression plus findings). Bind it to image_path (hashed) or to the source_event_id of an inspect_image_with_vision event. Required for every image you viewed. | image_path, source_event_id, model*, checklist*, findings, impression* |
| `circuit_records_status` | ro | no | Counts of decision / impression / vision-review records and the last Stop-hook verdict listing records this session still owes. |  |
| `circuit_ux_inbox` | ro | no | List UX-creator liaison requests targeting circuit with their state (new, answered, stale, blocked) plus malformed request/response files. |  |
| `circuit_ux_respond` | rw | no | Answer a UX-creator liaison request: writes liaison/<id>.ux-response.json with input hashes, artifact hashes, gate verdicts and VRP record refs. 'done' is refused when a gate verdict is fail/unknown or refs are missing. | request*, status*, reason, artifacts, gate_verdicts, decision_refs, impression_refs, questions_for_user |
