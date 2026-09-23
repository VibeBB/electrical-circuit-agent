# circuit plugin

`circuit` is a lightweight OpenHands plugin that uses KiCad 11 nightly.
The planned install source is:

```text
github:VibeBB/electrical-circuit-agent
path: plugins/circuit
```

Running it requires docker — every entry point goes through
`plugins/circuit/scripts/circuit_launcher.py`, which execs the module inside the
digest-pinned `circuit-tools` image (`$CIRCUIT_TOOLS_IMAGE` →
`docker/image-digests.json` → a local build of the cached Dockerfile). The image
bundles `kicad-cli`, `konnect`, the CERN/official libraries, and the Python
`circuit` package; the plugin's shipped `src/` is mounted read-only at
`/plugin-src` so assets and code never diverge. The KiCad API socket
(`KICAD_API_SOCKET`, default `/tmp/circuit-kicad.sock`) is bind-mounted when it
exists on the host. ERC/DRC verdicts are based solely on `kicad-cli` JSON.

`circuit-brief` creates a brief and intake sidecar from conversational
requirements, and schematic authoring proceeds only after intake is `ready` and
library verification is `pass`. The runtime MCP includes
`circuit_brief_intake_check` and `circuit_brief_library_check`.

The four sub-agents declare the same library-protection pre-tool hook,
`max_budget_per_run: 3.0` per run, and usage examples in their frontmatter.
Plugin-level hooks provide a session-start doctor and a Stop-time projection of
the `design-report.json` verdicts. The `circuit-library-guard` skill treats
KiCad/CERN libraries as read-only and enforces use of `register_*_library`.
Slash commands define argument hints.
