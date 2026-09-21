# ADR-0003 Adoption of Konnect and the AGPL boundary

- Status: Accepted
- Date: 2026-09-20
- Related: `THIRD_PARTY_NOTICES.md`

## Context

Konnect already implements KiCad file editing and live IPC, but it is
AGPL-3.0-only. AGPL code must not be import-bound into the circuit agent's
BSD-3-Clause code.

## Decision

Konnect v0.12.1 is bundled into the image as an unmodified binary and launched
as a separate MCP stdio process. The LICENSE, source URL, release commit, and
asset SHA-256 are recorded. Where IPC is reachable, live operations are used;
Konnect's limited closed-board fallback when IPC is absent is used as-is.

## Consequences

The process boundary avoids AGPL import into the Python package. Distributed
artifacts include the Konnect LICENSE and provenance information.
