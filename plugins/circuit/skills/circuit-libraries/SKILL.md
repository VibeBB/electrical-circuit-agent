---
name: circuit-libraries
description: Use the official KiCad and CERN libraries while preserving their provenance and licenses.
version: 0.1.0
license: BSD-3-Clause
triggers:
  - KiCad library
  - CERN library
  - footprint
  - 3D model
  - STEP model
  - シンボル
---

# Circuit libraries

For new project-owned parts, use the ordered
[`circuit-library-authoring`](../circuit-library-authoring/SKILL.md) workflow
and its `circuit-library` orchestrator. It requires independent PartSpec lanes,
fresh verification, and hash-bound human approval.

The image provides official KiCad libraries through the KiCad packages and the
pinned CERN library at `/opt/circuit/libraries/cern-kicad-libs`. Preserve each
library's license and provenance. Never edit files under `libraries/` or either
installed library path; create project-owned copies only when the design requires
a deliberate, documented derivative.

For every project-authored package footprint, make STEP work mandatory:
generate a deterministic model with `circuit_model_generate`, inspect its facts
and findings with `circuit_model_inspect`, and verify the completed library item
with `circuit_library_verify`. Create the hash-bound datasheet comparison with
`circuit_model_compare` and answer it through `circuit_vision_answer`, including
the required impression for each image. Do not mark 3D evidence complete when
any of these steps is missing.

If verification reports `model_terminals_unseparable` for a manufacturer STEP,
do not bypass the finding, merge solids, or weaken the check. Stop and ask the
human a HumanRequest-style question explaining that the STEP's terminal solids
cannot be mapped separately to footprint pads and requesting guidance on an
alternative authoritative model or next step.
