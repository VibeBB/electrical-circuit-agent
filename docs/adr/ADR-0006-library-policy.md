# ADR-0006 Library policy

- Status: Accepted
- Date: 2026-09-20
- Related: `libraries/README.md`

## Context

Design reproducibility requires pinned provenance for the official KiCad
libraries and the CERN KiCad libraries.

## Decision

Official symbol/footprint packages are used via KiCad PPA package pins, and the
CERN KiCad libraries are pinned to an upstream commit as a shallow submodule.
Both are used unmodified, and provenance is documented.

## Consequences

Third-party library licenses and sources can be tracked. When updating the
submodule, the commit, date, license, README, and notices are updated in the
same change.
