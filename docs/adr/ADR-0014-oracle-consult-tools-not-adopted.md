# ADR-0014 Oracle and consult tools not adopted

## Status

Accepted

## Context

The SDK ships two consult-style tools beyond the task/delegate boundary:

- `ask_oracle` (`AskOracleTool`): a one-shot, tool-less sub-agent that asks a
  saved `oracle` LLM profile for advice on the current context.
- `tom_consult` (`TomConsultTool`): consults a user-modeling agent that builds
  a persistent model of the user from indexed conversations.

Both return free-form advice to the agent that invoked them.

## Decision

Neither tool is adopted in this repository.

- `ask_oracle` returns non-deterministic, unprovenanced advice. The circuit
  agents' contract already channels all model judgement through declared
  lanes whose outputs are either deterministic gates (ERC/DRC via
  `kicad-cli` JSON) or explicitly advisory records (ADR-0012, ADR-0013) with
  recorded provenance. An advice call that does not record what was asked, to
  which profile, and what it produced cannot be cross-checked, so the
  existing advisory vision lane and explicit review steps cover the need.
- `tom_consult` builds a persistent user model across conversations — a
  stateful side channel outside the plugin's hook and provenance boundary,
  and orthogonal to what these agents need.

If a future need is a second-opinion review step, revisit `ask_oracle` with
the same provenance requirements as the vision advisory lane (recorded
profile, prompt, response hash) before adopting.

## Consequences

- No new tools are registered; the agent surface stays limited to the
  declared circuit MCP tools plus SDK task/file-editor tools.
- Users lose nothing the plugin promised: advisory review uses the recorded
  vision lane; verdicts stay with deterministic gates.
