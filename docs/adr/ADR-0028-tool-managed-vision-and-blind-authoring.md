# ADR-0028 Tool-managed vision and blind PartSpec authoring

## Status

Accepted

## Context

Mechanical PDF extraction can lose table structure, column association, rotated
labels, or details in drawings. A second read of the same pixels is useful
only when its exact evidence is reproducible and its interpretation remains
distinct from the mechanical extraction. Independent PartSpec authors also
need to remain blind to each other's conclusions until both have committed.

## Decision

- Datasheet visual reads are created by `circuit_vision_read` from
  PDF-SHA-bound, page-bounded crops. Requests use typed, closed Pydantic
  models and batches contain one to seven requested images plus a control
  image. Crops are padded by four points, clamped to the page, rasterized at
  300–1200 DPI, and rejected above the image-size limit rather than tiled.
  The public `batch.json` is written atomically without per-item field or
  control markers; salted field bindings are restored by the tool. Control
  identity and validation secrets are written atomically and exclusively to a
  hash-bound sidecar in `.vision-control/`, rooted at the extraction context
  (or at the authoring run root above lane A/B). Every loader resolves the
  relative sidecar reference and verifies its SHA-256 before restoring the
  salted bindings. The sidecar is written before `batch.json`. Pre-tool guards
  deny every agent tool argument, file read, or terminal command that
  references `.vision-control/` or `.vision-token-map/`; only the MCP server
  reads this private state during normal operation. The vision sidecar binds
  token text and glyph findings by a SHA-256 recorded in the public batch.
  This is context isolation, not a security boundary. Lane B uses
  pdfium; lane A and other lanes use Poppler, configurable through
  `CIRCUIT_PDFTOPPM`. The tool returns prompts, metadata, and image paths, not
  mechanical extraction text.
- Each visual answer, including the batch control, includes an answer and a
  multi-sentence impression. Impressions describe appearance, legibility,
  ambiguity, and surprises. Missing or invalid impressions reject the entire
  submission before any answer is written; successful answers may be
  resubmitted only when the previous submission failed.
- Table reads use the fixed prompt: “Transcribe the table in this image as
  JSON: a list of rows, each row a list of cell strings in left-to-right
  order, including header rows. Use an empty string for an empty cell.”
  Answers are parsed as `list[list[str]]`, normalized with NFKC and collapsed
  whitespace, and invalid shapes are marked `unparseable`.
- Set-of-Mark reads number words extracted by Poppler and pdfplumber, overlay
  those IDs on the crop, and resolve answer IDs back to the bound mechanical
  text through a hash-bound private token-text sidecar outside batches and
  authoring lanes. Unknown IDs and caller-supplied text that differs from the
  bound word are rejected. Ambiguous-glyph checks render both rasterizer lanes
  at 1200 dpi and compare against same-size characters from the PDF's embedded
  font; their findings remain in private state so they do not disclose
  extracted characters to the vision reader. Missing embedded fonts or
  templates produce blocking findings rather than falling back to a system
  font.
- Vision answers are evidence, not replacements for mechanical readings.
  `Reading.vision_read`, `PinoutDrawing.labels_vision_read`,
  `PinTable.vision_read`, and `PartSpec.orderable_vision_read` bind to a batch
  and image hash, the source PDF hash, control result, observation record when
  present, kind, page, and covered bbox. Table reads are cross-checked
  against cited pin or orderable cells. Pinout labels are checked against
  both cited vision labels and freshly derived geometry when available.
- Each authoring lane writes only its own PartSpec and seals it with a
  required overall datasheet impression. The commit binds the spec hash,
  lane, timestamp, profile, model, and vision-batch provenance. A/B use
  independently provisioned profiles and distinct rasterizers; model
  diversity is reported as unknown if either model is unknown, otherwise as
  same or distinct.
- Lane normalization compares authored values at stable JSON pointers while
  excluding evidence references and impressions. Pins are keyed by number,
  orderables by MPN, and pin names use the established `names_equal` rule.
  Impressions are shown verbatim for human context and are never compared or
  scored. Model diversity is `unknown` if either model is unknown; otherwise
  the comparison reports whether the models are distinct or the same. Both
  commits must be present in the observation log when that log exists.
- Library verification freshly compares the sealed lanes and blocks on
  missing, invalid, unobserved, or consensus-violating authoring data.
  Agreed values must match the current PartSpec unless a validated human
  correction records the change. Independent disagreements remain visible
  warnings and are raised as blind questions in the hash-bound review packet.
- Review packets bind the A/B sealed hashes into packet identity, show each
  visual impression next to its answer, and render both author impressions
  verbatim with HTML escaping.
- Footprint and symbol comparisons bind the PartSpec and current library
  artifact hashes, and their mirrored-image control must be detected. A
  mismatch remains a warning for deterministic verification and becomes a
  mandatory blind yes/no question for the human reviewer.
- Review packets list every overlay and comparison image with its SHA-256 and
  expected `vision_review` record path. Approval requires a valid,
  impression-bearing review record bound to every listed image; a missing,
  stale, unreadable, or mismatched record blocks approval.
- The lane guard prevents accidental cross-lane reads through the agent
  tools; it is a context-isolation policy, not a security boundary.

## Consequences

Mechanical extraction, visual evidence, independent authorship, and human
review can be traced to exact artifacts without promoting model agreement or
impressions into a deterministic verdict. A missing evidence link or
authoring consensus fails closed. The remaining risks include correlated
models, imperfect PDF rasterization, and code running with the same account
outside the lane guard.
