# ADR-0034: VibeBB Record Protocol v1, Sister Liaison Protocol v2, and the documentation split

## Status

Accepted

## Context

The VibeBB family is being rebuilt so sister plugins cooperate under
UX-creator's direction: every agent must leave auditable design-rationale
records (VRP v1), review images deliberately, answer liaison work orders
(SLP v2), and carry documentation a non-engineer can enter. The repo
previously had no record protocol, no liaison handling, an advisory 240-char
impression rule, and a bilingual README that mixed product and engineering
detail.

## Decision

- **VRP v1 adopted.** Typed writers in `src/circuit/records.py` append to
  `observations/circuit/*.jsonl`; shared stdlib hooks `_records.py` and
  `require_records.py` are byte-copies canonical across the family, pinned by
  normalized-AST sha256 in `check_shared_hooks.py` and never edited locally.
  `records-policy.json` binds impressions to real artifact globs and caps
  Stop-hook denials at 2.
- **Advisory 400/3 rule.** `advisory.impression_is_prose` delegates to
  `records.impression_is_prose` (≥400 chars, ≥3 sentences by `sentence_count`),
  replacing the 240-char/2-sentence rule; `humanrequest.agent_assessment`
  inherits it through the same validator. Every image review is mirrored into
  `vision-reviews.jsonl` by `review-record`.
- **SLP v2 local strict mirror.** `src/circuit/liaison.py` re-declares the
  UX-creator request/response schema (extra=forbid, no sister import) and
  implements `ux_inbox` states (stale > answered > blocked > new, malformed
  reported not raised) plus `ux_respond` refusal rules: `done` requires ≥1
  pass gate_verdict, ≥1 artifact, ≥1 decision_ref, ≥1 impression_ref — each
  checked separately — and fresh input hashes; missing inputs are omitted
  rather than hashed empty.
- **Blind lanes excluded from records.** `guard_author_lane.py` denies
  `CIRCUIT_AUTHORING_LANE=a|b` all `observations/circuit/` paths and the
  record/ux tools so shared logs cannot leak reasoning across lanes; the
  library agent records the comparison outcome after reveal.
- **Interchange shapes kept frozen.** `*.connectivity.json`,
  `*.envelope.json`, and `*.firmware.json` consumers are strict
  `extra="forbid"` sister mirrors, so no VRP fields were added to them; the
  reasoning binding travels in liaison responses instead.
- **Documentation split.** `README.md` is now a short bilingual product page
  (English + `## 日本語` in one file); technical content moved to `docs/`
  per-topic files with drift guards in `tests/test_docs.py` keeping tool,
  agent, skill, command, hook, and module names in sync with the docs.

## Consequences

- Design reasoning is auditable end-to-end and bound by sha256 to artifacts;
  records remain advisory and never gate request inputs or ERC/DRC verdicts.
- A `done` liaison answer is impossible without recorded reasoning and fresh
  gate verdicts, which makes stale or unverified claims detectable.
- Sub-agent hooks still do not inherit plugin-level hooks (see
  improvement-notes) — the Stop gate only fires for the top-level agent.
