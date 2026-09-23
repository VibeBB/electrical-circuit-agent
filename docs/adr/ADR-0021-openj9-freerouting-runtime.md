# ADR-0021 Semeru OpenJ9 and FreeRouting runtime in the tools image

- Status: Accepted
- Date: 2026-09-23

## Context

Konnect v0.12.1 ships a Specctra/FreeRouting tool family —
`check_freerouting`, `export_specctra_dsn`, `route_specctra_dsn`,
`plan_specctra_ses_import`, and `apply_specctra_ses` — that requires a Java
runtime and `freerouting.jar`. The tools image bundled neither, so the
coverage matrix recorded the requirement as deferred
(`docs/konnect-tools.md`). The deferral has now been lifted: the image must
carry a JRE and the JAR so Konnect can drive FreeRouting as a `java -jar`
subprocess over MCP stdio.

## Decision

1. Bundle IBM Semeru Runtime Open Edition 27.0.0.0 (Eclipse OpenJ9 0.62.0)
   in the tools image. The release tarball is pinned by version and SHA-256
   (`SEMERU_JRE_VERSION`, `SEMERU_JRE_SHA256`), extracted to `/opt/jre`, and
   resolved via `JAVA_HOME=/opt/jre` and `PATH`. The build probes that
   `java -version` reports Eclipse OpenJ9 and the pinned Semeru version.
2. Bundle `freerouting-2.4.1.jar`, pinned by `FREEROUTING_VERSION` and
   `FREEROUTING_SHA256`, at `/opt/freerouting/freerouting.jar`. This path is
   one of Konnect's built-in JAR search roots (`/opt/freerouting`, depth 3),
   so `check_freerouting` finds the engine without configuration. The build
   probes the banner `Freerouting v2.4.1` via a headless
   `-Djava.awt.headless=true` launch.
3. No global JVM tuning (`JAVA_TOOL_OPTIONS`, shared class cache, heap
   limits) is set. Konnect spawns the JVM itself as
   `java -jar <jar>` in MCP-stdio mode and does not consume a wrapper
   script, so a `freerouting` CLI wrapper like the one acd-agent uses is not
   installed. OpenJ9's measured footprint/startup advantages
   (acd-agent ADR-0045: RSS 1140→358 MB, 98→77 s) apply to its batch CLI
   mode; the MCP-mode JVM stays resident and its flags are
   Konnect-controlled, so tuning is revisited only if a direct batch-mode
   pipeline is adopted.
4. `check_freerouting` moves out of the deferred state: the smoke script
   loads the `integration` toolset and asserts `engine_found`,
   `java_available`, and `native_mcp_available`.
5. The DSN/SES round-trip stays gated by an upstream parser gap, not by the
   runtime: Konnect v0.12.1's `export_specctra_dsn` reads footprint
   positions as `(at x y)`, while boards written by KiCad 11 nightly
   serialize `(transform (translate x y) (rotate r) (scale sx sy))`. A
   container PoC against a KiCad-11-saved board (and against Konnect's own
   fixture after a `save_project` rewrite) fails with
   `footprint has no position`. `export_specctra_dsn`,
   `route_specctra_dsn`, `plan_specctra_ses_import`, and `apply_specctra_ses`
   therefore remain conditional until upstream parses `transform`.

## Consequences

- The autorouting runtime prerequisite is satisfied in the image;
  `check_freerouting` is green and is covered by the image smoke test and
  the digest-lock metadata probe (`semeru_jre`, `freerouting` keys).
- The DSN → route → SES-apply chain cannot produce routed output on
  KiCad-11-saved boards today. Enabling it requires either an upstream
  Konnect change to parse `(transform ...)`, or bypassing Konnect with a
  repository-owned DSN writer/SES reader (the approach acd-agent takes).
  That decision is left to a follow-up; the coverage matrix records the
  blocker per tool.
- FreeRouting is GPL-3.0 and Semeru is EPL-2.0/Apache-2.0/GPL-2.0+CPE; both
  ship as unmodified release artifacts executed as subprocesses, so no
  GPL/AGPL import-link into the Python package occurs. Attribution is
  recorded in `THIRD_PARTY_NOTICES.md` and `/usr/share/doc/` inside the
  image.
- The weekly dependency check now reports FreeRouting and Semeru release
  candidates; a Semeru major-series migration is manual
  (`docs/operations.md`).
