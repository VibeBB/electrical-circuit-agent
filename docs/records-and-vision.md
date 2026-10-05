# Records and vision (VRP for circuit)

The VibeBB Record Protocol v1 makes design reasoning auditable: append-only
JSONL under `observations/circuit/`, written by `src/circuit/records.py`
(MCP tools `circuit_record_*`, `circuit_records_status`, or
`circuit_launcher.py record decision|impression|vision-review|status`),
enforced by the shared hooks `require_records.py` (session-start hint +
first Stop hook) and `_records.py` (stdlib mirror, canonical across the
family — `scripts/check_shared_hooks.py` pins the AST hashes).

## Stages and typical decisions

| Stage | Typical decisions to record |
|---|---|
| intake/brief | part and package choices, requirement interpretations |
| library | lane comparison outcome, evidence sufficiency, refusal to substitute |
| schematic | net naming, power topology, label-vs-wire connectivity |
| layout | stackup/rule profile, connector placement, routing strategy, DRC waiver refusals |
| review | whether flagged advisory findings warrant action |
| manufacturing export | export set chosen, gerber/drill formats |

Every non-trivial decision → `circuit_record_decision` (first principles,
≥2 options with pros/cons, chosen, rationale ≥200 chars, evidence, assumptions,
unknowns, risks, revisit trigger). End of every stage →
`circuit_record_impression` bound to the stage artifacts (≥400 chars, ≥3
sentences by `sentence_count` — "3.3 V" does not end a sentence). Every image
looked at → `circuit_record_vision_review`. Records are advisory: they never
change ERC/DRC/kicad-cli verdicts and never gate request inputs.

## Artifact globs (records-policy.json)

Impressions must bind real artifacts matching:
`*.brief.json`, `*.intake.json`, `*.kicad_sch`, `*.kicad_pcb`, `*.kicad_pro`,
`*.kicad_sym`, `*.kicad_mod`, `part.spec.json`, `*.design-report.json`,
`*.connectivity.json`, `*.firmware.json`, `*-stackup.json`, `gerbers/**`,
`*-gerbers/**`. Ignored: `examples/`, `tests/`, `.devin/`, `library/corpus/`,
`libraries/`, `observations/`. `max_stop_denials: 2`.

## Vision points

Look at each image via its inline `ImageContent` or `inspect_image_with_vision`,
then record BOTH `circuit_record_vision_review` and a `review-record` advisory
(impression ≥400 chars judging accuracy, ambiguity, design intent, and whether
a maker could build, assemble and debug from it).

| Stage | What to look at | Checklist |
|---|---|---|
| intake | attached photos/sketches | `intake_image` |
| library | symbol/footprint/3D renders, datasheet crops, lane comparison | per kind |
| schematic | page plot after sch_lint | `schematic` |
| layout | top/bottom/side/isometric views, layers, stackup PNG after DRC | board views |
| review | `circuit_diff` PNGs | `diff` |
| manufacturing | gerber layer plots, fab PDF pages via `circuit_rasterize` | `stackup` |

## Inline-image tools (`IMAGE_TOOLS`)

`circuit_render`, `circuit_diff`, `circuit_rasterize`, `circuit_stackup`,
`circuit_vision_read`, `circuit_vision_compare`, `circuit_model_compare`.
The hooks.json post_tool_use matcher and `record_image_observation.py`
`OBSERVED_TOOLS` cover all of them plus `file_editor` (guarded by tests).

## Blind-lane exception

`CIRCUIT_AUTHORING_LANE=a|b` lanes are denied all `observations/circuit/`
paths and the record/ux tools by `guard_author_lane.py` — shared logs would
leak the other lane's or orchestrator's reasoning. Their commit impressions
are enforced by `circuit_part_author_commit` itself; the circuit-library
agent records the comparison decision and vision reviews afterwards.

## review-record mirror

`circuit_launcher.py review-record` writes the `vision_review` advisory, then
mirrors it into `vision-reviews.jsonl` (workspace-relative `image_path`,
`image_sha256`, `checklist` slug with `_`→`-`, findings as `severity` +
`"<category>: <note>"`), and prints the vision `event_id`. If the image is
outside the workspace the mirror is skipped — `"skipped: ..."` appears in the
output so the gap is visible.
