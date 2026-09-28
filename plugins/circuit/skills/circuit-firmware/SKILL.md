---
name: circuit-firmware
description: Exchange MCU pin connectivity with firmware-agent and confirm its pin map against the circuit.
version: 0.1.0
license: BSD-3-Clause
triggers:
  - firmware
  - pin map
  - pinmap
  - MCU pins
  - ファームウェア
  - ピンマップ
---

# Circuit ↔ firmware cooperation

The circuit owns which MCU pad sits on which net; firmware-agent owns which
peripheral drives each pad. The two sides meet through JSON files in the
shared workspace (ADR-0023) and never import each other's code.

## Mark the MCU in the brief

Set `"mcu": true` on the MCU part in the design brief. Every part with
`mcu: true` is exported; a design without one cannot be exported.

## Export connectivity for firmware

`circuit_firmware_export` (CLI `circuit firmware-export --brief B --netlist N --out DIR`)
writes `<design>.firmware.json` (`artifact_kind: circuit_firmware_connectivity`):
per MCU ref, every pin with its package pin number, KiCad `pinfunction`
name (when a `kicad-cli` netlist is given), net, signal class and net
voltage, plus the sha256 of the brief and netlist it was derived from.
Pass the netlist whenever one exists: pad names such as `GPIO4` come from
the KiCad pin functions; without it only package pins and nets are known.
Re-export after any brief or schematic change — firmware treats a hash
mismatch as stale.

## Confirm the firmware pin map

firmware-agent writes `<name>.fw-pinmap.json` (`artifact_kind: firmware_pinmap`).
`circuit_firmware_check` (CLI `circuit firmware-check --brief B --pinmap P [--netlist N] [--out F]`)
rebuilds the connectivity and checks every entry. It fails when:

- the MCU ref is not an `mcu: true` part, or the pin map is malformed;
- a pad (by name, alias or package pin) is not on an MCU pin;
- the pad's net differs from the pin map's net;
- a signal is assigned to a power or ground pin;
- the net voltage exceeds the MCU's `max_io_v`;
- an MCU pin on a signal net is left unassigned by firmware;
- the firmware supply net does not reach the MCU.

The check always re-derives connectivity from the current brief and
netlist, so it judges the pin map against the circuit as it is now; the
report records the brief, netlist, pin map and firmware contract hashes.

A `pass` from this check and a `pass` of firmware's `fw.netlist_match`
together confirm the pin map. On a mismatch, decide which side changes:
fix the brief/schematic and re-export, or answer the firmware
`fw_request` so firmware-agent reassigns the pad. Never edit the firmware
contract or its generated files from the circuit side.
