# Architecture

## Responsibility split

| Layer | Responsibility |
|---|---|
| OpenHands SDK / Agent Canvas | Conversation, delegation, workspace, plugin distribution |
| circuit-agent | KiCad tools image, api-server lifecycle, ERC/DRC JSON verdicts, skills, agents |
| Konnect | MCP tools providing KiCad file editing and live IPC |
| KiCad | `kicad-cli`, headless IPC, ERC/DRC execution |

`circuit-agent` is not a fork of ACD. It does not adopt the Design Graph,
Evidence, or L1-L3 gates; ERC/DRC verdicts are limited to deterministic parsing
of `kicad-cli` JSON.

## Execution sequence

```text
user
  -> orchestrating agent
  -> circuit-brief / circuit-library (+ part-author lanes a/b)
  -> circuit-schematic / circuit-layout / circuit-review
  -> Konnect MCP or kicad-cli
  -> .kicad_pro / .kicad_pcb / JSON
```

`circuit-schematic` owns the schematic, `circuit-layout` owns board placement
and routing, and `circuit-review` owns checking the deterministic ERC/DRC
output. Sub-agents are delegated via the SDK's `TaskToolSet` and
`AgentDefinition`.

## Records layer (VRP v1)

Design reasoning is append-only JSONL under `observations/circuit/`:
`decisions.jsonl`, `impressions.jsonl`, `vision-reviews.jsonl`,
`vision-tool-events.jsonl`, `image-observations.jsonl`, plus
`records-status.json` and `.sessions/` for hook state. Writers are
`src/circuit/records.py` (MCP tools `circuit_record_*`, `circuit_records_status`
and the `record` CLI subcommand); enforcement lives in the shared hooks
`require_records.py` (session-start hint + Stop gate) and `_records.py`
(mirror of the typed writers, stdlib-only, canonical across the family and
verified by AST hash in `scripts/check_shared_hooks.py`). `records-policy.json`
declares the artifact globs an impression must bind. Records are advisory —
they never change an ERC/DRC/kicad-cli verdict and never gate request inputs.
Blind authoring lanes (`CIRCUIT_AUTHORING_LANE=a|b`) are denied all records
paths and tools by `guard_author_lane.py`.

## Liaison layer (SLP v2)

`src/circuit/liaison.py` is a local, strict pydantic mirror of the UX-creator
contract — no import of sister code. `liaison/<id>.ux-request.json` files are
work orders; `circuit_ux_inbox` lists them with states new/answered/stale/
blocked plus a `malformed` list that never raises; `circuit_ux_respond` writes
`liaison/<id>.ux-response.json` hash-bound to current inputs and artifacts.
A `done` response is refused unless it carries at least one gate verdict (all
pass), one artifact, one decision_ref, and one impression_ref — each checked
separately — and when the request inputs are stale or missing. Missing inputs
are omitted from `input_hashes` for non-done statuses so a refusal can say
"input missing" without writing an empty digest.

## Advisory layer

All Konnect tools are classified by role, stage, and IPC requirement in the
coverage matrix. Konnect observations, reviews, visual comparisons, and
manufacturing exports made during authoring are recorded as `AdvisoryResult`,
but advisory success or failure is never promoted to a verdict. Connectivity,
ERC, and DRC verdicts continue to be determined solely by `kicad-cli` JSON, and
mutating advisory tools run as dry-runs or in a duplicated workspace.

KiCad 11 nightly's `kicad-cli jobset run` declaratively bundles ERC, DRC,
netlist, and manufacturing artifacts. Consistency with directly executed ERC/DRC
JSON is verified as a separate gate, and `pcb render` PNGs plus `sch`/`pcb diff`
change evidence are recorded for human review. Render/diff does not replace the
deterministic connectivity/ERC/DRC verdicts.

## Plugin boundary

`plugins/circuit` consists of `.plugin/plugin.json`, `.mcp.json`, `agents/`,
`commands/`, `hooks/`, and `skills/`. There are two MCP servers: the Python
runtime `python3 -m circuit.mcp_server` and the unmodified Konnect binary.
Because sub-agents do not inherit the parent conversation's MCP/hooks, each
AgentDefinition declares its MCP map explicitly.

The runtime's fixed paths are the API socket `/tmp/circuit-kicad.sock` and the
state file `api-server.json` under `CIRCUIT_STATE_DIR` (default `/tmp/circuit`).
doctor reports missing tools, version mismatches, socket directory problems, and
missing CERN libraries as fail-closed.

## Headless IPC

Inside the Docker image, start `kicad-cli api-server --socket <path>
<board.kicad_pcb>` and pass `KICAD_API_SOCKET=ipc:///tmp/<name>.sock` to Konnect.
The CLI `--socket` is a filesystem path; the client environment variable is an
IPC URL.

One api-server opens one `.kicad_pcb`. To switch boards, stop the existing
server and restart it with a different board. The server lifecycle is managed
by circuit-agent, and Konnect is treated as an unmodified separate process.
