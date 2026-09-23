# ADR-0008 GHCR publishing and digest-pinned lock

- Status: Accepted
- Date: 2026-09-20
- Related: ADR-0004, ADR-0007

## Context

The tools image and the OpenHands agent-server image must be distributed with
the same KiCad/Konnect/runtime boundary, and verification results must not
drift due to the mutability of `latest`. The SDK is not vendored; v1.49.4 is
fetched in CI and the standard server image build path is used.

## Decision

Publish `circuit-tools` and `circuit-server` to GHCR with a commit-derived tag
and a `latest` alias. The runtime source of truth is the digest-pinned
references in `docker/image-digests.json`. After publishing, the tools image is
pulled and smoke is re-run; the server image is built on top of the tools
digest with the OpenHands SDK build.py whose version follows the project pin
(currently v1.49.4). Lock updates are separated into bot PRs. Because
GITHUB_TOKEN events do not start workflows, the publish workflow dispatches
the lock-branch CI itself and merges the PR synchronously once that run
succeeds; when the Actions policy lands the bot PR's `pull_request` run as
`action_required`, the workflow approves it via the Actions API so its
required checks can run. The post-merge main CI and locked image check are
dispatched as observational runs. No lock file is created before the first
publish, and readers are fail-closed.

## Consequences

GHCR publishing, lock updates, the weekly locked-image-check, dependency update
Issues, main-failure Issues, and zizmor workflow lint are automated with
GitHub Actions. The only required secret is `GITHUB_TOKEN`, but `fast` must
be a required check in the repository's branch protection settings and the
Actions policy must let the publish workflow approve and dispatch its own
runs.
