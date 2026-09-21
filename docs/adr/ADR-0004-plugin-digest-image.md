# ADR-0004 Plugin and digest-pinned Docker image

- Status: Accepted
- Date: 2026-09-20
- Related: `docker/circuit-tools.Dockerfile`

## Context

The plugin must be distributed to OpenHands environments, and we do not want
the combination of KiCad, libraries, and Konnect to vary per host.

## Decision

In PR1, the plugin source and the tools image are separated. The tools image
pins KiCad nightly, Konnect, and libraries. A server image built by the SDK
agent-server build is introduced in PR3. The image digest lock is also added
in PR3.

## Consequences

Local smoke and CI can use the same tools image. Because Docker itself does not
guarantee determinism, digests are locked separately after publishing.
