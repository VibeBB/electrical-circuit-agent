# ADR-0010 Design brief intake provenance and library gate

- Status: Accepted
- Date: 2026-09-20

## Context

When an LLM generates a design brief from conversation, parts or nets that were
never mentioned and unverified dimensions can slip in. Also, selecting a
nonexistent `lib_id`, footprint, or pin based only on Konnect or LLM
descriptions surfaces uncertainty only after authoring has begun.

## Decision

An intake sidecar is attached to the design brief, recording requirements as
`R*`, explicit assumptions as `A*`, and unresolved items as `Q*`. The sidecar
is bound to the brief's SHA-256 and maps every part and net to a requirement or
assumption. A SHA mismatch, unmapped entries, an unknown source, or unresolved
open questions result in `blocked`.

Before authoring, the installed KiCad/CERN libraries' `.kicad_sym` and
`.pretty` files are parsed directly, and the existence of every symbol,
footprint, and pin referenced by each net is verified deterministically. Parse
failures or missing elements result in `fail`. Delegation to
`circuit-schematic` happens only when intake is `ready` and library
verification is `pass`.

Conversation text is not evidence for connectivity, ERC, or DRC. These verdicts
continue to rely solely on direct `kicad-cli` output.

## Consequences

- The brief and its conversation-derived basis can be audited mechanically.
- Parts/nets built only from assumptions can be presented to the user at review time.
- Nonexistent library elements are detected before Konnect authoring.
- A parser for the KiCad symbol format must be maintained; unknown formats are
  fail-closed.
