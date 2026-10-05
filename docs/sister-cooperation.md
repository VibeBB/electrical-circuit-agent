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
- artifact paths must exist (file → sha256, dir → tree hash).

For non-done statuses missing inputs are simply omitted from `input_hashes`
so a refusal can say "input missing". The response is hash-bound: input
hashes, artifact hashes, gate verdicts, record refs, `responded_at`.

## Interchange files

| File | Circuit role | Consumed by | Shape |
|---|---|---|---|
| `*.connectivity.json` | written by `circuit_connectivity_export` | wire-agent, simulation-agent | frozen — sim mirror is extra=forbid |
| `*.firmware.json` | written by `circuit_firmware_export` | firmware-agent | frozen — firmware strict mirror |
| `*.fw-pinmap.json` | read by `circuit_firmware_check` | firmware-agent produces | strict read |
| `*.envelope.json` | not written by circuit | mech → wire/sim | frozen upstream |
| `*.design-report.json` | written by `circuit_design_report` | doc/prodeng, UX-creator | strict, circuit-owned |
| `liaison/*.ux-*.json` | read + written | UX-creator | SLP v2 strict mirror |

Because several consumers validate with `extra="forbid"`, the interchange
shapes cannot carry optional VRP fields (`decision_refs`, source sha256) —
the reasoning binding lives in the liaison responses instead.
