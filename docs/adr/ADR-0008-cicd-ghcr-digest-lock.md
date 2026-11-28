# ADR-0008 GHCR publishing and digest-pinned lock

- Status: Accepted
- Date: 2026-09-20
- Related: ADR-0004, ADR-0007

## Context

The tools image and the OpenHands agent-server image must be distributed with
the same KiCad/Konnect/runtime boundary, and verification results must not
drift due to the mutability of `latest`. The SDK is not vendored; the pinned version is
fetched in CI and the standard server image build path is used.

## Decision

Publish `circuit-tools` and `circuit-server` to GHCR with a commit-derived tag
and a `latest` alias. The runtime source of truth is the digest-pinned
references in `docker/image-digests.json`. After publishing, the tools image is
pulled and smoke is re-run; the server image is built on top of the tools
digest with the OpenHands SDK build.py whose version follows the project pin
(currently v1.50.1). Lock updates are separated into bot PRs. The bot PR's
`pull_request` run supplies the required checks. If GitHub holds that run as
`action_required`, the publish workflow approves the run via the Actions
API; that approval is not a PR review. A lock PR that touches a path in
`.github/CODEOWNERS` waits for manual code-owner approval from
`@uist1idrju3i`, even after the required checks pass. Auto-merge can
complete once both requirements are met. The owner's bypass is PR-only;
the bot has no ruleset bypass. The post-merge main CI and locked image check are
dispatched as observational runs. No lock file is created before the first
publish, and readers are fail-closed.

## Consequences

GHCR publishing, lock updates, the weekly locked-image-check, dependency update
Issues, main-failure Issues, and zizmor workflow lint are automated with
GitHub Actions. The only required secret is `GITHUB_TOKEN`. Keep `fast` as
a required check and require code-owner review in the `main` ruleset. The
Actions policy must let the publish workflow approve held runs and dispatch
the post-merge runs.
