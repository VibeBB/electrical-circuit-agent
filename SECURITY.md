# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

## Reporting a vulnerability

Please do not open public issues for security vulnerabilities. Report them
via GitHub's private vulnerability reporting on this repository, or by
contacting the maintainer directly. Include:

- the affected version/commit,
- a minimal reproduction (design brief JSON, command, or payload),
- impact assessment if known.

You can expect an acknowledgement within a few days. We will coordinate a
fix and disclosure with you before publishing details.

## Scope notes

circuit executes KiCad, Konnect, and FreeRouting inside a Docker image and
exposes deterministic tools over a stdio MCP server. The container and the
fail-closed gates protect verdict integrity but are not a sandbox: do not
process untrusted KiCad projects, design briefs, or imported drawings in
environments where a crafted file could reach other tooling — EDA binaries
parse external CAD data as native code. The MCP server speaks stdio only
and never opens network listeners.

Secrets must never be written to logs, inputs, or commits; see the
invariants in [AGENTS.md](AGENTS.md).
