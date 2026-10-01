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
