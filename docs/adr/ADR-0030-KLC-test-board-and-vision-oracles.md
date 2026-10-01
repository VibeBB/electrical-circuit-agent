# ADR-0030 KLC, test-board, and vision oracles

## Status

Accepted

## Context

Library verification needs independent checks for KiCad library conventions,
KiCad's interpretation of generated board data, and visual correspondence
between datasheet drawings and library artifacts. Each oracle has a distinct
authority and licensing boundary.

## Decision

- KiCad Library Convention checks use the unmodified, pinned
  `kicad-library-utils` source only as a subprocess. Runtime code does not
  import or copy GPL code. Results are read from upstream JUnit XML; colored
  stdout is never parsed. Missing tools or unusable reports fail closed.
- F5, F6, F7, and S4 functional findings are errors. Naming and metadata
  findings are warnings. The checker commit and license are recorded in the
  tools image and third-party notices.
- Test-board verification creates a project-local schematic and PCB with
  local library tables, runs KiCad CLI oracles, and checks net mapping, pad
  readback, DRC, assembly attributes, paste coverage, exposed-pad apertures,
  mask web, and ERC. `unconnected_items` is the only expected DRC exception;
  ERC findings remain warnings. A missing KiCad tool is an error.
- Footprint and symbol vision comparisons bind the exact PartSpec and
  artifact hashes. A mirrored control must be detected. Vision mismatches
  remain warnings and cannot clear deterministic errors; every mismatch
  creates a blind human yes/no question.
- Review approval is gated on valid, hash-bound `vision_review` records with
  valid impressions for all packet-listed overlay and comparison images.

## Consequences

The GPL checker remains process-isolated, while the test-board and vision
oracles add independent evidence without promoting model judgments over
deterministic checks. Missing evidence or unavailable deterministic oracles
cannot silently pass verification.
