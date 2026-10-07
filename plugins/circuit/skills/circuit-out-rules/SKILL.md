---
name: circuit-out-rules
description: Path rule — generated-artifact reminders injected whenever a file under out/ is touched.
version: 0.1.0
license: BSD-3-Clause
paths:
  - "**/out/**"
---

# Circuit generated-artifact rules

Files under `out/` are projections of the design brief, written only by the
deterministic authoring pipeline inside the pinned `circuit-tools` image.

- Never edit files under `out/` by hand — change the brief or intake and
  regenerate. The `protect-libraries` hook blocks protected design writes
  anyway; do not try to work around it.
- Pass/fail verdicts come only from the deterministic gates (ERC, DRC,
  connectivity); treat any text or LLM judgement about these files as
  advisory, never as a verdict.
- To change a generated artifact, edit the source of truth
  (`*.brief.json`/`*.intake.json`) and re-run the authoring or gate command.
