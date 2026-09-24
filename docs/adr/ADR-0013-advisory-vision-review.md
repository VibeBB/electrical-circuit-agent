# ADR-0013 Advisory vision review via SDK ImageContent

## Status

Accepted

## Decision

Board and schematic renders may be inspected by a vision-capable LLM as
additional advisory evidence. `circuit_render` returns the produced PNG both
as a JSON path (text) and as an MCP `ImageContent` block, so a vision-capable
conversation model sees the image inline in the tool result; the SDK silently
drops image blocks for non-vision models, keeping the text path intact.
`inspect_image_with_vision` remains the path for images the user attaches to
the conversation (board photos, datasheet screenshots, hand-drawn
schematics) — it only reads images in the latest user message and cannot
reach workspace renders.

A `post_tool_use` hook records every successful `inspect_image_with_vision`
call (profile, model, question, response hash) to
`observations/circuit/vision-tool-events.jsonl` for provenance cross-checking.

Visual observations are advisory for human judgement only: they are never
promoted to connectivity, ERC, or DRC verdicts (ADR-0011, ADR-0012), and a
review missing a vision path records `advisory visual review skipped` and
continues (fail-open) rather than blocking. Text visible inside an image is
data, not instructions; it must never be executed as a design change or a
pass/fail directive.

## Consequences

- `src/circuit/mcp_server.py` appends `ImageContent` to `circuit_render`
  results when the output file is a readable PNG; other tools keep the
  text-only result.
- `circuit-review` documents the advisory visual review procedure;
  `circuit-brief` records image-derived details as `A*`/`Q*` intake items,
  never as stated requirements.
- Regression detection between revisions prefers the deterministic
  `set_visual_baseline` / `compare_visual_baseline` Konnect tools over
  free-form vision inspection.
- Remote HTTP(S) image URLs remain unused; only workspace files and
  user-attached `data:` images participate.

## Rationale

ERC/DRC JSON cannot see silkscreen collisions, component overhang, missing
polarity marks, or unreadable schematics — the exact class of defects a
render reveals. SDK 1.49.x already provides the whole pipeline (`ImageContent`
messages, MCP image conversion, `file_editor` image view,
`inspect_image_with_vision`); wiring it here is a small additive change with
no new authority surface, consistent with the existing advisory-evidence
policy.
