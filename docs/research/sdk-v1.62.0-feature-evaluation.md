# OpenHands SDK v1.62.0 feature evaluation (electrical-circuit-agent)

Scope: `openhands-sdk` and `openhands-tools` move from 1.59.0 to 1.62.0
(upstream releases `v1.60.0`, `v1.61.0`, `v1.62.0`). The complete upstream
range `v1.59.0..v1.62.0` was reviewed. In the same change `mcp` moves to
the 2.x API (forced: `openhands-sdk` 1.62.0 requires `fastmcp>=4`, and the
fastmcp-slim client extra pins `mcp>=2`, so `mcp>=1.29,<2` was
unsatisfiable), `uv` moves 0.13.6 to 0.13.8, `ruff` moves to 0.17.5,
`pydantic` moves to 2.14.3, `step-security/harden-runner` moves to
v2.22.4, `docker/login-action` moves v4.7.0 to v4.8.0,
`github/codeql-action` moves to v4.39.3, `astral-sh/setup-uv` moves to
v10.3.3, and `zizmor`/`trivy` move to 1.32.0/0.76.2. KiCad nightly,
Semeru JRE 27.0.1.0 and the CERN KiCad libraries submodule were also
bumped (details below).

Primary sources:
[v1.60.0 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.60.0),
[v1.61.0 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.61.0),
[v1.62.0 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.62.0).

## SDK 1.59.0 -> 1.60.0

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| #6159 feat(sdk): make ThinkTool opt-in instead of a default tool | adopted implicitly | ThinkTool is no longer injected into every agent; none of the VibeBB plugins reference `think` in their AgentDefinitions, so agents simply lose an unused tool. If a plugin later wants it, it is opt-in per agent. |
| #5502 feat(sdk): consume proxy-advertised request-parameter capabilities | adopted implicitly | LLM request shaping now honors capabilities the proxy advertises; transparent to plugins. |
| #6068 feat(llm): add DecisionModel with OTEL tracing | available, not adopted | A dedicated model for routing/decisions with tracing. VibeBB decisions are deterministic gates or VRP records, not LLM routing calls; no adoption point today. |
| #5734 feat(agent-server): load user hooks in POST /api/hooks | noted | Server-side hook loading for remote conversations; plugins register hooks via plugin.json, unchanged. |
| #6343 fix(agent-server): encrypt bash event files at rest (OHE-3812) | adopted implicitly | Security hardening of persisted events; no plugin action needed. |
| #5199 feat(agent-server): suspend idle Docker containers on terminal conversation state | adopted implicitly | Resource efficiency on the server side. |
| #6041 feat: let Canvas App backends use explicitly approved secrets | available, not adopted | Relevant only where a repo ships an Agent Canvas app; document-agent has none. |
| #6159-range llm/tool fixes (SGLang overflow map #6301, never end on assistant turn #6309, validation-loop vs repair #6278, browser_get_content #6329, task_tracker description #6330, str_replace edge cases #6357/#6358/#6372/#6342, secret masking order #6356, JSON-schema tuple form #6373, zero-history limit #6375, git URL parsing #6379, store path separator match #6374, title inputs #6337, non-UTF-8 .mcp.json as validation error #6430, sentiment logits #6384, gpt-oss reasoning #6111) | adopted implicitly | Correctness and hardening in the shared tool/llm layer; each benefits plugin runtime without changes. |
| #6281 feat(workspace): clone Bitbucket Data Center repositories | not applicable | Hosting provider support; VibeBB runs on GitHub. |
| #5885-class dependency/ci/test churn (dependabot batch, TestLLM refactors, parity tests, ci plumbing) | not applicable | Upstream internals only. |

## SDK 1.60.x -> 1.61.0

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| #6540 fix(mcp): migrate to fastmcp 4 and mcp 2 | adopted implicitly (SDK side) | The SDK's own MCP layer moved to fastmcp 4. The fastmcp 4 core no longer depends on the `mcp` package, but the `fastmcp-slim[client]` extra the SDK pulls in pins `mcp>=2`, which makes the repo's `mcp>=1.29,<2` cap unsatisfiable. `mcp_server.py` migrated to the mcp 2.x API in this change and the mcp deferral entry was consumed and removed. |
| #6542 feat(hooks): expose per-plugin PLUGIN_ROOT to plugin hook commands | adopted | The loader now exports `PLUGIN_ROOT` and resolves `${PLUGIN_ROOT}` in hook/MCP command strings. Every `for c in` candidate list in `plugins/*/hooks/hooks.json` and `plugins/*/.mcp.json` now tries `${PLUGIN_ROOT:-}` first — a cheap, backward-compatible resolution improvement (empty var is skipped on older SDKs). |
| #5767 fix(agent-server): load skills marketplace from .plugin manifest first | adopted implicitly | Matches the VibeBB `.plugin/plugin.json` layout; install-source resolution prefers our manifest. |
| #6307 Replace OpenVSCode Server with Coder code-server | noted | Agent-server IDE component swap; VibeBB plugins do not reference the IDE. |
| #6276 feat(secrets): optional scope and scope_description, grouped in the system prompt | available, not adopted | Secret metadata for the system prompt; plugin secret flow unchanged. |
| #6107 feat(sdk): support effort level for Claude ACP models | available, not adopted | ACP effort control; VibeBB does not configure ACP agents. |
| #6510 refactor(tools): deprecate legacy delegation and move the shared visualizer | adopted implicitly | Legacy `DelegateTool` was already avoided by policy (`TaskToolSet` only); deprecation aligns upstream with our rule. |
| #4862 fix(mcp): kill stdio subprocesses unconditionally on sync_close | adopted implicitly | Stdio server lifecycle hardening. |
| #6529 fix(agent-server): aggressive dependency upgrades (npm 12, code-server 4.145 + undici 7.30.1, node 24.22.2, uv 0.13.7, msgpack 1.2.3) | adopted implicitly | Server image refresh; consistent with this sweep's uv 0.13.8 bump. |
| #5775/#5816/#5792/#5763/#5796/#5775 agent-server security fixes (cipher-key profile guard, ACP switch refusal, root-path session cookie, WebSocket subprotocol negotiation, MCP server command/url validation) | adopted implicitly | Server-side hardening; no plugin action. |
| Remaining fix batch (workspace/Docker lifecycle, file-store cache correctness, file editor CRLF/all-or-nothing/ripgrep fallbacks, glob/grep brace expansion, hook validation + process-group kill, skills discovery README/BOM handling, secret-lookup lock scope, malformed arg repair, usage cost recording, title/case handling, safe_rmtree reporting, trajectory zip isolation, verify-sdk feature map #5750) | adopted implicitly | Correctness, robustness and observability improvements in the shared layers; none change plugin contracts. `verify-sdk` (#5750) is an upstream executable feature map — noted for future capability audits, not adopted. |

## SDK 1.61.0 -> 1.62.0

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| #6704 feat(agent-server): read a pull request's state and CI result | available, not adopted | Server API for PR status; VibeBB release/publish flows use `gh` in scripts, not this endpoint. |
| #6556 feat(sdk): make ACP reasoning effort a first-class `acp_effort` setting | available, not adopted | Same ACP surface as #6107; no ACP agents configured. |
| #5994 fix(acp): bump claude-agent-acp to 0.99.1 | adopted implicitly | Dependency refresh inside the ACP path. |
| #6669/#6706/#6671/#6709/#5766/#5886/#6713/#4502/#6661/#6664 robustness fixes (damaged event-file reopen, HTTPException headers, tmux follow-up, socket reads during a step, per-download temp zips, device-login gateway errors, macOS ARM runner, Playwright Chromium on Windows, allowed_domains error surfacing, truncated-skill description pointer) | adopted implicitly | Correctness fixes in shared layers; no plugin action needed. |
| #6744 Add Claude Fable 5.2 to verified model lists | adopted implicitly | Model catalog refresh. |

## Other components

| Component | Decision | Evaluation |
| --- | --- | --- |
| ruff 0.17.3 -> 0.17.5 | adopted | Patch releases: new/updated lint rules and fixes; `ruff check` + `ruff format --check` run green locally. |
| uv 0.13.6 -> 0.13.8 | adopted | Resolver/build fixes; `required-version` pin and every `setup-uv`/`version:` reference updated. |
| step-security/harden-runner v2.22.x -> v2.22.4 | adopted | Patch release; SHA pin updated family-wide. |
| docker/login-action v4.7.0 -> v4.8.0 | adopted | Minor release; SHA pin updated where used. |
| github/codeql-action v4.x -> v4.39.3 | adopted | Patch release; SHA pin updated. |
| zizmor 1.31.1 -> 1.32.0 | adopted | New audits + fixes; wheel URL and sha256 pin updated, local `uvx zizmor` audit green. |
| trivy v0.76.1 -> v0.76.2 | adopted | Patch release; version refs updated where pinned. |
| pydantic 2.14.1 -> 2.14.3 | adopted implicitly | Lock-level patch upgrade via `uv lock --upgrade`; pydantic-core compat fixes only. |

## SDK 1.61.x -> 1.62.0

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| fastmcp>=4 requirement (drops the `mcp` package dependency) | adopted (forced) | `fastmcp-slim[client]` pins `mcp>=2`, so the repo's `mcp>=1.29,<2` pin could not resolve. `src/circuit/mcp_server.py`, `src/circuit/mcp_collect.py`, `src/circuit/mcp_konnect.py` and the replay/test call sites were migrated to the mcp 2.x lowlevel API (`Server(..., on_list_tools=..., on_call_tool=...)`, `PaginatedRequestParams`/`CallToolRequestParams` params, `ListToolsResult`/`CallToolResult` returns, snake_case `input_schema`/`is_error`/`mime_type`). |
| Remaining v1.62.0 changes | adopted implicitly | Routine fixes/dependency refresh; no plugin-visible contract change beyond the mcp boundary above. |

## Non-SDK updates in this change

| Component | Decision | Evaluation |
| --- | --- | --- |
| KiCad nightly `202610142039+ccb19070e9~190` -> `202611131137+711550ba60~190`, symbols `202610290319+45efbd93b~12` -> `202611121241+9fa442824~12`, footprints `202610272317+a337146e0~14` -> `202611021521+a0dcd398e~14` | adopted | KiCad nightly is a rolling build of upstream `master`; there is no per-build changelog — the covered range is roughly four weeks of upstream development (symbols/footprints are content-only packages, so no behavior surface). The tools image consumes the debs as an execution environment for `kicad-cli` ERC/DRC and the Konnect bridge; risk is upstream nightly regressions, mitigated the same way as previous nightly bumps: deb sha256 re-verified by download and the `_cvpcb.kiface` ERC failure noted in `docs/operations.md` is re-tested after the tools image republishes. No new flags/features are consumed by `src/circuit/kicad_cli.py` (it shells out to `kicad-cli` and Konnect, both version-agnostic). |
| Semeru JRE `27.0.0.0` -> `27.0.1.0` | adopted | IBM Semeru OpenJ9 27.0.1.0 is the quarterly maintenance refresh of the Java 27/OpenJ9 0.58 line (bug and security fixes only, no feature changes per the release tag `jdk-27.0.1.0`). The JRE only runs FreeRouting (`freerouting.jar`) inside the tools image; tarball sha256 re-verified by download. No JRE feature is adopted — FreeRouting is invoked as a subprocess. |
| CERN KiCad libraries `0b41c59f` -> `c62d9bd1` | adopted | Single-commit range (`c62d9bd1 "Update KiCad libraries"`): a bulk content refresh of the vendored CERN symbol/footprint libraries the import-source provenance gate treats as `origin: "cern"`. Content-only change; no API surface, and `src/circuit/libsource.py` licenses/origin rules are unchanged. |
| `sdk-litellm-pin` publish needles | updated | Upstream SDK v1.62.0 moved the litellm constraint to `constraint-dependencies` as `==1.105.1` (was `==1.93.0`) and dropped the `exclude-newer-package` cap; the Re-resolve step's sed needles were updated to match and the stale comments rewritten. |
| Trivy binary pins (container-audit.yml, publish-circuit-images.yml) | kept at 0.76.2 | Checker could not resolve a newer release (fetch failed); no newer `v0.x` verified, pins already on the sweep target. |
