# Improvement notes

## Implemented in this change

- VRP v1 ported: shared hooks (`_records.py`, `require_records.py`),
  `records-policy.json`, `src/circuit/records.py`, record MCP/CLI tools,
  protect-libraries deny rules, blind-lane record isolation, prompts in all
  agents + workflow skill.
- Vision coverage: advisory delegates to the 400-char/3-sentence rule,
  `review-record` mirrors into `vision-reviews.jsonl`, new checklists
  (`stackup`, `diff`, `intake_image`), `circuit_stackup` PNG inline,
  `IMAGE_TOOLS` frozenset mirrored into hook matcher + OBSERVED_TOOLS.
- SLP v2: strict local `liaison.py` mirror, `circuit_ux_inbox`/
  `circuit_ux_respond`, `ux` CLI subcommands, inbox states
  (new/answered/stale/blocked + malformed), done-evidence refusals.
- Version alignment (0.1.0), AGENTS.md family count (11) + VRP/SLP sections,
  operations.md required-check list, README EN+JA, docs split.

## Remaining ideas

- Plugin-level hooks do not propagate to SDK sub-agents, so the VRP Stop gate
  (require-records) only fires for the top-level agent; sub-agents would need
  the hook declared in each AgentDefinition frontmatter to enforce it there.
- Interchange shapes (`*.connectivity.json`, `*.envelope.json`,
  `*.firmware.json`) are frozen by strict `extra="forbid"` sister mirrors, so
  no VRP refs can live inside them — a family-level contract versioning scheme
  (schema_version + tolerated unknowns) would unlock attaching
  decision_refs/sha256 bindings later.
- UX job ids in high-risk request rationales cannot be validated locally;
  only non-emptiness is enforced.
- The ci.yml `fast (3.14)` and 3.15 canary legs are non-required — document
  (or gate) whether their failures are informational.
- `test_cicd_scripts.py` pins `circuit=0.0.1` in image-tool fixtures; will
  drift at the next bump — match a version pattern instead.
- hooks.json command strings are duplicated per hook; a resolver convention
  would reduce copy errors when adding hooks.
- `records-policy.json` artifact_globs enumerate suffixes manually; a
  generator test asserting every writer's suffix is covered would catch drift.
- Pillow `Image.getdata` deprecation in `test_visionread.py` — switch to
  `get_flattened_data` before Pillow 14.
- `payload`-dict list access in `ux_respond` needed casts for pyright strict;
  a shared `payload_list()` helper would serve all sisters.
