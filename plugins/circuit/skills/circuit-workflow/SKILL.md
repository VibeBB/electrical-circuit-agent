---
name: circuit-workflow
description: Execute a KiCad design workflow with explicit board server lifecycle and deterministic verification.
version: 0.1.0
license: BSD-3-Clause
triggers:
  - KiCad design
  - circuit workflow
  - 回路設計
  - 基板設計
---

# Circuit workflow

Clarify requirements, identify the project and source paths, and assign the brief-intake
agent first. Resolve open questions, require an `ready` intake report and a `pass` library
report, then assign schematic, layout, and review work through the SDK task tools. A headless API server handles one
`.kicad_pcb` at a time. Start it before IPC operations and stop it before switching
boards. Use Konnect live IPC for interactive edits; use direct file editing only when
the operation does not require an open board. Write and validate a design brief,
author schematic nets with labels rather than crossing pin-to-pin wires, and run the
authoritative netlist connectivity check before ERC. Save before running ERC or DRC,
and use the circuit MCP server as the deterministic verification boundary.
When the board has an MCU, export its pin connectivity for firmware-agent and confirm the
returned pin map with `circuit_firmware_check` (circuit-firmware skill).

For each new project-owned part, delegate library authoring to `circuit-library`
and follow the ordered [`circuit-library-authoring`](../circuit-library-authoring/SKILL.md)
skill before schematic authoring. Do not route around its HumanRequest stops or
the required hash-bound library review.
When `circuit_brief_library_check` returns `authoring_requests`, delegate each
request to `circuit-library`, then rerun `circuit_brief_library_check` before
proceeding. Never replace a requested part with a similar library item; retain the
requests as `Q*` open questions until the gate passes or a human resolves them.

## Records you must leave (VibeBB Record Protocol — mandatory, unprompted)

Record these without being asked; the Stop hook refuses to finish a
session that still owes them.

- **Decision** (`circuit_record_decision`) for every non-trivial choice:
  the question, the first principles / physical laws / standards it rests
  on, at least two options with pros and cons, the chosen option, a
  rationale of 200+ characters, evidence (artifact paths are hashed; cite
  datasheets or standards as references), assumptions, unknowns, residual
  risks and the observation that would reopen it. Reason from principles,
  not from habit. Typical circuit decisions: part and package choice, net
  naming and power topology, connector placement, stackup and rule
  profile, placement/routing strategy, a DRC waiver refusal, and the
  library lane comparison outcome.
- **Stage impression** (`circuit_record_impression`) when a stage ends —
  intake/brief, library, schematic, layout, review, manufacturing export —
  after its final regeneration: 400+ characters and 3+ sentences on what
  you noticed, what works, what worries you, how a maker or user would
  read the result, and what to do next. List the stage's output files or
  directories so the impression is bound to their sha256.
- **Vision review** (`circuit_record_vision_review`) every time you look
  at an image (a board or schematic render, a photo, a datasheet crop, an
  `inspect_image_with_vision` answer): findings plus a long-form
  impression of 400+ characters judging accuracy, ambiguity, whether the
  design intent comes across and whether the shop floor could act on it —
  not only legibility. Bind it to `image_path` or to the vision event's
  `source_event_id`.

Vision and impressions are advisory: they never change an ERC, DRC or
kicad-cli verdict. Results do not have to be identical from run to run;
the reasoning must be recorded every run. `circuit_records_status` shows
what is still owed.

## Vision points

Look at every render through the inline image or `inspect_image_with_vision`,
then record BOTH a `circuit_record_vision_review` and a `review-record`
advisory; the impression needs 400+ characters and 3+ sentences judging
accuracy, ambiguity, design intent, and usefulness to the maker/user —
not only legibility.

| Stage | What to look at |
|---|---|
| intake | attached photos and sketches (checklist `intake_image`) |
| library | symbol/footprint/3D renders, datasheet crops, lane comparison renders |
| schematic | page plot after sch_lint (checklist `schematic`) |
| layout | top/bottom/side/isometric views, layer plots, stackup PNG after DRC |
| review | circuit_diff PNGs (checklist `diff`) |
| manufacturing | gerber layer plots and fab PDF pages rasterized via circuit_rasterize |

## Sister Liaison Protocol (SLP v2)

UX-creator drops `*.ux-request.json` files into `liaison/`. Call
`circuit_ux_inbox` at session start and at every stage boundary; answer every
`circuit` request with `circuit_ux_respond` — `accepted`/`in_progress` when
you start, `done`/`needs_info`/`rejected`/`deferred` when you finish. A `done`
answer requires artifacts, gate verdicts, and VRP `decision_refs` +
`impression_refs`; report `malformed`, `stale`, and `blocked` inbox entries to
the user instead of working around them.
