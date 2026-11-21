# OpenHands SDK v1.63.1 feature evaluation (circuit)

Scope: `openhands-sdk` and `openhands-tools` move from 1.62.0 to 1.63.1
(upstream releases `v1.63.0`, `v1.63.1`). The complete upstream range
`v1.62.0..v1.63.1` was reviewed. In the same change `uv` moves 0.13.8 to
0.13.9, `ruff` floats to 0.17.5-0.17.6 in the lock, `pydantic` floats to
2.14.4, `step-security/harden-runner` moves to v2.23.0,
`astral-sh/setup-uv` moves to v10.4.0, `github/codeql-action` moves to
v4.39.4, `actions/dependency-review-action` moves to v5.0.1, and
`ossf/scorecard-action` moves to v2.6.0. The `filelock>=4.2.1`
`override-dependencies` entry is dropped because litellm 1.108.3 (now
pinned by the SDK's constraint-dependencies) allows `filelock<5`, so the
lock already resolves filelock 4.2.12. In the same change the KiCad nightly package moves `202611161144+2f8847e57d~190` to `202611201144+4f26951493~190`, kicad-nightly-symbols moves `202611121241+9fa442824~12` to `202611200121+2f6414dda~12`, the CERN KiCad libraries submodule moves `f5445d61` to `f793f03a`, and the publish workflow's litellm re-resolve needle moves `==1.105.1` to `==1.108.3` (the new SDK constraint-dependencies pin).

Primary sources:
[v1.63.0 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.63.0),
[v1.63.1 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.63.1),
[uv changelog](https://github.com/astral-sh/uv/blob/main/CHANGELOG.md),
[ruff releases](https://github.com/astral-sh/ruff/releases),
[pydantic releases](https://github.com/pydantic/pydantic/releases),
[harden-runner releases](https://github.com/step-security/harden-runner/releases),
[setup-uv releases](https://github.com/astral-sh/setup-uv/releases),
[codeql-action changelog](https://github.com/github/codeql-action/blob/main/CHANGELOG.md),
[dependency-review-action releases](https://github.com/actions/dependency-review-action/releases),
[scorecard-action releases](https://github.com/ossf/scorecard-action/releases).

## SDK 1.62.0 -> 1.63.0

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| feat(agent-server): experimental features the user can turn on or off (#6759) + Conversation reactions + factories flag (#7016, v1.63.1) | available, not adopted | Agent-server-side experimental gating and the first experiment (reactions). VibeBB plugins run inside OpenHands, not a self-hosted agent-server; no plugin wiring references experiments. |
| feat(sdk): expose restart recovery provenance and outcome dispositions | adopted implicitly | Better observability for the resume path plugins rely on. |
| fix(sdk): drop a since-disabled ambient plugin's skills and MCP servers on resume (#5756) | adopted implicitly | Prevents stale skills/MCP servers leaking into a resumed session when a plugin was disabled. Directly relevant to every VibeBB plugin. |
| fix(sdk): keep one copy of a plugin's hooks when it is loaded twice (#5815) | adopted implicitly | Hooks dedupe for double-loaded plugins; the shared hook contracts stay single-registered. |
| fix: inherit parent secret sources in task sub-agents (#6757) | adopted implicitly | Task-delegated sub-agents (all VibeBB review/design delegates) now inherit the parent's secret sources; matches how the plugins expect secrets to flow. |
| fix(sdk): consolidate capability resolution and reject silent image loss (#6040) + deepseek-v4.1-flash vision detection + DeepSeek image-loss bad-request | adopted implicitly | Vision-capability resolution hardened; VibeBB vision-review paths can no longer silently drop images. |
| feat: enable MCP protocol auto negotiation (#6794) | adopted implicitly | The repo's stdio MCP server now negotiates protocol versions automatically; no code change needed. |
| fix(task): key the task tool examples by the built-in agents' names | adopted implicitly | Task-tool guidance now matches delegated agent names. |
| fix(conversation): let run() resume a detector-STUCK conversation + persist state while a step waits on the model | adopted implicitly | Resume reliability for long-running plugin sessions. |
| Secret masking in thinking_blocks + signature preservation + keep the source's cipher and secrets on a forked conversation | adopted implicitly | Secret-handling hardening; no plugin action. |
| fix(llm) batch: bounded retries on persistent timeouts, retry content-free completions, classify safe exception types, record span messages/token totals | adopted implicitly | LLM-call robustness in the shared layer. |
| fix(agent-server) batch: socket close on delete/shutdown, one ServerContext per init, lease takeover for reused/zombie PIDs, public MCP OAuth callbacks, SameSite=Lax workspace cookie, gateway paused-conversation replies | adopted implicitly | Server lifecycle fixes upstream of the plugins. |
| fix(browser): kill hung browser on reset/close; fix(terminal): Windows process termination before pipe close; fix: tmux init race | adopted implicitly | Tool-layer stability fixes. |
| Dependency churn (fastmcp 4.1.6, pydantic 2.14.3, boto3, posthog, sqlalchemy, cachetools, joserfc, streamlit, pytest-asyncio) | adopted implicitly | Transitive bumps land through the lock. |

## SDK 1.63.0 -> 1.63.1

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| fix(sdk): keep openhands-sdk importable on a fresh litellm install (#7015) | adopted | The SDK now pins `litellm==1.108.3` in constraint-dependencies and stays importable on that line. litellm 1.108.3 relaxes `filelock` to `<5`, which is what allows the repo's `filelock>=4.2.1` override to be dropped in this sweep. |
| fix(mcp): keep runtime-added MCP tool names across restart and resume (#6929) | adopted implicitly | MCP tool names survive restart/resume; relevant to plugin MCP servers. |
| fix(llm): classify orphan tool messages as a malformed history (#6678) | adopted implicitly | History validation instead of silent failure. |
| fix(sdk): close built tool executors when response_schema is rejected + fix(acp): bill fork usage updates in ask_agent + fix(browser) close_tab | adopted implicitly | Correctness in tool executor and ACP/browser edges. |
| New verified-model list entries | noted | Verified-list additions are informational; LLM profiles stay user-configured. |

## Other components

| Component | From -> to | Decision | Evaluation |
| --- | --- | --- | --- |
| uv | 0.13.8 -> 0.13.9 | adopted | `required-version`, Dockerfile `UV_VERSION`/`UV_DIGEST`, setup-uv `version:` input. Changelog reviewed: PEP 832 support (informational), lockfile size reduction (benefits `uv.lock` size), wheel name-mismatch rejection (stricter — safe). |
| ruff | lock drift -> 0.17.5/0.17.6 | adopted implicitly | Bug-fix releases incl. a preview TC010 fix; no config change. |
| pydantic | lock drift -> 2.14.4 | adopted implicitly | jiter 0.19.0 bump + JSON Schema fixes. |
| filelock override | `filelock>=4.2.1` -> dropped | adopted (removal) | The override existed only to outrun litellm<1.108's `filelock<4.0` cap for CVE-2026-112940; litellm 1.108.3 allows `<5` and the lock resolves 4.2.12. |
| step-security/harden-runner | v2.22.4 -> v2.23.0 | adopted | Secure Registry community tier (available, not wired), S3 URL redaction fix (adopted implicitly). |
| astral-sh/setup-uv | v10.3.3 -> v10.4.0 | adopted | New `require-checksums` input evaluated: workflows already pin `version:` and the action SHA, so checksum pinning adds little; not adopted this sweep. PEP 440 fix adopted implicitly. |
| github/codeql-action | v4.39.3 -> v4.39.4 | adopted | CodeQL bundle 2.27.5; pin-only. |
| actions/dependency-review-action | v5.0.0 -> v5.0.1 | adopted | Better compare-endpoint error reporting; pin-only. |
| ossf/scorecard-action | v2.5.0 -> v2.6.0 | adopted | Scorecard v5.7.0. New `fail_on_publish_error` input evaluated and not adopted: result publishing must never fail the required scorecard check. Example-workflow additions not applicable. |
| kicad-nightly | 202611161144 -> 202611201144 (+4f26951493) | adopted | Latest Ubuntu 26.04 nightly deb; sha256 verified against launchpad. The `_cvpcb.kiface` ERC failure note in operations.md stands to be re-tested on the new build. |
| kicad-nightly-symbols | 202611121241 -> 202611200121 (+2f6414dda) | adopted | Same nightly series; sha256 verified. |
| CERN KiCad libraries | f5445d61 -> f793f03a | adopted | Submodule bump to upstream master. |
| publish litellm needle | `==1.105.1` -> `==1.108.3` | adopted | SDK 1.63.1's workspace pin moved; the re-resolve sed + fail-closed greps now match `==1.108.3` and relax to `>=1.104,<1.109`. Checker baseline updated to match. |

