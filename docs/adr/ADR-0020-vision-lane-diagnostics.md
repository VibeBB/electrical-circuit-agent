# ADR-0020 Vision-lane diagnostics in doctor

- Status: Accepted
- Date: 2026-09-23

## Decision

`python3 -m circuit.doctor` gains a `vision` check reporting the best
available vision lane as `vision=<lane>`:

- `model` — a vision-capable conversation model is configured
  (`$OPENHANDS_LLM_MODEL`/`$LLM_MODEL` matches a heuristic hint list:
  vision/kimi-k3/gpt-4o/gpt-4.1/gpt-5/claude/gemini/qwen-vl);
- `profile` — a dedicated vision agent profile is configured
  (`$OPENHANDS_AGENT_PROFILE`/`$CIRCUIT_VISION_PROFILE` contains
  "vision");
- `materialize-only` — the agent-canvas events dir is reachable
  (`$CIRCUIT_AGENT_EVENTS_DIR` or the default
  `~/.openhands/agent-canvas/dev_conversations`), so intake images can be
  materialized even though no vision model is set up;
- `none` — no lane detected.

The check is warn-level by contract: `none` yields `status: "warn"`, all
other lanes `"ok"`, and doctor's overall verdict only counts `fail`
entries — vision unavailability never fails the diagnostic. Present
`pdftoppm`/`rsvg-convert` binaries are listed in the detail line as
rasterizer availability.

`docs/operations.md` documents the model-capability matrix observed on
the deployment environment (`default` → kimi-k2.6 without vision;
`kimi-k3-vision` → kimi-k3 with vision) and the Canvas guidance: prefer
a vision agent profile for image-heavy sessions, or the `switch_llm`
pattern mid-session (ADR-0017 lane).

With P5 the phased plan is fully implemented, so
`docs/research/vision-deepening-plan.md` is removed — ADR-0016 through
ADR-0020 are the normative records.

## Rationale

Vision is strictly advisory in this repo, so a missing lane must never
surface as a doctor failure — but operators still need one place that
answers "can this environment see images, and through which lane?"
before an intake with a hand-drawn sketch or a review with renders goes
sideways. The lane vocabulary matches the four real configurations:
the model itself, a profile-level vision model, attachment
materialization without a vision reader, or nothing at all.

## Consequences

- `src/circuit/doctor.py` adds `_vision_probe`; `main()` computes the
  verdict as "no `fail` entries" so `warn` statuses exist without
  changing exit semantics (`--warn` still exits 0).
- Check results may now carry `status: "warn"` — consumers must not
  assume the binary ok/fail set.
- `docs/research/vision-deepening-plan.md` is deleted.
