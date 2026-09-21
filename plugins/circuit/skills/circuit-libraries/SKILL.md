---
name: circuit-libraries
description: Use the official KiCad and CERN libraries while preserving their provenance and licenses.
version: 0.1.0
license: BSD-3-Clause
triggers:
  - KiCad library
  - CERN library
  - footprint
  - シンボル
---

# Circuit libraries

The image provides official KiCad libraries through the KiCad packages and the
pinned CERN library at `/opt/circuit/libraries/cern-kicad-libs`. Preserve each
library's license and provenance. Never edit files under `libraries/` or either
installed library path; create project-owned copies only when the design requires
a deliberate, documented derivative.
