# Skills

Ten skills under `plugins/circuit/skills/`; keyword-triggered skills stay
model-invocable, `paths:`-triggered skills act as deterministic rules.

| Skill | Purpose |
|---|---|
| `circuit-brief` | Guide the brief/intake contract when turning conversation into a design brief |
| `circuit-brief-rules` | Path-triggered rule: brief/intake JSON authoring conventions |
| `circuit-firmware` | Firmware connectivity export and pin-map check workflow (ADR-0023) |
| `circuit-konnect` | Konnect MCP usage: toolsets, IPC socket, `circuit_konnect_call` ops batching |
| `circuit-libraries` | KiCad library policy: reuse, provenance, import rules |
| `circuit-library-authoring` | Evidence-backed PartSpec authoring: datasheet extraction, vision reads, checks |
| `circuit-library-guard` | Path-triggered rule protecting `libraries/` against unverified edits |
| `circuit-out-rules` | Path-triggered rule on `**/out/**`: generated artifacts are read-only projections — change the brief and regenerate (`protect-libraries` enforces) |
| `circuit-verification` | ERC/DRC/connectivity gate discipline: kicad-cli JSON only, fail-closed |
| `circuit-workflow` | Stage-by-stage workflow: delegation order, gates, Records (VRP), Vision points, SLP |
