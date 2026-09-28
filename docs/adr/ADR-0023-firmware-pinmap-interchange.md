# ADR-0023 Firmware pin map interchange

- Status: Accepted
- Date: 2026-09-28

## Context

firmware-agent assigns MCU pads to signals and peripherals. Those pads are
only correct if they are on the nets the circuit wires to them, at a safe
voltage, and no wired pad is left undriven. The circuit already has the
authoritative connectivity (design brief plus the `kicad-cli` netlist,
ADR-0009); firmware must not parse KiCad files itself, and VibeBB siblings
cooperate through workspace files rather than imports.

## Decision

- Brief parts may set `mcu: true`. The netlist parser keeps each node's
  KiCad `pinfunction`, so MCU pads are known by their datasheet names.
- `circuit_firmware_export` writes `<design>.firmware.json`
  (`circuit_firmware_connectivity`, schema 1): per MCU ref, every pin with
  package pin, pin function, net, signal class and voltage, and the
  sha256 of the brief and netlist.
- `circuit_firmware_check` validates firmware's `<name>.fw-pinmap.json`
  (`firmware_pinmap`, schema 1) against freshly derived connectivity and
  writes a `circuit_firmware_check` report. Unknown pads, net mismatches,
  power pins used as signals, over-voltage, unassigned signal pins, a
  supply net that misses the MCU and malformed input all fail. The report
  records the brief, netlist, pin map and firmware contract hashes.
- Both schemas are strict models duplicated on each side and versioned by
  `schema_version`; neither repository imports the other.

## Consequences

The pin map is confirmed only when this check and firmware's
`fw.netlist_match` both pass. Changing either side is detected by the
other on its next run, because each side re-reads the other's latest
artifact. The MCP surface grows
by two tools (`circuit_firmware_export`, `circuit_firmware_check`).
