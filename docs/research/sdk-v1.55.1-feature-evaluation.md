# OpenHands SDK v1.55.1 feature evaluation (electrical-circuit-agent)

Scope: `openhands-sdk` and `openhands-tools` move from 1.54.0 to 1.55.1
(upstream releases `v1.55.0` of 2026-10-16 and `v1.55.1` of 2026-10-17). The
complete upstream range `v1.54.0..v1.55.1` (64 merged PRs) was reviewed. In
the same change `uv` moves 0.13.0 to 0.13.1, `ruff` moves 0.17.0 to 0.17.1,
`github/codeql-action` moves v4.38.3 to v4.39.0, the pinned `zizmor` wheel
moves 1.30.1 to 1.31.1, and `aquasecurity/trivy-action` `version:` inputs move
v0.75.0 to v0.76.0.

Primary sources:
[OpenHands SDK v1.55.0 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.55.0),
[OpenHands SDK v1.55.1 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.55.1).

## SDK 1.54.0 -> 1.55.1

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| #5682 fix(agent-server): reject blank and redacted secret values | adopted implicitly | Hardens credential handling in the agent-server; lands with the pin. |
| #5614 ci: upgrade integration matrix to DeepSeek V4.1 Flash | not applicable | Upstream CI internals. |
| #5675 test(integration): make token condensation deterministic | not applicable | Upstream test coverage. |
| #5291 fix(llm): don't send stop words to OpenAI models on Bedrock | adopted implicitly | LLM request correctness fix. |
| #5661 #5700 fix(agent-server): keep sockets and /v1 closed until deferred init completes; gate ingress during deferred initialization | adopted implicitly | Removes an uninitialized-ingest race on the agent-server. |
| #5703 fix(llm): keep verified OpenRouter models discoverable | adopted implicitly | Provider list correctness. |
| #4838 fix(mcp): recover after HTTP session failure | adopted implicitly | HTTP MCP sessions now recover instead of wedging; directly relevant since the plugin exposes stdio MCP and other agents consume MCP tools. |
| #5436 fix: support GPT-6 Sol, Luna and GPT-6.1 Sol subscriptions | available, not adopted | New subscription plan support; LLM credentials live in the deployment, not the plugin. |
| #5715 fix(terminal): notice a finished command within milliseconds | adopted implicitly | Reduces agent command latency; no plugin change needed. |
| #5689 #5774 #5920 #5944 #5945 #5918 #5907 #5787 #5924 #5921 #5931 #5929 #5923 fix(agent-server): Canvas backend lifecycle, proxy, upload, secure-context and tool-advertising fixes | adopted implicitly | Agent-server robustness batch; benefits the deployment. |
| #5587 fix(sdk): recognize Haiku 5.5 prompt caching without model metadata | adopted implicitly | Model handling fix; applies only when Haiku 5.5 is configured. |
| #5659 fix(agent-server): ignore empty session API keys | adopted implicitly | Same family as #5660. |
| #5657 fix(llm): keep provider credentials out of completion logs | adopted implicitly | Security: secrets no longer leak into completion logs. |
| #5621 feat: verify-agent-server executable feature map + control CLI; #5727 #5728 maintenance passes | available, not adopted | Upstream verification tooling for the agent-server API; could be used operationally but nothing in the repo consumes it. |
| #5724 Replace policy rail regexes with shell AST analysis | available, evaluate | Upstream replaced regex-based policy checks with shell AST analysis inside the agent. VibeBB's `safety_rail.py` shared hook remains the repo's own pre-tool guard and is unchanged; no adoption action needed here, but the upstream direction validates the AST approach. |
| #5660 fix(agent-server): treat an empty OH_SECRET_KEY as unset | adopted implicitly | `release-note-required` behavioral change: an empty `OH_SECRET_KEY` no longer authenticates. Deployments relying on an empty value must unset it explicitly; plugin code unaffected. |
| #5520 fix(subagent): let scoped sub-agents keep conversation-local tools like task_tracker | adopted implicitly | Sub-agents now keep conversation-local tools; VibeBB sub-agents declared via `task_tool_set` benefit (task tracking works inside sub-agents). |
| #5744 fix(profiles): preserve disabled skills when seeding default | adopted implicitly | Profile seeding fix. |
| #5409 feat: launch inline Agent Profiles and per-launch LLM overrides | superseded | Inline profile launches were subsequently removed by #5983 in the same release; no adoption. |
| #4960 feat(agent-server): accept additional skills when launching an agent | available, not adopted | Launch-time skill injection is a server-side option; plugins declare their own skills. |
| #5754 fix(sdk): skip sub-agents whose skill is missing instead of failing | adopted implicitly | Missing-skill sub-agents degrade to a skip rather than a hard failure — matches fail-soft expectations, though VibeBB gates remain fail-closed. |
| #5759 fix(agent-server): re-probe a Canvas App backend after a missed health probe | adopted implicitly | Health-probe recovery. |
| #4814 fix(sdk): keep fork titles out of local conversation tags | not applicable | Conversation metadata hygiene. |
| #5478 feat: add newly released LLM models to verified lists | adopted implicitly | Verified model list refresh. |
| #4926 docs(acp): clarify client surfacing contract | not applicable | Documentation only; ACP unused. |
| #5981 feat(sdk): let Agent Profiles select public skills, read them locally and use the global critic key | available, not adopted | Profiles can now pull public skills and use the global critic key; VibeBB's `ensure_agent_profiles.py` seeds repo-owned profiles and needs no change. |
| #5747 fix(subagent): sub-agents inherit exactly the parent's MCP servers | adopted implicitly | Deterministic MCP propagation to `task` sub-agents; VibeBB relies on `task_tool_set` delegation — verify no repo expected looser scoping. |
| #5926 fix(agent-server): read an unreachable OpenAI refresh as disconnected | adopted implicitly | Provider liveness fix. |
| #5986 fix(git): keep Azure DevOps clone URLs without a .git suffix | not applicable | VibeBB hosts on GitHub only. |
| #5909 fix(sdk): accept the documented `github:owner/repo/<path>` install source | adopted implicitly | Plugin install-source parsing fix; relevant to installing VibeBB plugins from repo paths. |
| #5891 fix(sdk): keep edge whitespace on the original side of a git diff | adopted implicitly | Diff fidelity fix; protects byte-deterministic artifacts. |
| #5903 fix(agent-server): clear the skills marketplace cache on sync | adopted implicitly | Cache-coherence fix. |
| #5905 fix(sdk): name a manifest-less git repository plugin after the repository | adopted implicitly | Plugin naming fix for manifest-less repos; VibeBB plugins always carry `.plugin/plugin.json`. |
| #5589 fix(agent-server): stop startup tmux cleanup killing live sibling sessions | adopted implicitly | Session-robustness fix. |
| #6003 fix(task_tracker): accept common task status names | adopted implicitly | task_tracker accepts `todo`/`doing`/`done` style statuses; sub-agents can use the tracker (paired with #5520). |
| #5990 fix(plugin): load plugins whose root plugin.json has no `$schema` with the Claude Code layout | adopted implicitly | `check_plugin_load.py` exercises `Plugin.load`; VibeBB plugin.json files already declare `$schema`. |
| #5983 refactor(agent-profiles): remove inline profile launches and keep a profile active | adopted implicitly | Removes the #5409 surface again; profiles stay active instead. |
| #5984 refactor(agent-profiles): deprecate the legacy profile resolver | adopted implicitly | Resolver deprecation only; no repo code path changes. |
| #5991 fix(agent-server): drop pip's vendored pkg_resources (v1.55.1) | adopted implicitly | Removes a vendored vulnerable dependency. |
| #5753 fix(sdk): list changes of a repository below a non-repository path (v1.55.1) | adopted implicitly | Git change-listing fix under nested paths. |
| #5696 fix(agent-server): resolve trajectory download dir from request config (v1.55.1) | adopted implicitly | Trajectory export fix. |
| #5987 feat(plugin): add strict plugin fetch preflight (v1.55.1) | adopted implicitly | Plugin fetches now preflight strictly; relevant to installing the VibeBB plugin set — no plugin-side change needed. |

Constraint surface: `fastmcp>=3.2.0,<4` still applies, so `mcp<2` remains
forced and the MCP 2.x deferral stands (re-verified against the 1.55.1
requirements). `requires-python >=3.12` unchanged.

## Toolchain and workflow bumps

| Change | Evaluation |
| --- | --- |
| uv 0.13.0 -> 0.13.1 | Bug-fix/performance release only (`--dry-run` metadata, `uv export` edge cases, resolver perf). No repo workflow changes required; pin moved in `required-version`, `UV_VERSION`/`UV_DIGEST` Dockerfile args, and `version:` inputs in ci.yml where present. |
| ruff 0.17.0 -> 0.17.1 | Bug-fix release (formatter/lint edge cases). Re-run of `ruff check` and `ruff format --check` produced no new diagnostics. |
| codeql-action v4.38.3 -> v4.39.0 | Bundles CodeQL 2.20.7 minimum and adds `checkout_path` input validation; this repo analyzes the workspace root so no input change is needed. Pin sha + comment updated everywhere the action appears. |
| zizmor wheel 1.30.1 -> 1.31.1 | Adds an `issue_comment` trigger audit and root-aware input collection, plus a cache mode. A local `uvx --from <wheel> zizmor .github/workflows` run was executed against the updated workflows; no new findings. |
| trivy-action `version:` v0.75.0 -> v0.76.0 | Adds CBOM generation and Echo/RapidFort image sources. New detectors may surface additional findings on the next scheduled container audit; the pin itself is input-compatible. |
| astral-sh/setup-uv `version:` 0.13.0 -> 0.13.1 (dashboard ci.yml) | Keeps the workflow-installed uv in lockstep with `required-version`. |
| KiCad nightly 202610072102+dc102b0059~189 -> 202610142039+ccb19070e9~190 | Weekly nightly; .deb URLs + sha256 re-pinned for core, symbols and footprints. |
| cern-kicad-libs submodule 5debc1b -> fe4025f | Library sync; gitlink + docs pins updated. |

## Deferrals kept

- `mcp` 2.x: still impossible — `openhands-sdk` 1.55.1 requires
  `fastmcp>=3.2.0,<4`, and fastmcp 3.x requires `mcp<2`. Deferral reasons
  refreshed to cite 1.55.1.
- Python 3.15 CI leg: `fastuuid` 0.14.0 (latest) still ships no cp315
  wheels, so the leg stays advisory.
