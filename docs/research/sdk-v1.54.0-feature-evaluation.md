# OpenHands SDK v1.54.0 feature evaluation (electrical-circuit-agent)

Scope: `openhands-sdk` and `openhands-tools` move from 1.53.0 to 1.54.0
(GitHub release 2026-10-09). The complete upstream range `v1.53.0..v1.54.0`
(33 PRs) was reviewed. In the same change `uv` moves 0.12.23 to 0.13.0,
`astral-sh/setup-uv` v10.2.0 to v10.3.0, `ossf/scorecard-action` v2.4.4 to
v2.5.0, and `github/codeql-action` v4.38.2 to v4.38.3.

Primary source: [OpenHands SDK v1.54.0
release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.54.0).

## SDK 1.53.0 -> 1.54.0

| Upstream change | Decision | Evaluation |
| --- | --- | --- |
| #5503 feat(apps): support checksum-pinned remote backend artifacts (`release-note-required`) | available, not adopted | Lets an `apps` manifest pin backend artifacts by content hash. electrical-circuit-agent is an AgentCanvas `.plugin/plugin.json` plugin with no app manifest or remote backends; nothing consumes this surface. Adopt if a VibeBB app manifest ever declares remote backend artifacts. |
| #5527 chore: replace Node 22 with Node 24 | n/a | Upstream agent-server/CI runtime bump; the repo's own Node pins are managed separately (none here). |
| #5504 refactor(sdk): type LLM streaming, message, and tokenizer boundaries | adopted implicitly | Internal typing refactor; `Plugin.load` and the tool registry are unaffected (`check_plugin_load.py` passes). |
| #5263 #5407 #5429 feat(agent-server): reclaim Docker runtime caches, shed gitignored dirs over a disk budget, archive long-inactive runtimes | adopted implicitly | Agent-server side resource hygiene; benefits the deployment's agent-server, not this plugin's code. |
| #5357 refactor(sdk): typed settings and tool-metadata APIs | adopted implicitly | Internal refactor; public plugin surface unchanged. |
| #5551 fix(agent-server): omit acp/ credential dir from download-trajectory zip | adopted implicitly | Security fix in the trajectory export; lands with the pin. |
| #5293 feat: typed ACP capability and session contracts | available, not adopted | ACP is the upstream client protocol; this repo exposes deterministic MCP/CLI entry points and does not implement an ACP agent. |
| #5536 feat(typescript-client): HttpError.detail/isHttpError | not applicable | TypeScript client surface; this repo is Python/MCP. |
| #5467 #5460 fix(llm): DeepSeek v4 vision serialization + images dropped under forced string serializer | adopted implicitly | Fixes image handling for vision-capable models; relevant to VibeBB vision-review workloads when DeepSeek v4 is configured. |
| #5548 test: real Docker scenario for storage reclaim | not applicable | Upstream test coverage. |
| #5538 fix(agent-server): tolerate state saves while zipping a trajectory | adopted implicitly | Race fix in trajectory export. |
| #5547 fix(agent-server): remove a deleted local conversation's worktree, keep its branch | adopted implicitly | Runtime cleanup fix. |
| #5342 fix(terminal): preserve scripts across heredoc boundaries | adopted implicitly | Terminal execution fix; improves shell-script fidelity for agents running wire CLI commands inside heredocs. |
| #5582 fix(container): refresh agent server security dependencies | adopted implicitly | Dependency hardening in the container layer. |
| #5341 feat(agent-server): expose requested_ref on installed canvas extensions | available, not adopted | Canvas-extension manifest surface; not used by `.plugin/plugin.json` plugins. |
| #4784 fix(file_editor): prefer valid UTF-8 encoding | adopted implicitly | Aligns with this repo's `encoding="utf-8"` invariant; fewer mojibake edits on artifacts. |
| #5559 #5619 fix(agent-server): keep tmux socket paths within the Unix socket limit | adopted implicitly | Fixes overlong socket path failures on deep workspace roots. |
| #4952 fix(agent-server): resolve linked providers for plaintext profile reads | adopted implicitly | Profile/provider resolution fix. |
| #5616 chore(agent-server): refresh npm pin + automate observed updates | n/a | Upstream pin hygiene. |
| #5673 feat(llm): DigitalOcean Inference verified provider | available, not adopted | New LLM provider option; VibeBB LLM config lives in the deployment, not the plugin. |
| #5182 feat(security): emit SecurityAnalysisEvent per action batch | adopted implicitly | New observability event from the security analyzer; hooks receive the event stream as before, no plugin change needed. |
| #5683 chore: remove stale triage workflow/roster | not applicable | Upstream repo hygiene. |
| #5670 fix(file-editor): preserve terminal newline semantics | adopted implicitly | Editor correctness fix; protects the byte-deterministic artifact projections. |
| #5579 feat(acp): discover ACP default/available models, restore Gemini handling | available, not adopted | ACP-side; not used here. |
| #5671 test: hard reset semantics (c02) | not applicable | Upstream test coverage. |
| fastmcp `>=3.2.0,<4`, pydantic `>=2.13.5`, pillow, requires-python `>=3.12` | n/a | Constraint surface unchanged from 1.53.0; `mcp<2` still forced by fastmcp — the MCP 2.x deferral stands. |

## uv 0.12.23 -> 0.13.0

Breaking-change review against this repo's surfaces:

| Upstream change | Evaluation |
| --- | --- |
| CPython 3.15 becomes the default stable interpreter | No effect: `.python-version` pins 3.14 and the Dockerfile pins `uv python install 3.14`. The 3.15 CI leg stays advisory (see compatibility deferrals). |
| `--require-hashes` honored in `-c` constraint files; editable reqs rejected in constraints; `--constraint/--override/--exclude/--build-constraint` treated as single paths | No effect: the repo uses no constraint/override/exclude files. |
| tar codec replaces the legacy tar backend (`UV_LEGACY_TAR_BACKEND=1` opt-out) | No effect observed: `uv lock --upgrade` and `uv sync` both ran clean under 0.13.0 locally. |
| `_virtualenv` distutils patch omitted on Python >=3.10 | No effect: nothing imports `distutils` (ruff-clean codebase). |
| `uv build --clear` rejects output dirs containing sources | No effect: images use `uv export`/`uv sync`, not `uv build --clear`. |

setup-uv v10.3.0, scorecard-action v2.5.0, codeql-action v4.38.3 are
maintenance bumps (new SHAs, same inputs); no behavioral changes affecting
this repo's workflows.

## Repo-specific dependencies (electrical-circuit-agent)

| Component | From -> to | Decision |
| --- | --- | --- |
| KiCad nightly PPA pins (3 packages) | unchanged | `check_kicad_ppa` confirms the pinned 2026-10-05/07 builds still match the newest resolute packages the PPA index offers; no adoption needed. |
| FreeRouting | already 2.5.0 | Pinned release current; nothing to adopt. |
| CERN KiCad libraries submodule | unchanged | Checker reports the pinned commit still matches `gitlab.com/ohwr/cern-kicad-libs` HEAD. |
| Konnect / Semeru JRE | unchanged | No newer upstream release. |

## Compatibility deferrals

- MCP 2.x remains deferred: SDK 1.54.0 still requires `fastmcp>=3.2.0,<4`
  (caps `mcp<2`); `scripts/dependency_update_deferrals.json` now cites 1.54.0.
- Python 3.15 stays the advisory canary: the sdk-check group's
  `openhands-sdk -> litellm -> fastuuid 0.14.0` chain ships only an sdist
  whose bundled PyO3 fails to build on 3.15 (verified locally — `uv sync`
  fails at maturin). `pydantic-core >=2.50` now ships cp315 wheels, so
  fastuuid is the sole remaining blocker; the python-version deferral
  records this and the canary keeps detecting the landing.
