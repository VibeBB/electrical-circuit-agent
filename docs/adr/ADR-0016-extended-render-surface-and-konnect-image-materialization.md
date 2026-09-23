# ADR-0016 Extended render surface and Konnect image materialization

- Status: Accepted
- Date: 2026-09-23

## Decision

`circuit_render` becomes a multi-kind render surface behind a `kind`
parameter (`board3d` default, `schematic`, `layers`) instead of three
separate tools, keeping the MCP surface flat and every existing
`circuit_render` call valid. The `board3d` kind exposes the full `pcb
render` camera: six orthographic `side` views, `rotate`/`pan`/`pivot`
(`X,Y,Z` strings, validated), `zoom`, `perspective`, `floor`, `background`,
and `quality`. `schematic` runs `sch export png` (per-page PNGs) and
`layers` runs `pcb export png` (per-layer PNGs); both take an output
directory and return the produced paths, attaching up to four images as
`ImageContent` so an advisory visual review can inspect them (ADR-0013).

`circuit_diff` gains a `format` parameter (`json` default, `png`, `svg`):
the JSON report keeps its `changes` parse only for `format: "json"`, while
visual formats attach the produced image. `circuit_export` gains `fp_svg`
(`fp export svg`); because `kicad-cli` resolves footprint libraries, its
`source_path` must be a library directory containing `.kicad_mod` files.

Konnect tool results are scanned for embedded image payloads: any base64
string at least 1024 characters long that decodes to PNG or JPEG bytes —
including `data:` URLs and MCP image blocks inside `ops` batch results —
is written to `$CIRCUIT_KONNECT_IMAGE_DIR` (default
`circuit-reports/konnect-images/` relative to the working directory) as
`NNN.png`/`NNN.jpg` and replaced inline by
`{"image_path", "sha256"}`. The Konnect binary itself stays unmodified and
subprocess-only (ADR-0003); interception happens purely on our side of the
stdio pipe.

## Rationale

The advisory visual review loop (ADR-0013) needs more than one top view:
side elevations approximate the missing cross-section feature, schematic
and per-layer plots let a vision model check readability and copper
details, and `sch/pcb diff --format png` gives a deterministic visual diff
for revision comparison — all kicad-cli built-ins requiring no new
dependency. Konnect's screenshot/plot answers arrive as base64 blobs that
no consumer could reach as files; materializing them to disk keeps the
provenance trail (`image_path` + `sha256`) instead of inlining megabytes
into conversation context.

## Consequences

- `src/circuit/kicad_cli.py` adds `render_schematic`, `render_layers`,
  camera args on `render`, a `format` parameter on `diff`, and `fp_svg`.
- `src/circuit/mcp_server.py` dispatches `circuit_render` by `kind`,
  attaches up to four `ImageContent` blocks per call, and rewrites Konnect
  image payloads to files; `circuit_diff(format=png)` attaches the diff
  image.
- `tests/integration/test_render_surface_docker.py` exercises every kind
  inside the pinned tools image against the smoke-board fixture.
- All rendered/diffed/materialized images remain advisory evidence only
  (ADR-0011, ADR-0013): nothing in this surface promotes a visual
  observation to a connectivity, ERC, or DRC verdict.
- True 3D cross-sections stay out of scope: `kicad-cli pcb render` has no
  clipping plane; side elevations plus generated stackup diagrams cover
  the need.
