---
description: Diagnose circuit-agent tools and runtime installation.
allowed-tools:
  - terminal
---

Run `python3 -m circuit.doctor` and report its JSON result without changing or
weakening any check. Use `python3 -m circuit.doctor --warn` for session startup
diagnostics where findings must not block the session.
