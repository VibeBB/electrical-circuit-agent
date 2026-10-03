# OpenHands SDK v1.50.1 feature evaluation (electrical-circuit-agent)

Scope: `openhands-sdk`, `openhands-tools`, and any directly pinned OpenHands packages move from 1.50.0 to 1.50.1 (PyPI upload 2026-09-30T19:31Z). The complete 16-commit upstream range `v1.50.0..v1.50.1` was reviewed. uv remains pinned at 0.12.22.

Primary source: [OpenHands SDK v1.50.1 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.50.1).

## SDK 1.50.0 -> 1.50.1

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| #5000 hard per-attempt timeout for async LLM streams | adopted implicitly | VibeBB profiles do not set `LLM.timeout`, so the 300-second hard timeout and `LLM.stream_idle_timeout` default apply. Both timeouts are retryable and map to `LLMTimeoutError`; no profile or shared-hook change is needed. `vision_is_active()` and the capability probe remain unchanged. |
| #5297 `openhands-tools` accepts non-UTF-8 text in `file_editor` | adopted with the pin | Useful for Japanese Shift_JIS/CP932 and other non-UTF-8 text inputs; no plugin code change is needed. |
| #5295 httpx[socks] >= 0.28.1; #5391 PyJWT 2.14.0 security fixes | lock-only | Refresh the requested SDK/tools lock resolution; these dependencies are picked up through `uv.lock` where they are present. |
| #5069 restore tool registrations after remote attach | not adopted | Plugins run in local conversations and do not use remote attach. |
| #5272 authenticated Canvas App backend bridge | not adopted | No VibeBB plugin ships a Canvas extension backend. |
| #5302, #5301, #5305 TypeScript client dependencies | not applicable | This repository does not use the SDK TypeScript client. |
| #5296 google-cloud-aiplatform 2.1.3 Vertex extra | not adopted | No VibeBB plugin enables the optional Vertex SDK extra. |
| #5378 agent-server budget-denial test | not applicable | Upstream test-only change; no repository behavior to adopt. |
| #5350 HTTP 429 admission control; #5369 idle-runtime catalog refresh; #5282 VSCode token on `/api/init`; #5393 git repository search | upstream image | This repository builds its mechanical/circuit agent-server image from the SDK source at the pinned version. The next publish derives `SDK_VERSION` from the updated lock and therefore picks up 1.50.1; the committed `docker/image-digests.json` is intentionally unchanged and remains workflow-owned. |
| #5410 release | not applicable | Release housekeeping; no plugin-level change. |

## Compatibility deferrals

MCP 2.x remains deferred: installed SDK metadata continues to require `fastmcp>=3.2.0,<4`, which caps `mcp<2`. Keep the existing deferral and its re-check policy. The `openhands-agent-server` image tag `1.50.1-python` is available upstream; no committed image lock is edited by this bump.
