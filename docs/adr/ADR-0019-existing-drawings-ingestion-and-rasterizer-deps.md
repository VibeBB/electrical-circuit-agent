# ADR-0019 Existing-drawings ingestion, rasterizer dependencies, and the stackup section diagram

- Status: Accepted
- Date: 2026-09-23

## Decision

Adopt the plan's D1 recommendation and wire three new MCP surfaces for
ingesting existing drawings and approximating cross-sections.

`circuit_import` wraps `kicad-cli sch import` / `pcb import`
(Altium/Eagle/CADSTAR/EasyEDA(+Pro)/LTspice/PADS/DipTrace/PCAD/OrCAD
schematics; PADS/Altium/Eagle/CADSTAR/Fabmaster/PCAD/SolidWorks boards).
The converted KiCad file is written to `output_path` and the importer's
own `--report-format json` report lands next to it as
`<output>.import.json` — the tool result carries the parsed report, so
what the importer actually converted is on record rather than
paraphrased. Unsupported format hints, missing inputs, missing outputs,
and unparseable reports fail closed.

`circuit_rasterize` turns intake files into PNGs the vision lane can
read: `.pdf` via `pdftoppm` (one PNG per page) and `.svg` via
`rsvg-convert`. Two small apt packages — `poppler-utils` and
`librsvg2-bin` — are added to the tools image; `rsvg-convert` is the same
rasterizer bard-agent adopted for score rendering. Missing binaries or
zero output fail closed with a named-package hint; binaries are
overridable via `$CIRCUIT_PDFTOPPM` / `$CIRCUIT_RSVG_CONVERT` (the
`$CIRCUIT_KICAD_CLI` pattern). PyMuPDF was rejected: a PyPI wheel is a
heavier trust surface for the same capability.

`circuit_stackup` runs `pcb export stackup --format json` and renders a
deterministic drawing-style section SVG (`src/circuit/stackup.py`,
stdlib only): one band per enabled layer colored by type
(copper/dielectric/silkscreen/mask/paste/finish), height proportional to
`thickness.valueNm` with a minimum visible band, and labels carrying
material, mm thickness, and dielectric εr/tanδ. This is the
cross-section approximation of record — combined with `board3d`
orthographic side elevations it covers connector-height and stack review
without a FreeCAD/OCCT dependency (option 3 in the plan, deferred).

## Rationale

Foreign CAD intake and PDF datasheets were the last uningestable inputs
(G5): the events-dir hook only materializes embedded images, so PDF
datasheets/drawings and non-KiCad sources had no path into either the
gate lane or the vision lane. `circuit_import` converts CAD sources into
first-class KiCad artifacts (which then get the full render/diff/gate
surface), and `circuit_rasterize` converts PDF/SVG into the PNG lane the
rest of the vision machinery already understands. The stackup diagram
closes G8: KiCad has no clip-plane render, so a generated section
drawing from the authoritative stackup JSON is the honest cross-section
— deterministic, reviewable, and versionable.

## Consequences

- `src/circuit/kicad_cli.py` adds `import_file` (with per-kind format
  allowlists) and `export_stackup`; `src/circuit/stackup.py` and
  `src/circuit/raster.py` are new stdlib modules.
- `docker/circuit-tools.Dockerfile` installs `poppler-utils` +
  `librsvg2-bin` (unpinned apt, Ubuntu 26.04 archive) and smoke-checks
  both binaries at build time; `scripts/check_dependency_updates.py`
  records them as apt targets with deferred version tracking.
- The `record-image-observation` hook matcher now covers
  `circuit_rasterize`, so rasterized intake pages get the same
  sha256 provenance as renders.
- `circuit-brief` directs PDF datasheets through `circuit_rasterize`
  and foreign CAD sources through `circuit_import`, keeping the
  source-image comparison vision loop documented.
