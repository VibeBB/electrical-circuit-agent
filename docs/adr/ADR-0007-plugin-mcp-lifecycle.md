# ADR-0007 Plugin MCP configuration and api-server lifecycle

- Status: Accepted
- Date: 2026-09-20
- Related: ADR-0002, ADR-0003, ADR-0005

## Context

An OpenHands plugin needs to invoke deterministic KiCad operations and
verification from conversation. Sub-agents do not inherit the parent's MCP
configuration. Also, KiCad's headless api-server handles only one `.kicad_pcb`,
and the `.kicad_pro` open-document handler is not available.

## Decision

The Python runtime is `python3 -m circuit.mcp_server` as a stdio MCP server.
Konnect is launched as a separate server with
`KICAD_API_SOCKET=ipc:///tmp/circuit-kicad.sock`. The runtime owns
`/tmp/circuit-kicad.sock` and `/tmp/circuit/api-server.json` (or under
`CIRCUIT_STATE_DIR`) and provides start/status/stop. doctor checks for missing
tools, versions, the socket directory, and the CERN library as fail-closed.

## Consequences

The MCP configuration included in the plugin can be made explicit to both the
parent and sub-agents. The KiCad lifecycle is consolidated in one place,
avoiding server conflicts when switching boards. Because we use an installed
package inside the image rather than a git-pinned PEP 723 script, the runtime
imports and the MCP launch run in the same environment.
