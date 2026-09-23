---
name: circuit-verification
description: Interpret KiCad ERC and DRC JSON reports and perform deterministic exports.
version: 0.1.0
license: BSD-3-Clause
triggers:
  - ERC
  - DRC
  - KiCad verification
  - 検証
---

# Circuit verification

Use `circuit_erc` and `circuit_drc`, not an LLM report, as the source of truth.
The runtime verdict passes only when `errors == 0` and DRC `unconnected == 0`;
warnings do not fail the verdict. Missing output, process failure, and malformed
JSON are fail-closed. A KiCad jobset is a declarative export set and its ERC/DRC
reports are compared with direct reports for self-consistency. `circuit_render`
provides visual review evidence (`kind`: `board3d` camera views,
`schematic` page plots, `layers` per-layer plots) and `circuit_diff` provides
change evidence (`format`: `json`, `png`, `svg`); neither can replace
deterministic gates. Supported exports are `gerbers`, `drill`, `pos`,
`bom`, `netlist`, `pdf-sch`, `step`, `sch_pdf`, `sch_svg`, `pcb_pdf`, `pcb_svg`,
`dxf`, `ipc2581`, `odb`, `gencad`, `vrml`, `glb`, and `fp_svg`
(footprint-library directory to per-footprint SVG).
