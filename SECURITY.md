# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

## Reporting a vulnerability

Please [report vulnerabilities privately on GitHub](https://github.com/VibeBB/electrical-circuit-agent/security/advisories/new).
Do not include vulnerability details in a public issue, pull request, or discussion.

Include:

- the affected version/commit,
- a minimal reproduction (design brief, command, or payload),
- impact assessment if known.

You can expect an acknowledgement within a few days. We will coordinate a
fix and disclosure with you before publishing details.

## Scope notes

circuit executes KiCad CLI verification and authoring locally; Konnect runs
as an unmodified subprocess. The fail-closed gates defend verdict integrity
but are not a sandbox: do not run untrusted briefs or library files in
environments where crafted EDA assets could reach other tooling. The MCP
server speaks stdio only and never opens network listeners.
