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
  -> circuit-schematic / circuit-layout / circuit-review
  -> Konnect MCP or kicad-cli
  -> .kicad_pro / .kicad_pcb / JSON
```

`circuit-schematic` owns the schematic, `circuit-layout` owns board placement
and routing, and `circuit-review` owns checking the deterministic ERC/DRC
output. Sub-agents are delegated via the SDK's `TaskToolSet` and
`AgentDefinition`.

## Advisory layer

All 234 Konnect tools are classified by role, stage, and IPC requirement in the
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
