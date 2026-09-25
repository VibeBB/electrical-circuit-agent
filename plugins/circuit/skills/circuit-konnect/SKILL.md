---
name: circuit-konnect
description: Use Konnect MCP toolsets for live KiCad PCB operations without confusing them with verdict authority.
version: 0.1.0
license: BSD-3-Clause
triggers:
  - Konnect
  - live IPC
  - route trace
  - 配線
---

# Circuit Konnect

For schematic authoring, validate a design brief first, load the project/library
toolsets, call `get_symbol_info`, place symbols with `batch_place_components`, and
wire the schematic like a hand-drawn one: connect the main signal chain and serial
paths with `add_wire`/`batch_add_wire` (or `connect_pins`/`batch_connect_pins`)
plus `add_junction` at T-junctions, reserving `batch_connect_to_net` labels for
power rails (VCC/GND) and nets that would otherwise cross. Do not draw pin-to-pin
wires across components. In v0.12.1 call `save_project` with `{}`.

Konnect loads most authoring operations through `load_toolset`, and some
harnesses never re-fetch `tools/list` after that call, so the operations never
appear as visible `konnect_*` tools. Do not loop on `get_active_toolsets` when
that happens. Invoke the operation through `circuit_konnect_call` instead:
`{"tool": "batch_place_components", "arguments": {...}}` spawns a managed
Konnect stdio session and returns its result verbatim. Because toolset state is
session-scoped, batch related calls in the `ops` array so `load_toolset` and the
real ops share one session: `{"ops": [{"tool": "load_toolset", "arguments":
{"name": "schematic"}}, {"tool": "batch_edit_schematic_components",
"arguments": {...}}]}` — the call reports `isError` when any op fails. As a fallback, drive
the same ops with a small stdio JSON-RPC client (the repository's
`scripts/konnect_client.py` pattern) against the `konnect` binary with
`KICAD_API_SOCKET` set; record such terminal invocations in the summary since
they are less visible to policy than `circuit_konnect_call` events.

Keep the `.kicad_sch` under a single-writer discipline: while a Konnect
session is authoring the schematic, do not run host-side writers
(`inject_title_block`, `fit-sheet`, or any other edit) against the same
file — two writers can interleave a truncated file. Apply host-side
writes only between Konnect ops, after `save_project` has flushed. When
`circuit_sch_lint` reports `item_out_of_bounds` on `label`/`global_label`/
`hierarchical_label` items, `python -m circuit fit-sheet <schematic>`
clamps them back inside the frame; out-of-bounds symbols or wires must be
re-placed through Konnect ops instead.

Set `KICAD_API_SOCKET=ipc:///tmp/circuit-kicad.sock` and start the circuit API
server for the selected board first. Load the PCB toolset as needed, then use
`update_pcb_from_schematic`, `set_component_placements`, `route_pad_to_pad`, and
`save_project`. The source should report `ipc` for live operations. Konnect is an
unmodified AGPL subprocess; the authoritative connectivity, ERC, and DRC results
remain the circuit kicad-cli-backed reports.

The complete v0.12.1 coverage matrix is in
`references/konnect-tools.json`, rendered in `docs/konnect-tools.md`. Use it before
adding a tool to an authoring flow. IPC-required tools currently include
`align_components`, `get_component_list`, `query_traces`, and `refill_zones`.
Advisory checks and export comparisons are recorded in the design report and never
change its kicad-cli-derived verdict. Mutating advisory tools run on copies or with
`dry_run` where supported.

## Terminal tool notes

The terminal tool runs **one command per call**: a payload carrying several commands is bounced
as "Cannot execute multiple commands at once". Chain with `&&` inside a single command when you
need two steps, and write files with `file_editor` rather than multi-line heredocs.
