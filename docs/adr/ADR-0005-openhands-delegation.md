# ADR-0005 SDK delegation mechanism

- Status: Accepted
- Date: 2026-09-20
- Related: `https://docs.openhands.dev/sdk/guides/task-tool-set`

## Context

Schematic, board, and review have different responsibilities. The OpenHands SDK
provides TaskToolSet and AgentDefinition, so no custom delegation
infrastructure needs to be added.

## Decision

Adopt `TaskToolSet`, `AgentDefinition`, and `TaskTrackerTool`.
`WorkflowToolSet` is deferred, and the deprecated `DelegateTool` is not used.

## Consequences

We can rely on the SDK's standard delegation path. The rationale is
<https://docs.openhands.dev/sdk/guides/task-tool-set> and
<https://github.com/OpenHands/software-agent-sdk/issues/2725>.
