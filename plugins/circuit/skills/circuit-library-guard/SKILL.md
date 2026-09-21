---
name: circuit-library-guard
description: Protect the read-only KiCad and CERN library trees from edits.
version: 0.1.0
license: BSD-3-Clause
paths:
  - libraries/**
  - /opt/circuit/libraries/**
  - "**/cern-kicad-libs/**"
---

# Circuit library guard

The KiCad and CERN libraries are read-only inputs. Do not edit, delete, or
copy-modify files under `libraries/`, `/opt/circuit/libraries/`, or
`cern-kicad-libs/`.

The KiCad libraries are licensed under CC-BY-SA-4.0 with the KiCad exception.
The CERN libraries are licensed under CERN-OHL-P-2.0. Preserve their license
and provenance when using them.

Use the runtime `register_*_library` tools to register libraries. If a design
needs a deliberate derivative, create a project-owned artifact outside these
trees and document its provenance instead of changing the source library.
