# Hexagonal-cell-grid autorouting (CuFlow) research

Scope: the Hackaday article "Improving an Autorouter with a Hexagonal Grid"
(2026-10-11), its primary source (James Bowman's Substack post, 2026-10-06),
the CuFlow implementation, the geometry claims, prior art, and the evaluation
for this repository's routing pipeline. No code is adopted by this file; it
records research per the `docs/research/` convention.

Primary sources:

- [Hackaday article](https://hackaday.com/2026/10/11/improving-an-autorouter-with-a-hexagonal-grid/)
  (Aaron Beckendorf, 2026-10-11) — secondary coverage.
- [Routing PCBs on a hex-cell grid](https://excamera.substack.com/p/routing-pcbs-on-a-hex-cell-grid)
  (James Bowman, Excamera Labs, 2026-10-06) — primary post.
- [CuFlow repository](https://github.com/jamesbowman/cuflow) at commit
  `502c783372c31eace0db919102344fe0d7195c16` (2026-10-04): `hex.py` (grid
  math), `hexboard.py` (~610 lines, the router), `spiq_a.py` (SPIDriver
  Quattro board driver), `tests/test_hexboard.py`, `router.py` (the older
  square-grid router), `images/cuflow-via-clearances.png` (dimensioned
  hex figure). BSD-3-Clause.

## What CuFlow is

CuFlow is Bowman's procedural PCB layout system: a Python library that
describes a board as code and emits Gerbers, drill files, BOM, pick-and-place,
POVRay renders and SVG. It has been used for all of his production PCBs since
2020 (TermDriver 2024 used partial autorouting). The headline board for the
hex work is SPIDriver Quattro (Crowd Supply pre-launch): RP2040, 4-layer
ENIG PCB, 61×49×6 mm; per the post, "almost every signal is auto-routed" —
QSPI to four channels, USB D+/D-, crystal, SWD/JTAG, the LCD bus, and two
3-terminal I2C nets.

## The hex router, from the source

Grid (`hex.py`): axial hex coordinates stored as odd-r offset pointy-top
(the code comment calls it "oddr" — the Red Blob Games convention). The row
pitch `size` is 0.4 mm; nearest-neighbor spacing `height = size/sin(60°)`
= 0.4619 mm. `Hex.distance` is the standard `(|dq|+|dr|+|ds|)/2`; helpers
cover `from_xy`, `to_plane`, `neighbors`, `neighborhood`, `line`, `rot`.

Blocking (`hexboard.py`): blocking is geometric, not raster-approximate.
`hex_setup()` computes `route_radius = max(drill/2, trace/2 + clearance)`
and builds `route_disks` plus shapely `STRtree` spatial indexes. Per layer,
`layer_blocks(nm, width, exempt_points, exempt_geometries)` marks cells
whose route disk intersects buffered copper geometry or drill keepouts.
`edge_blocks(width)` insets the board outline by `clearance + width/2`,
plus the via radius at concave corners. Pad endpoints are cells whose
wire-width disk is at least 50 % inside the pad boundary
(`pad_hex_cells`); off-lattice pads are therefore snapped to the cell
set, and endpoints wider than the trace reserve wider corridors.

Search: `hex_route(a, b)` is a per-layer BFS wavefront (Lee's algorithm,
1961): it floods a distance field over free cells and backtracks the
minimum-cost path. It **asserts both endpoints are on the same layer** —
there is no via insertion; the Quattro board routes on the two outer
copper layers (inner layers are planes) and any vias are placed by the
board script. `hex_route_net(terminals, width)` fans out per-endpoint
wavefronts and joins them at the cell minimizing (sum of distances, then
max distance) — a single-junction Steiner heuristic, not a true Steiner
tree. There is no rip-up/reroute strategy: net ordering is the board
script author's problem. There are no diff-pair, length-matching, or
skew constraints.

Escapes: fine-pitch fanout is still hand-authored — `river_ongrid`,
`wire_ongrid`/`wire_ongrid2` snap escape "river" traces to the lattice and
quantize direction to `30 + 60·k` degrees before handing off to the
autorouter.

Contrast with the older square-grid router (`router.py`): a numpy Lee
wavefront over two layers with `COST_H=[5,1]`/`COST_V=[1,5]` preferred-
direction costs and `COST_VIA=100` — the classic non-uniform-cost square
lattice the post argues against.

## The geometry claim — verified

Mechanism: a via sits on a lattice node. The worst-case trace is the
chord between two neighbor nodes adjacent to the via. For a minimum
via-to-trace separation `R`:

- Square grid: the two orthogonal neighbors sit 90° apart, so the chord
  passes at `p/√2`; hence `p ≥ √2·R`. With the post's stack (pad Ø0.45,
  drill Ø0.30, trace 0.127) `R ≈ 0.4137` → `p = 0.585 mm`, cell area
  `p² = 0.342 mm²`.
- Hex grid: two adjacent neighbors sit 60° apart, so the chord is the
  altitude of an equilateral triangle, `s·√3/2` — exactly the row pitch
  `0.4 mm`. Cell area `(√3/2)s² = 0.185 mm²` — about half.

Numeric check against the published dimensioned figures (trace 0.127,
pad Ø0.45, drill Ø0.30): hex pad→track `0.4 − 0.225 − 0.0635 = 0.1115`,
hole→track `0.1865`, track-to-track `0.4 − 0.127 = 0.273`; square
pad→track `0.4137 − 0.2885 = 0.1252`, hole→track `0.2002`. All five match
the figures exactly.

For the *same* minimum separation `R`, a square lattice needs cell area
`2R²` vs hex `2R²/√3` — hex packs `√3 ≈ 1.73×` more cells; the measured
pair shows 1.89× because the pitches were sized to slightly different `R`
(0.40 vs 0.4137 mm). Equivalently: hex spacing `s ≥ 2R/√3 ≈ 1.155R` vs
square `p ≥ √2R ≈ 1.414R`.

## Why it helps, and what it does not solve

Gains: six equidistant directions (0°/±60°) remove the square grid's
diagonal non-uniformity (no `√2` penalty, no need for an 8-neighbor
approximation); denser channel packing under a given clearance rule; a
uniform step cost gives a clean distance field; and the cell-blocking
test is exact geometry (shapely), not a raster approximation.

Limits in the current implementation: per-layer routing only (no via
insertion — via placement is manual); no rip-up/reroute or net-ordering
strategy; no diff-pair/length-match/skew constraints; off-lattice pads
snap to cells (fine-pitch escapes still need the hand-authored river
prologue); the multi-terminal join is a single-junction heuristic.
Manufacturing is unaffected — hex traces emit as ordinary Gerber draws,
so the "ODB/CAM" worry raised in article comments is moot; conversely
DDR/high-speed asks in the same thread are unaddressed by this design.

## Prior art and landscape

- Lee's algorithm (1961) and the Akers coding improvement (1967) — the
  same wavefront core.
- "HAM — a hexagonal array machine" (ICCAD 1989; IEEE TCAD 1990): a
  hardware accelerator whose processors are wired in a hexagonal mesh to
  run Lee expansion over multi-layer grids. Conceptually adjacent — the
  hex mesh there is the machine's interconnect for wavefront propagation,
  not the copper lattice — but it is the earliest hex-mesh routing
  hardware found.
- Y-architecture VLSI routing (Chen/Cheng/Kahng/Mandoiu/Wang, IEEE TCAD
  2005): 0°/60°/120° wiring directions; the hex PCB router is the
  board-level analog, and the VLSI literature already preferred Y's
  regular routing grid over X-architecture (45°).
- FreeRouting 2.x (this repo's bundled autorouter, v2.4.1): post-2.0.1
  the engine is an A* `search_wave` over door sections, drills and the
  goal — a topological room/door model, not a raster grid — with fanout,
  up-to-30 autoroute passes, rip-up retry, and postroute optimization.
  A fundamentally different architecture from CuFlow's uniform raster;
  its strength is completion (rip-up, multi-layer, compaction), not grid
  density.
- KiCad's interactive router is push-and-shove, not an autorouter.
- Red Blob Games' hex-grid guide is the coordinate-convention source the
  code follows (oddr offset, axial/cube distance).
- Adjacent work surfaced in the comment thread: TraceMaker (placement
  optimization rather than routing); the "octagon can't tile the plane"
  exchange is exactly why a hex lattice is the densest regular option.

## Evaluation for this repository

Current routing surface: Konnect v0.13.0 MCP tools
(`route_trace`, `route_pad_to_pad`, `add_via`, …) drive KiCad's
interactive routing, and the Specctra/FreeRouting family
(`export_specctra_dsn` → `route_specctra_dsn` → `apply_specctra_ses`,
`check_freerouting`, `plan_specctra_ses_import`) targets FreeRouting
2.4.1 in the tools image. The DSN/SES chain is **blocked upstream**:
KiCad 11 serializes footprint positions as `(transform …)` and Konnect's
exporter expects `(at x y)` (ADR-0021). The recorded fallback there is a
repository-owned DSN writer/SES reader (the approach acd-agent takes).

Assessment:

- License: BSD-3-Clause — studying or porting ideas is permitted with
  attribution; the repo invariant (never import-bind GPL/AGPL) is
  unaffected. Note the LICENSE text names "gd2-lib" in the no-endorsement
  clause — a copy-paste quirk, still BSD-3-Clause in substance.
- A small hex-cell Lee router is a credible deterministic supplement for
  simple outer-layer signal routing where the Specctra chain is blocked,
  and the grid-sizing rule (`square p ≥ √2R`, `hex s ≥ 2R/√3`, cell-area
  ratio `√3`) is a reusable analytic for clearance-aware corridor
  feasibility checks even without a router.
- It does not replace FreeRouting: no rip-up, no multi-layer/via
  insertion, no compaction, and no constraint routing. And it can never
  be promoted to a routing verdict — only `kicad-cli` DRC/connectivity
  gates decide, per the invariants.
- If a routing fallback becomes necessary, the recorded preference stays
  (a) the repo-owned DSN writer/SES reader (keeps FreeRouting's full
  feature set) over (b) porting the hex router; use the hex-grid
  feasibility math independently of that choice.

## Open questions / unverified

- No published completion or runtime benchmark against a square grid on
  the same board — the published claim is density-driven.
- Whether 0.4 mm pitch is a manufacturing sweet spot or just a round
  number for JLCPCB-class rules.
- Behavior on inner signal layers and mixed plane pours is unmeasured
  (Quattro's inner layers are planes).
