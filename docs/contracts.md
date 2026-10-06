# Contracts

Every JSON file the plugin reads or writes, its producer/consumer, and
strictness. "strict" = pydantic `extra="forbid"` (unknown fields rejected);
"frozen" = a strict mirror exists in a sister repo so the shape cannot grow.

## Design artifacts

| File | Model (`artifact_kind`) | Producer → consumer | Strictness |
|---|---|---|---|
| `*.brief.json` | `DesignBrief` (`circuit_design_brief`) — parts, nets, board, placement, optional `drawing` (ISO 7200 title-block data, ADR-0035), optional `lifetime` (`LifetimeSpec`: per-part `rated_life_h` at `rated_temp_c`, explicit `activation_energy_ev`, mission `profile[]` of `temperature_c`/`fraction` summing to 1, `required_life_h`, mandatory `source`, optional `response_path`), optional `thermal` (`ThermalSpec`, simulation handoff) | circuit-brief → gates, exports, reports | strict |
| `*.intake.json` | `Intake`/`IntakeReport` (`circuit_brief_intake`/`_report`) — per-requirement provenance, `A*`/`Q*` | circuit-brief → orchestrator, intake gate | strict |
| `*.connectivity.json` | ConnectivitySource contract — connectors, nets, cavities | `circuit_connectivity_export` → wire-agent, simulation-agent | frozen: sim `imports.py` mirror is extra=forbid — no VRP fields may be added |
| `*.board-geometry.json` (+ optional `.emn`/`.emp` IDF 3.0) | `BoardGeometry` (`circuit_board_geometry`) — Edge.Cuts outline in a board-centred y-up frame, thickness, mount holes, per-part side/position/courtyard box/verified height and sources, connector edge faces, `unknown`; `src/circuit/board_geometry.py` | `circuit_board_geometry_export` → mechanical-agent | strict; heights need PartSpec or STEP evidence, a bare `circuit_height_mm` property never suffices |
| `*.thermal.sim.json` / `*.thermal.sim-request.json` | simulation-agent brief v1 (`thermal` section: `ambient_c`, per-part `power_w`, `tj_max_c`, `derating_margin_c`, θ path) and v1 `SimulationRequest` (`from_system: circuit`, `kind: thermal`, `request_id` = `<name>-thermal-<sim brief sha256[:12]>`) built from the authored brief `thermal` section (`ThermalSpec`; every part cites a datasheet/measurement `source`, refs must be brief parts; `src/circuit/sim_thermal.py`) | `circuit_sim_thermal_request` → simulation-agent | strict; circuit never judges junction temperature, simulation's gates do |
| `*.thermal.sim-response.json` | simulation-agent `SimulationResponse` v2, mirrored strictly in `sim_thermal.SimResponse` | simulation-agent → `circuit_sim_thermal_check` | strict read, fail-closed: unset/missing/malformed/`needs_info`/`deferred` → `unknown`; request or report hash drift, or a request id / sim brief sha256 that the *current* brief would not emit → `fail`; each `thermal.*` check is reported with simulation's own verdict, measured value and limit (never promoted) |
| `*.lifetime.sim.json` / `*.lifetime.sim-request.json` / `*.lifetime.sim-response.json` | same handoff with `--kind lifetime`: simulation-agent brief v1 `lifetime` section (`model: arrhenius`) and v1 request `kind: lifetime`; response mirrored by `SimResponse` | `circuit_sim_thermal_request`/`_check` with `kind: lifetime` | same strict, fail-closed checks; only simulation `lifetime.*` verdicts are reported |
| `*.firmware.json` | `circuit_firmware_connectivity` — MCU pin map | `circuit_firmware_export` → firmware-agent | frozen: firmware strict mirror — shape must not change |
| `*.fw-pinmap.json` | `firmware_pinmap` | firmware-agent → `circuit_firmware_check` | strict (read only) |
| `*.envelope.json` | EnvelopeSource — mechanical anchors | mechanical-agent → wire/sim; circuit does not write it | frozen upstream by mech/sim mirrors |
| `*-stackup.json` | kicad-cli `pcb export stackup` output | `circuit_stackup` → agent, docs | kicad-cli schema |
| `*.design-report.json` | `DesignReport` — every gate result | `circuit_design_report` → user, sisters | strict |

## Part & library

| File | `artifact_kind` | Notes |
|---|---|---|
| `part.spec.json` | `circuit_part_spec` | Agent-authored PartSpec; datasource-bound, hashable |
| `*.part-spec-check.json` | `circuit_part_spec_check` | Cross-check vs extraction + provenance |
| `*.datasheet-extraction.json` | `circuit_datasheet_extraction` | Dual-lane PDF text/tables/page images |
| `*.land-pattern.json` | computed IPC-7351B pattern | `circuit_land_pattern` |
| `provenance.json` | `circuit_library_provenance` | Per-library source/license manifest |
| `*.library-verification.json` | `circuit_library_verification` | Symbol/footprint/model verification report |
| `*.library-review-packet*` | `circuit_library_review_packet` | Hash-bound human review packet |
| review status/decision | `circuit_library_review_status`, `circuit_library_review_correction` | Approval state + applied corrections |
| `*.library-metrics.json` | `circuit_library_metrics` | Escape-rate/Clopper-Pearson metrics |
| `*.mutation-report.json` | `circuit_mutation_report` | Seeded-mutation oracle results |
| corpus truth | `circuit_golden_corpus`/`_truth` | Sealed golden corpus (`library/corpus`) |
| lineage | `circuit_footprint_lineage` | Hash-bound base→current pad changes |
| pin sources | `circuit_pin_source_comparison` | IBIS/BSDL/vendor pin-source comparison |
| `*.kicad_sym`/`.kicad_mod`/`.step` | KiCad files | Deterministic writers + provenance |

## Advisory, vision & human requests

| File | `artifact_kind` | Notes |
|---|---|---|
| `review-visual-*.advisory.json` | `vision_review` AdvisoryResult — image sha256, model, checklist, findings, impression | `review-record` CLI writes it; mirrored into vision-reviews.jsonl |
| `review-*.advisory.json` | generic AdvisoryResult | Konnect advisory records, never a verdict |
| `*.vision-read.json` | `circuit_vision_read_batch` / `_answers` | Tool-managed crop batches + answers |
| HumanRequest dir | `circuit_human_request` | Immutable hash-bound request + Markdown packet |
| `*.ux-request.json` / `*.ux-response.json` | SLP v2 (`schema_version: 2`, `system: "ux-creator"`) | UX-creator → circuit; response written only by `circuit_ux_respond`; strict (extra=forbid) locally mirrored in `liaison.py` |

## Records (VRP v1)

| File | Producer | Shape |
|---|---|---|
| `observations/circuit/decisions.jsonl` | `circuit_record_decision` / `record decision` | event_id, stage, question, principles, options, chosen, rationale ≥200 chars, evidence sha256, assumptions, unknowns, risks, revisit_when |
| `observations/circuit/impressions.jsonl` | `circuit_record_impression` | event_id, stage, artifact sha256 bindings, impression ≥400 chars / ≥3 sentences |
| `observations/circuit/vision-reviews.jsonl` | `circuit_record_vision_review` / review-record mirror | event_id, image_path sha256 or source_event_id, model, checklist, findings, impression ≥400 chars |
| `observations/circuit/vision-tool-events.jsonl` | record-vision-tool-event hook | inspect_image_with_vision Q&A events |
| `observations/circuit/image-observations.jsonl` | record-image-observation hook | image paths produced/viewed by IMAGE_TOOLS + file_editor |
| `observations/circuit/records-status.json` | require_records.py stop hook | last Stop verdict + owed records |
| `plugins/circuit/hooks/records-policy.json` | repo | plugin, records_dir, artifact_globs, ignore_globs, max_stop_denials: 2, record_hint |

All records files are append-only; direct writes are denied by
protect-libraries and written only through the typed writers or shared hooks.

## `drawing` — ISO 7200 title-block data

`DesignBrief.drawing` (`DrawingInfo`, all fields optional): `legal_owner`,
`identification_prefix` (drawing number; defaults to the brief `name`),
`revision` (default `A`), `responsible_dept`, `technical_reference`,
`created_by`, `approved_by`, `date_of_issue` (ISO date, requires
`approved_by`), `supplementary_title`, `classification`, `language`
(default `en`). The document status is derived: `Released` (approver and
issue date), `In approval` (approver only), otherwise `In preparation`.
Unset fields print `—`. `drawing_sheet.variables` projects these into the
`VIBEBB_*` project text variables the `.kicad_wks` prints; the brief digest
prints as `VIBEBB_BRIEF_SHA256` (first 16 hex digits).
