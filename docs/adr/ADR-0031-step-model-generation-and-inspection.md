# ADR-0031 Deterministic STEP model generation and inspection

## Status

Accepted

## Context

3D package models must agree with the mechanically checked PartSpec and
footprint, and must also be interpreted consistently by KiCad. A visually
plausible model is not sufficient: terminal-to-pad mapping, pin-1 orientation,
solid validity, and export behavior need deterministic oracles.

## Decision

- STEP (`.step` or `.stp`) is the only accepted model format, and KiCad model
  transforms must be exactly identity. Manufacturer or user models enter only
  through the existing provenance and license gate. No manufacturer or
  aggregator is downloaded automatically.
- `cadquery-ocp-novtk==8.0.1.0.0` is used behind typed wrappers in
  `src/circuit/occt.py`; this is the sole OCP import boundary. The generator
  emits a deterministic STEP file and a hash-bound manifest. Its supported
  parametric families use separate body, terminal, and exposed-pad solids so
  terminal regions remain independently measurable.
- Verification checks STEP units, validity, closed solids, positive volumes,
  deterministic round-trip geometry, body dimensions, and identity transforms.
  It intersects each solid with the `z=[0, 0.02] mm` terminal slab and requires
  a bijection between terminal-region centers and copper pads. A region must
  fit its pad bbox within `0.025 mm`; adjacent terminal pitch must agree with
  the PartSpec nominal within `0.01 mm`. Geometry, terminal, and pin-1 errors
  fail closed.
- Pin-1 evidence comes from a geometric dimple or qualifying top-face shape,
  with best-effort STEP face-color inspection as a second method. Polarized
  packages without either marker are unverifiable. Manufacturer and generated
  models are cross-checked for body extents, terminal centers, and pin-1
  quadrant.
- The independent export oracle uses `kicad-cli pcb export step` for placements
  at 0 and 90 degrees. Export volume must agree within relative `1e-4`, and
  terminal slab regions must match transformed board pads within `0.02 mm`.
  Missing or inconsistent exports are errors.
- Model vision comparison reuses the hash-bound vision batch and answer path.
  It pairs a datasheet package drawing with a same-scale orthographic KiCad
  render and includes a mirrored control. Each image requires an answer and a
  valid impression. Missing, stale, or failed-control evidence is an error;
  a disagreement is a warning plus a mandatory human question and cannot
  override deterministic findings.

## Consequences

Generated and imported models share the same fail-closed geometry checks, while
KiCad's STEP export independently checks its frame interpretation. Vision
provides additional human-readable evidence without becoming an alternate
acceptance path. OCP/OCCT licensing and the wheel's bundled shared libraries
are documented in `THIRD_PARTY_NOTICES.md`.
