# ADR-0008 GHCR publishing and digest-pinned lock

- Status: Accepted
- Date: 2026-09-20
- Related: ADR-0004, ADR-0007

## Context

The tools image and the OpenHands agent-server image must be distributed with
the same KiCad/Konnect/runtime boundary, and verification results must not
drift due to the mutability of `latest`. The SDK is not vendored; v1.49.2 is
fetched in CI and the standard server image build path is used.

## Decision

Publish `circuit-tools` and `circuit-server` to GHCR with a commit-derived tag
and a `latest` alias. The runtime source of truth is the digest-pinned
references in `docker/image-digests.json`. After publishing, the tools image is
pulled and smoke is re-run; the server image is built on top of the tools
digest with the OpenHands SDK v1.49.2 build.py. Lock updates are separated into
bot PRs that auto-merge only after their CI succeeds. No lock file is created
before the first publish, and readers are fail-closed.

## Consequences

GHCR publishing, lock updates, the weekly locked-image-check, dependency update
Issues, and main-failure Issues are automated with GitHub Actions. The only
required secret is `GITHUB_TOKEN`, but auto-merge and a `fast` required check
must be enabled in the repository's branch protection settings.
