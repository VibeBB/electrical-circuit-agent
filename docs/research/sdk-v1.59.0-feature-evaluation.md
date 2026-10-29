# OpenHands SDK v1.59.0 feature evaluation (electrical-circuit-agent)

Scope: `openhands-sdk` and `openhands-tools` move from 1.55.1 to 1.59.0
(upstream releases `v1.56.0`, `v1.57.0`, `v1.58.0`, `v1.58.1`, `v1.58.2`,
`v1.59.0`). The complete upstream range `v1.55.1..v1.59.0` was reviewed. In
the same change `uv` moves 0.13.1 to 0.13.6, `pytest` moves 9.1.1 to 9.2.0,
`ruff` moves to 0.17.3, `step-security/harden-runner` moves v2.22.1 to
v2.22.3, `astral-sh/setup-uv` moves v10.3.0 to v10.3.3,
`github/codeql-action` moves v4.39.0 to v4.39.1, `docker/login-action`
moves v4.6.0 to v4.7.0, and the pinned `trivy` version moves 0.76.0 to
0.76.1.

Primary sources:
[v1.56.0 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.56.0),
[v1.57.0 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.57.0),
[v1.58.0 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.58.0),
[v1.58.1 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.58.1),
[v1.58.2 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.58.2),
[v1.59.0 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.59.0).

## SDK 1.55.1 -> 1.56.0

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| #6009 fix(marketplace): load skills from registered repositories without a manifest | adopted implicitly | More lenient plugin/skill install-source resolution; matches how VibeBB plugins are installed from repo paths. |
| #5955 #5842 #5780 #5779 #5772 #5769 #5768 #5778 #5826 #5968 #5973 #5954 #5953 #6046 #5950 #5966 #5965 #5848 #5864 #5875 #5877 #5898 #5916 #5917 #5951 #5957 #5958 #5964 #5967 #5867 #5868 #5802 #5840 #5851 #5874 agent-server robustness and correctness batch | adopted implicitly | Concurrency, paging, streaming, webhook, profile-store and bash-route hardening; benefits the deployment without plugin changes. |
| #5901 fix(sdk): reject skill installs with unparsable frontmatter as invalid | adopted implicitly | Fail-closed skill parsing matches the plugin family convention. |
| #5885 fix(sdk): drop Bedrock-only ids from the supported model list | not applicable | Provider list hygiene; VibeBB does not configure Bedrock. |
| #5884 fix(sdk): expand ~ in OH_PERSISTENCE_DIR | adopted implicitly | Path handling fix for the meta-profile store. |
| #6072 fix(sdk): add ghu_/ghs_/gho_ GitHub token patterns to redact_api_key_literals | adopted implicitly | Secret-hygiene hardening; complements the repo's own redaction rules. |
| #6125 fix(agent-server): align hybrid UUID routing | adopted implicitly | Routing correctness. |
| #6036 #6038 test(macOS)/tmux path fixes | not applicable | Upstream test/tooling internals. |

## SDK 1.56.0 -> 1.57.0

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| #6098 feat(agent-server): list the skills a profile launch can load | available, not adopted | Read-side API for profile launches; VibeBB profiles are seeded by `ensure_agent_profiles.py` and do not need it. |
| #6112 fix(terminal): run agent terminals below the server's CPU priority | adopted implicitly | Keeps the agent-server responsive under load. |
| #6031 fix(marketplace): warn what a source without a manifest leaves out | adopted implicitly | Clearer diagnostics for manifest-less sources. |
| #5977 feat(agent-server): per-conversation runtime choice and runtime settings | available, not adopted | Runtime selection is a deployment/server concern; plugins stay runtime-agnostic. |
| #5976 fix(agent-server): collect plugin format schemas in PyInstaller bundle | adopted implicitly | Bundled-server packaging fix. |
| #5343 refactor(sdk): declare lifecycle capabilities on base classes | adopted implicitly | Internal refactor; no API change for plugins. |
| #5685 fix(sdk): reject unavailable cached extension refs | adopted implicitly | Fail-closed extension resolution. |
| #5489 fix(logging): bound Rich exception traversal | adopted implicitly | Logging robustness under deep exception chains. |
| #4564 feat(sdk): support custom title generation prompts | available, not adopted | Server-side launch option; not consumed by plugins. |
| #4392 fix(agent-server): surface _emit_event_from_thread executor failures | adopted implicitly | Failure surfacing fix. |
| #5758 fix(agent-server): support cipher key rotation and refuse writes under a lost key | adopted implicitly | Security: prevents silent writes under a rotated key. |
| #6121 docs(examples): fix stale paths | not applicable | Documentation only. |

## SDK 1.57.0 -> 1.58.0

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| #6101 feat(llm): add minimax-m3.1-flash to verified models | available, not adopted | Model list entry; LLM credentials live in the deployment. |
| #5557 fix(agent-server): forward OTEL/Laminar env vars into conversation containers | available, not adopted | Telemetry passthrough for the server deployment. |
| #5908 fix(tools): find Playwright Chromium under PLAYWRIGHT_BROWSERS_PATH | adopted implicitly | Browser tool robustness inside agent-server. |
| #6145 fix(mcp): register OAuth clients with a supported auth method and send Basic credentials once | adopted implicitly | MCP OAuth correctness; relevant because plugins expose MCP servers. |
| #6135 Deliver SessionStart hook additionalContext to the agent | adopted implicitly | SessionStart hooks can now inject context; VibeBB plugins already declare SessionStart hooks and get the behavior for free. |
| #6138 fix(mcp): keep stored OAuth credentials when a conversation's token refresh fails | adopted implicitly | Credential-lifecycle hardening. |
| #6147 feat(workspace): clone Azure DevOps repositories | not applicable | VibeBB hosts on GitHub only. |
| #6148 fix(tools): apply the sanitized environment to tmux terminal shells | adopted implicitly | Security: prevents env leakage into terminals. |
| #5770 fix(sdk): keep discriminated-union validation from mutating its input | adopted implicitly | Validation-purity fix; protects byte-deterministic artifacts. |
| #5927 #5892 #5865 #5857 #5854 #5827 #5818 #5803 #5798 #5786 #5781 #5887 #5906 #5876 #5832 #5817 #5821 #5814 #5943 agent-server/SDK correctness batch | adopted implicitly | Meta-profile, credential-binding, archive, webhook and lifecycle fixes. |

## SDK 1.58.0 -> 1.58.1 / 1.58.2

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| #6162 fix(security): distrust compromised eMudhra/emSign CAs (certifi/pip/uv) | adopted implicitly | CA-bundle security fix; lands with the pin. |
| #6165 fix(llm): send OpenRouter session_id for sticky routing | adopted implicitly | Provider affinity fix. |
| #6029 fix(sdk): forward stored OAuth2 MCP credentials to ACP agents | adopted implicitly | MCP credential propagation fix. |
| #6140 fix(terminal): stop waiting on a running command once the terminal is closed | adopted implicitly | Terminal lifecycle fix. |
| #6167 fix(security): pin Debian snapshot so system ca-certificates distrusts emSign G3 | adopted implicitly | Upstream image hardening; VibeBB pins its own base digests, so this only matters transitively. |
| #6202 fix(security): update locked Zod to 4.6.7 | not applicable | TypeScript-side dependency. |
| #6141 chore(deps): enforce seven-day third-party release-age policy | noted | Upstream CI policy; VibeBB's dependabot already applies a 7-day cooldown. |
| #6200 fix(security): ship Node.js fixed for CVE-2026-111497 (Brotli) in agent-server images | adopted implicitly | Security fix inside upstream images. |

## SDK 1.58.2 -> 1.59.0

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| #6188 fix(sdk): preserve embedded skill source subpaths | adopted implicitly | Skill install-source fidelity. |
| #6189 fix(llm): reject structured quota exhaustion during profile validation | adopted implicitly | Fail-closed profile validation. |
| #6229 fix(agent-server): allow child launches in fresh workspaces | adopted implicitly | Fixes sub-agent launches in clean workspaces. |
| #6187 fix(typescript): include public skills in agent profile types | not applicable | TypeScript client typing. |
| #6192 fix(verify-agent-server): gate fresh runs on health and refresh API recipes | available, not adopted | Upstream verification tooling; not consumed here. |
| #6153 fix(launch): keep client-supplied skills on ACP agents under native sourcing | adopted implicitly | Skill propagation fix. |
| #6171 feat(acp): expose skills to every ACP agent as files plus a path catalog | available, not adopted | ACP surface; plugins do not run through ACP. |
| #6118 fix(sdk): upgrade Codex ACP to 2.2.2 | adopted implicitly | Bundled agent update. |
| #5866 fix(agent-server): answer replayed MCP OAuth callbacks with the job status | adopted implicitly | OAuth flow hardening. |
| #5794 fix(agent-server): keep the server responsive while a profile store lock is held | adopted implicitly | Lock-scope reduction. |
| #6155 fix(sdk): never secret-mask event and tool-call ids | adopted implicitly | Preserves provenance ids in records — aligns with the family's audit rules. |
| #6213 fix(skills): expose path-trigger paths in SkillInfo | adopted implicitly | Surfaces `paths:` metadata; VibeBB skills use path-triggered rules, so this improves introspection. |
| #6193 feat(profiles): add integration_refs to AgentProfile | available, not adopted | New optional field; `ensure_agent_profiles.py` needs no change. |
| #6257 #6279 feat: per-conversation cost budget set/read | available, not adopted | Server-side budgeting API; no plugin consumer today. |
| #6274 fix(llm): honor provider retry delays without retrying early | adopted implicitly | Retry correctness. |
| #6259 fix(sdk): preserve event timestamps during secret masking | adopted implicitly | Record fidelity fix. |
| #6157 fix(agent-server): expand secret references in the MCP connection test | adopted implicitly | Test-path secret resolution. |
| #6197 fix(sdk): keep a conversation loadable when an event file is missing | adopted implicitly | Fault-tolerant persistence. |
| #6260 Fix/tmux utf8 locale sdk path | adopted implicitly | Terminal locale fix. |
| #6286 chore(sdk): remove ACP curated model lists past their 1.59.0 deadline | adopted implicitly | Dead-list cleanup; no action needed. |
| #6243 docs: refresh AGENTS.md guidance | not applicable | Documentation only. |

## Outcome

No breaking change for this repository's usage surface (`TaskToolSet`,
`AgentDefinition`, skill `triggers:`/`paths:` frontmatter, hook
declarations, plugin manifest formats). All `adopted implicitly` items land
with the pin bump; `available, not adopted` items are recorded for future
consideration. The `mcp>=1.29,<2` deferral stays in place because
openhands-sdk 1.59.0 still requires `fastmcp>=3.2.0,<4`, which requires
`mcp<2.0`.
