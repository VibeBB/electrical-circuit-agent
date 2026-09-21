---
description: Export deterministic KiCad manufacturing or documentation artifacts.
argument-hint: <board.kicad_pcb> <kind>
allowed-tools:
  - terminal
---

Call `circuit_export` with one supported export kind, source path, and output
directory. Supported kinds are `gerbers`, `drill`, `pos`, `bom`, `netlist`,
`pdf-sch`, `step`, `sch_pdf`, `sch_svg`, `pcb_pdf`, `pcb_svg`, `dxf`, `ipc2581`,
`odb`, `gencad`, `vrml`, and `glb`. Report the returned paths and any error
without inventing artifacts.
