# Records and vision (VRP for circuit)

The VibeBB Record Protocol v2 makes design reasoning auditable: append-only
JSONL under `observations/circuit/`, written by `src/circuit/records.py`
(MCP tools `circuit_record_*`, `circuit_records_status`, or
`circuit_launcher.py record decision|impression|vision-review|reconcile|insight|song-receipt|read|status|digest|search`),
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

## review-record and the VRP vision log

`circuit_launcher.py review-record` writes only the typed `vision_review`
advisory. It no longer mirrors into `vision-reviews.jsonl`: a VRP v2 vision
review needs facets, grounded claims with a lookback and a reviewer role
that the advisory does not carry, so the agent records it separately with
`circuit_record_vision_review`.

## VRP v2 logs

`observations/circuit/` holds `decisions.jsonl`, `impressions.jsonl`,
`vision-reviews.jsonl`, `reconciles.jsonl`, `insights.jsonl`, `reads.jsonl`
and `song-receipts.jsonl`. Every line carries `schema_version` 2, `sequence`,
`prev_event_id`, `writer` and `event_id` (the SHA-256 of the canonical
record). The writer verifies the chain before appending and refuses a log
that was edited, truncated or reordered; readers report such a log under
`integrity_problems` and never resolve references into it. A log written
entirely by VRP v1 is renamed to `<name>.v1.jsonl` on the first v2 write.

Impressions carry facets (`observed`, `works`, `concerns`, maker/user
`feelings`, `next_actions`), two or more claims quoting exact tokens from the
bound artifacts, and a confidence; a near copy of an earlier impression is
refused. `circuit_records_digest` lists open sister concerns, disputes,
unanswered reads, insights, split reviews and undelivered songs;
`circuit_records_search` filters every plugin's impressions. Answer each
sister impression acted on in `upstream` (`adopted`, `deferred`, `disputed`
or `noted`) or with `circuit_record_read`. A disputed concern cannot be
raised again while the artifact bytes are unchanged, and a rejected insight
returns only with `revisits` and a reason (`circuit_record_insight`).

## Blind second review of renders

`dual_review_globs` (`**/circuit-reports/*.png`) make every schematic page
plot, board view, side elevation and layer plot a dual-review image. After
the primary review, the `circuit-blind-review` sub-agent records a `blind`
review behind the `blind-guard` hook, which hides earlier reviews, records,
advisory reports, design reports, liaison answers and bard songs.
`circuit_record_reconcile` compares `[primary, blind]`; a split gets one
`tiebreak` round (at most two rounds) and stays `unknown` when unresolved.
The Stop hook owes the blind review and the reconcile until both exist for
the current image bytes. A blind review can only push toward stopping; it
never changes an ERC, DRC or gate verdict.

## Songs from bard

bard may deliver `liaison/<id>.bard-song.json` addressed to `circuit`.
circuit answers with `circuit_record_song_receipt`, once per delivery; the
receipt binds the song bytes by SHA-256, is advisory, never feeds a gate and
never asks bard for another song, so the exchange cannot loop. The
`protect-libraries` hook denies hand edits to song deliveries.
