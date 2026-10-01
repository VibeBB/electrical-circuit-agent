# ADR-0027 Pinout orientation oracles

## Status

Accepted

## Context

Incorrect package pin numbering or a mirrored footprint can invalidate
electrical and physical verification even when the individual symbol and pad
shapes appear plausible. Datasheet pinout drawings are often rotated text,
and their view direction is material: a bottom view is mirrored relative to
the package top view.

## Decision

- `PackageSpec.drawing_view` records the view cited by the package pin-1
  reading. `PackageSpec.pin1_corner` is always expressed in the canonical top
  view; a bottom-view reading mirrors left and right when checked against that
  corner. `Pin.view` may record the view associated with an individual pin
  reading, but is descriptive evidence and does not change pin-number
  interpretation.
- A `PinoutDrawing` cites its PDF page and bounding box, declares `top` or
  `bottom`, includes a separately bounded view reading, and records vision
  labels and their observation record. The declared view must be supported by
  visible mechanical text and the vision reading.
- Pinout geometry is derived only from visible words that agree between
  Poppler and pdfplumber. Coordinates use PDF page points with y increasing
  downward. Bottom-view x coordinates are mirrored around the label centroid
  before normalization to the canonical top view. Numeric labels from 1
  through `pin_count` must each occur exactly once; numbers above `pin_count`
  such as an exposed-pad number are ignored.
- Number-to-name association uses the package side geometry and searches
  outward first, then inward while excluding labels closer to another pin.
  Missing and ambiguous names are errors. Vision labels are compared with the
  mechanically derived names and PartSpec pin names; name-permutation
  hypotheses are diagnostic only and do not repair the input.
- Pinout geometry is mandatory for `no_lead_quad`, `no_lead_dual`,
  `gullwing_quad`, and `gullwing_dual`. Missing or unavailable geometry fails
  closed. Other families keep pinout optional.
- Library verification compares numbered copper-pad centers with the
  canonical drawing geometry, excluding exposed-pad numbers outside the signal
  pin range. It checks chirality, pin-1 rotation, and cyclic pin order in that
  sequence. Symbol pin names are compared by number, with any suggested
  permutation reported as information rather than applied automatically.
- The orientation oracle covers only the four required families above. It does
  not claim coverage for grid/BGA packages, connectors, or through-hole
  families. Graphics-only pinouts without mechanically recoverable, visible
  number labels fail closed rather than receiving inferred geometry.

## Consequences

Fresh PartSpec checks provide a reproducible geometry oracle for downstream
library verification and review packets. A human review packet separately
shows pin names at positions and asks whether the cited pinout is top or
bottom view; human approval does not override a deterministic failure. These
checks reduce common mirrored and numbering errors but are not a universal
package-recognition system.
