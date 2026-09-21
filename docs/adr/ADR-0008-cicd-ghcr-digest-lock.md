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
digest with the OpenHands SDK build.py of the version pinned in
`pyproject.toml` (currently v1.49.2). Lock updates are separated into bot PRs.
Because workflows do not start on GITHUB_TOKEN-driven events, the publish
workflow approves the pending `pull_request` run, gates the merge on an
explicit `workflow_dispatch` CI run plus the PR's own checks, then dispatches
post-merge main CI and the locked-image check; merge commits by the bot never
trigger push workflows otherwise. No lock file is created before the first
publish, and readers are fail-closed.

## Consequences

GHCR publishing, lock updates, the weekly locked-image-check, dependency update
Issues, and main-failure Issues are automated with GitHub Actions. The only
required secret is `GITHUB_TOKEN`, and a `fast` required check must be enabled
in the repository's branch protection settings.
