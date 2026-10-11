# Sister cooperation

## Sister Liaison Protocol (SLP v2)

UX-creator drops work orders as `liaison/<id>.ux-request.json`; circuit answers
with `liaison/<id>.ux-response.json`. `src/circuit/liaison.py` is a local,
strict (extra=forbid) pydantic mirror — no sister code is imported.

### Request (`UxRequestV2`)

`schema_version: 2`, `system: "ux-creator"`, `id` slug equal to the file stem,
`target_agent` (one of bard/circuit/dashboard/doc/firmware/fpga/mech/prodeng/
sim/wire), `stage`, `risk` (high ⇒ non-empty `rationale` citing the UX job id —
existence can't be checked locally), `purpose` ≥20 chars, `requested_changes`
≥1, `inputs` [{path, sha256}], `expected_deliverables` ≥1, `acceptance` ≥1,
`depends_on` [slugs], `created_at` aware datetime.

### Inbox states (`circuit_ux_inbox`)

Per request, precedence order:

1. `stale` — any input's current sha256 differs from the request's (missing
   file counts as changed), or an existing response's `input_hashes` no longer
   match current files.
2. `answered` — a valid `<id>.ux-response.json` from responder `circuit` exists.
3. `blocked` — some `depends_on` id lacks a valid response (any responder).
4. `new` — otherwise.

Files that fail JSON/validation or whose id ≠ stem land in `malformed`
({path, error}) — all reported, never raised. Requests for other agents are
counted in `other_targets`.

### Responding (`circuit_ux_respond`)

Statuses: `accepted`, `in_progress`, `done`, `rejected`, `deferred`,
`needs_info`. `reason` ≥20 chars unless accepted/in_progress. Refusals:

- `done` needs EACH: ≥1 gate_verdict (all `pass`), ≥1 artifact, ≥1
  decision_ref, ≥1 impression_ref — separate explicit checks.
- `done` refused while a verdict is `fail`/`unknown`, when the request is
  stale, or when inputs are missing.
- decision/impression refs must exist as event_ids in the VRP logs.
  Refs resolve only against an intact VRP v2 chain: a log that fails
  `verify_log` (forged, edited or truncated lines) refuses every ref into it.
- artifact paths must exist (file → sha256, dir → tree hash).

For non-done statuses missing inputs are simply omitted from `input_hashes`
so a refusal can say "input missing". The response is hash-bound: input
hashes, artifact hashes, gate verdicts, record refs, `responded_at`.

## Mechanical board envelope

mechanical-agent's `board-envelope` writes `<name>.mech-board-envelope.json`
from its enclosure brief: the board size and mount holes its standoffs
expect, the part height allowed above (`max_height_top_mm`) and below
(`max_height_bottom_mm`) the board, and each side-face opening as a
face-local `along_mm` span plus a `z_mm` span from the board top surface, in
the `board_center_y_up_mm` frame `board-geometry` uses. Pin it in the brief
`enclosure` block (`envelope_path`, `envelope_sha256`) and run
`python -m circuit enclosure --brief BRIEF --geometry <name>.board-geometry.json`
after placement. A tall part or an edge connector with no opening then fails
here, before mech re-imports the board. To change the cavity, ask mech
(ECR/ECO); never edit the envelope. `circuit enclosure` also checks
mech's interchange envelope v3 sidecar (`import_provenance`): `fail` when
it is malformed, describes other bytes or another producer, a source changed
or a decision ref is not in mech's intact log; `unknown` when it is missing.

## Interchange envelope v3

Every circuit export below that a sister reads (`connectivity`,
`board-geometry`, `assembly`, `handling --out`, `sim-request`,
`firmware-export`, CLI and MCP) also writes `<artifact>.provenance.json`
(`src/circuit/_envelope.py`, shared with the family): `envelope_version: 3`,
`producer: "circuit"`, `artifact{name, sha256}`, `sources[{path, sha256}]`
(the brief, PCB, netlist, part specs or STEP that fed it) and
`decision_refs` (circuit decisions whose evidence cites a source at its
current sha256). A broken circuit decision chain refuses the export. The
artifact body is unchanged, so sister strict mirrors keep reading it.

## Interchange files

| File | Circuit role | Consumed by | Shape |
|---|---|---|---|
| `*.connectivity.json` | written by `circuit_connectivity_export` | wire-agent, simulation-agent | frozen — sim mirror is extra=forbid |
| `*.board-geometry.json` (+ `.emn`/`.emp`) | written by `circuit_board_geometry_export` | mechanical-agent (board envelope, mount holes, connector faces) | strict; `unknown` heights fail the export verdict |
| `*.circuit-assembly.json` | written by `python -m circuit assembly` | production-engineering-agent (`circuit-assembly` import, `circuit.assembly` gate) | strict; a populated footprint without a `smd`/`through_hole` attribute is `unknown` and fails the export |
| `*.thermal.sim.json` + `*.thermal.sim-request.json` | written by `circuit_sim_thermal_request` from the brief `thermal` section | simulation-agent (`sim-liaison` answers with `*.sim-response.json`) | strict; hash-bound request id |
| `*.thermal.sim-response.json` | read by `circuit_sim_thermal_check` (`thermal.response_path`) | simulation-agent produces | strict read; stale or tampered → `fail`, unanswered → `unknown` |
| `*.lifetime.sim.json` + `*.lifetime.sim-request.json` / `*.lifetime.sim-response.json` | written/read with `kind: lifetime` from the brief `lifetime` section (`lifetime.response_path`) | simulation-agent (Arrhenius + Miner) | same hash binding; stale or tampered → `fail`, unanswered → `unknown` |
| `*.firmware.json` | written by `circuit_firmware_export` | firmware-agent | frozen — firmware strict mirror |
| `*.fw-pinmap.json` | read by `circuit_firmware_check` | firmware-agent produces | strict read; the `*.fw-pinmap.provenance.json` envelope v3 sidecar must pass (tampered, foreign, broken firmware log or missing sidecar fails the check) |
| `*.envelope.json` | not written by circuit | mech → wire/sim | frozen upstream |
| `*.design-report.json` | written by `circuit_design_report` | doc/prodeng, UX-creator | strict, circuit-owned |
| `liaison/*.ux-*.json` | read + written | UX-creator | SLP v2 strict mirror |

Because several consumers validate with `extra="forbid"`, the interchange
shapes cannot carry optional VRP fields (`decision_refs`, source sha256) —
the reasoning binding lives in the liaison responses instead.

## Change control (ECR/ECO)

A change to a file that another agent pins by sha256 goes through
UX-creator's change records, never a silent re-pin. There is no human
approval step.

1. Raise `<id>.ecr.json` with the reason, the proposal and each file to
   change with its current sha256.
2. After regenerating, write `<id>.eco.json` with each file's `before` and
   `after` sha256, the gate report that passed it (verdict `pass`, citing
   the `after` sha256) and the AI review findings with their dispositions.
   A `blocker` closes only as `fixed`, and the author cannot review its own
   change.
3. `ux change <eco>` (MCP `ux_change_check`) finds every consumer that pins
   a changed file. It fails if one still pins the old sha256, or re-pinned
   without being listed in the ECO.

AI review findings can only stop a change. This plugin's deterministic
gates and the change check decide. The record formats are in
[UX-creator-agent `docs/sister-cooperation.md`](https://github.com/VibeBB/UX-creator-agent/blob/main/docs/sister-cooperation.md#change-control-ecreco-and-design-review).
