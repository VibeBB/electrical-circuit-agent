# Performance and limits

Real constants from the code (grep the named symbols for the current values).

## Image and vision caps

| Constant | Value | Meaning |
|---|---|---|
| `mcp_collect._MAX_INLINE_IMAGES` | 4 | max inline `ImageContent` attached per tool result |
| `mcp_server._MAX_VISION_IMAGES` | 8 | cap for vision-tool image payloads |
| vision_read requests | 1–7 per batch | `visionread` enforces `1 <= len(requests) <= 7` |
| datasheet/render DPI | default 300; datasheet extraction allows 72–1200 | `datasheet.extract_datasheet`, kicad_cli renders |
| footprint/symbol library renders | 300 dpi (symbol), geometry-derived dpi ≥72 (footprint) | `libraryvision` |

## Timeouts

| Constant | Value | Where |
|---|---|---|
| api-server start | 30 s | `apiserver.start(timeout_s=30.0)` |
| api-server stop | 10 s | `apiserver.stop(timeout_s=10.0)` |
| client probe | 0.2 s socket timeout | `apiserver` readiness poll |
| doctor probes | 30 s | `doctor._PROBE_TIMEOUT_S` |
| kicad-cli runs | 600 s default | `kicad_cli.run(timeout_s=600.0)` |
| datasheet fetch/extract | 120 s | `datasheet` subprocess |
| KLC checker | 300 s | `klc.run_klc` |

## Records (VRP) minimums

| Rule | Value | Where |
|---|---|---|
| impression length | ≥400 chars, ≥3 sentences (`sentence_count` — "3.3 V" does not end a sentence) | `records.IMPRESSION_MIN_CHARS=400`, `IMPRESSION_MIN_SENTENCES=3` |
| decision rationale | ≥200 chars | `records.RATIONALE_MIN_CHARS` |
| stop-hook retries | `max_stop_denials: 2` | `records-policy.json` |
| SLP reason | ≥20 chars unless accepted/in_progress | `liaison` |
| SLP purpose | ≥20 chars | `liaison` |
| SLP request id | slug `^[a-z0-9][a-z0-9._-]{0,63}$` = file stem | `liaison` |

## Agent budgets

`max_iteration_per_run`: 30 (40 for circuit-library);
`max_budget_per_run`: 3.0 for every agent.

## Corpus

The sealed golden corpus lives under `library/corpus/` (manifest + truth
entries, ~50 JSON files); author lanes cannot read or score it
(`corpus.CorpusError`).

## Structural limits

- One api-server binds one `.kicad_pcb`; switch boards = stop + start.
- Inline images: `circuit_render` board3d writes one image; schematic/layers
  kinds attach up to 4; `circuit_rasterize` attaches up to 4.
- `circuit_stackup` fail-closes when SVG→PNG rasterization fails.
- Blind lanes (a|b) are denied `observations/circuit/` paths and all
  record/ux tools.
