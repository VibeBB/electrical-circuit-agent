# ADR-0018 Structured visual review records and image-observation provenance

- Status: Accepted
- Date: 2026-09-23

## Decision

Advisory visual review gets a typed, auditable record contract plus full
provenance coverage of what the model actually saw.

`src/circuit/advisory.py` defines `VisualReviewDetail` — the `detail`
payload for any `AdvisoryResult` with `tool: "vision_review"`:
`{image_path, image_sha256, model, checklist, impression, findings[]}`.
`checklist`
names the artifact kind (`board_top`, `board_bottom`, `board_side`,
`board_isometric`, `board_layers`, `schematic`, `footprint`),
`impression` is a required free-text field holding the reviewer's
subjective reading of the drawing (a record without one is discarded),
and each
finding is `{category, severity (error|warning|info), note, bbox?}` with
`category` drawn from a fixed vocabulary spanning the per-artifact
checklists (`silkscreen_overlap`, `component_overhang`,
`mounting_hole_collision`, `height_collision`, `label_readability`,
plus the drawing-quality set `ambiguous_notation`, `missing_dimension`,
`missing_manufacturing_info`, `design_intent`,
`datasheet_mismatch`, … `other`). `circuit-review` reviews every sheet
on baseline fidelity, manufacturing completeness, and design intent
(functional grouping, signal flow, power/ground topology, silkscreen
as assembly instruction). `bbox` is an optional normalized
`[x, y, w, h]` region. `circuit-review` documents which checklist applies
to which render kind, including the part-review loop (`create_footprint`
/`edit_footprint_pad`/`set_footprint_graphics` → `fp_svg` or render →
compare pad count/pitch/numbering against a materialized datasheet image).
A `parse_visual_review` helper type-checks a record's detail for
cross-checking and tests.

A sibling `post_tool_use` hook, `record_image_observation`, closes the
provenance gap (G9 in the plan): previously only
`inspect_image_with_vision` calls were logged. Now `circuit_render`,
`circuit_diff`, and `file_editor view` executions also append
`{sequence, event_id, tool_name, image_path, image_sha256, recorded_at,
session_id}` to `observations/circuit/image-observations.jsonl` (override
`$CIRCUIT_IMAGE_OBSERVATIONS`) — every image the model saw has a record,
not just delegated vision calls. Image paths are extracted from the tool
result (any string containing a `.png`/`.jpg`/`.jpeg` path that resolves
to an existing file) or, for `file_editor`, from `command == "view"` on an
image path. Error responses and missing files log nothing; the hook always
exits 0.

Records stay strictly advisory: `vision_review` findings never promote to
connectivity/ERC/DRC verdicts (ADR-0011/0013), and `stage` remains
`"review"` inside the existing `AdvisoryResult` envelope so
`circuit_design_report` aggregation is unchanged.

## Rationale

Free-text vision observations in `review-*.advisory.json` files were
unauditable — no way to say which image, which model, which checklist
produced a finding, or to dedupe/diff findings across revisions. A fixed
finding-category vocabulary makes findings machine-comparable while the
`checklist` field binds each review to the artifact type it judged. And
without logging *direct* image observations (renders inline in tool
results, `file_editor view`), the provenance trail had a hole exactly
where the vision lane is busiest — a `vision_review` record could claim
an `image_sha256` nothing verified. The observation hook means every
image the model saw is independently listed, so review records can be
cross-checked against what actually entered the context.

## Consequences

- `src/circuit/advisory.py` adds `VisualChecklist`,
  `VisualFindingCategory`, `VisualFinding`, `VisualReviewDetail`,
  `VISION_REVIEW_TOOL`, and `parse_visual_review`.
- `plugins/circuit/hooks/scripts/record_image_observation.py` runs on
  `post_tool_use` matcher `circuit_render|circuit_diff|file_editor`.
- `circuit-review` and `circuit-verification` document the per-artifact
  checklists and the `review-visual-<slug>.advisory.json` convention.
- Two observation logs now exist side by side:
  `vision-tool-events.jsonl` (delegated `inspect_image_with_vision`
  calls: profile, model, question, response hash) and
  `image-observations.jsonl` (direct image sightings: path + sha256).
