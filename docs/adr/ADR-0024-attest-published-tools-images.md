## SBOM attestations

When tools are published, the workflow generates an SPDX-2.3 SBOM for the
digest-pinned image, attests it with predicate type
`https://spdx.dev/Document/v2.3`, and uploads it for 30 days. The lock records
the returned URL as `sbom_attestation`; checks verify it when present and
warn when absent. SBOM steps are skipped with `skip_tools`.
# ADR-0024: Attest published tools images

- Status: Accepted
- Date: 2026-09-30

## Context

The `circuit_tools` lock pins the tools image by digest, but a digest alone
does not establish which repository workflow published it. The tools image is
used as the execution environment for KiCad and Konnect, so its publishing
identity should be verifiable before the locked image is pulled.

## Decision

The image publisher creates a GitHub build-provenance attestation for
`ghcr.io/vibebb/circuit-tools` and stores the returned attestation URL in the
root digest lock. The publisher mirrors the same entry into the plugin lock.
`locked-image-check.yml` verifies the digest with `gh attestation verify`,
requiring the circuit image publisher workflow as signer, before pulling the
image.

Locks without attestation metadata warn and continue for compatibility with
images published before this decision. If metadata is present but
verification fails, the image check fails. No attestation URL is added to the
committed locks unless a publisher run provides one.

## Consequences

Newly published tools images can be tied to this repository's publisher
workflow. Existing digest pins remain usable until a subsequent publish
creates an attestation; the separate `circuit_server` lock continues to
record its image digest independently.

## Launcher-side verification

`CIRCUIT_VERIFY_ATTESTATION` accepts `auto` (the default), `require`, or
`off`. Before pulling a lock-provided image, and on every `prewarm`, the
launcher uses `gh attestation verify` with the lock entry and publisher
workflow. `auto` prints one note and skips for an image override, missing
attestation, missing `gh`, or failed `gh auth status`; once verification
starts, failure or timeout prevents the pull. `require` makes skip conditions
errors, while `off` never verifies. Ordinary invocations do not re-verify a
locally present image, and `--warn` doctor paths never verify.
