# Agent Canvas v1.25 feature evaluation (electrical-circuit-agent)

Scope: the full Agent Canvas / OpenHands platform surface as of agent-canvas
1.25.0 + agent-server/SDK 1.53.0 + automation 1.19.0 — not just the
September/October 2026 release notes but every documented feature area
(goal loop, profiles, runtime, automations, canvas UX, security, skills).

Primary sources: the public docs index (287 non-API pages) plus the
installed SDK/agent-server/automation source trees. Decisions were made in
the October 2026 VibeBB platform review; this file records them and their
reasons so future bumps re-check against them.

## Adopted in this change

| Feature | Decision | Evaluation |
| --- | --- | --- |
| `oracle` LLM profile + `ask_oracle` | adopted | The tool resolves a saved LLM profile named literally `oracle` (stateless second opinion, no history, no tools, never switches the conversation LLM). `ensure_llm_profiles.py` now seeds `oracle` alongside `vibebb-author`/`vibebb-review` so the convention name exists on every install. |
| Agent profiles (schema_version 3) | adopted | New `ensure_agent_profiles.py` SessionStart hook writes `~/.openhands/agent-profiles/vibebb-circuit.json` when absent: `agent_kind=openhands` (required — ACP kinds do not run plugin hooks), `llm_profile_ref=vibebb-author`, `mcp_server_refs=["circuit"]` (least-privilege tool surface), `secret_refs=[]` (no secrets — VibeBB tools need none). |
| Docker conversation-runtime fail-closed | adopted | Verified in `docker_runtime/registry.py`: conversation containers set `HOME=/var/openhands/.openhands` and inject `OH_PERSISTENCE_DIR`/`OH_RUNTIME_LAUNCHED_PROFILE` (and `OH_CONVERSATION_RUNTIME=local` inside — useless as a signal), and carry no docker client. `circuit_launcher.py` now detects this (`_inside_conversation_container`) and fails closed with guidance to switch to the local runtime. `.mcp.json`/`hooks.json` probe chains gained `${HOME}/plugins/installed/circuit` and `${OH_PERSISTENCE_DIR}/plugins/installed/circuit` so the plugin still resolves inside that layout. |
| Path-triggered rules (`paths:` frontmatter) | extended | SDK `Skill` supports `paths:` → deterministic `<EXTRA_INFO>` injection when a matching file is touched (model-independent). circuit already had `circuit-brief-rules` (`*.brief.json`/`*.intake.json`) and `circuit-library-guard`; added `circuit-out-rules` covering `**/out/**` (generated artifacts are read-only; regenerate, never hand-edit). Complements — does not replace — the `protect-libraries` hook, which enforces. |
| Design-side automations (plugin preset) | adopted | `POST /v1/preset/plugin` accepts `plugins: [PluginSource]` + `prompt` + `trigger {type: cron, schedule, timezone}` and runs with plugins attached (hooks + MCP intact). Shipped `automations/reverify-weekly.automation.json` as a template. |

## Evaluated and recorded; not adopted

| Feature | Decision | Evaluation |
| --- | --- | --- |
| Child conversations (`launch_child_conversation`) | not adopted | Children carry their own profile/event stream and optionally a separate worktree. Sister coordination is defined as JSON contract files inside one conversation's shared workspace, driven by `task` sub-agents — child conversations would split the event trail and hide uncommitted sibling artifacts. Users exploring alternatives use `Branch from here` instead. |
| `response_schema` on tools | recorded, no adoption surface | Verified: `tools[].params.response_schema` reaches `set_response_schema` through the registry for any tool including MCP, validating the LLM reply against a JSON Schema. No VibeBB component parses ad-hoc LLM JSON — every tool returns deterministic structured output already — so there is nothing to replace. Recorded as an option if a sister ever needs typed LLM replies. |
| Native critic (`verification` profile field) | not adopted | The hosted critic scores every action in real time; VibeBB's author/review lanes, blind dual review, and deterministic gates already own verdicts. A second always-on reviewer doubles review cost with no additional verdict authority. The field is left off in seeded profiles. |
| TOM agent (`TomConsultTool`/`SleeptimeComputeTool`) | not adopted | Experimental user-modeling pair; VibeBB already structures requirements through question-driven skills and contract schemas. Revisit if it stabilizes. |
| Meta-profile routing (`meta_profile_ref`, `route_task_to_model`) | not adopted | The explicit `vibebb-author`/`vibebb-review` lanes already are the routing decision; a meta-profile would only help users with many model profiles. Field left unset in seeded profiles — operators may enable it. |
| `FallbackStrategy` (`fallback_llms`) | recorded, operator option | Per-call fallback to named LLM profiles on transient errors. Useful for long design sessions; left to operators editing the seeded LLM profiles rather than a shipped default. |
| Reasoning controls (`reasoning_effort`/`reasoning_summary`/`extended_thinking_budget`) | recorded, operator option | LLM-profile fields; raising effort on `vibebb-author` trades cost for design depth. Not set by seeding. |
| Browser/search tools (`BrowserToolSet`, web researcher) | recorded, environment-dependent | Agent-side tools (unrelated to the `--network none` tool containers). Could help datasheet/parts research but needs a working browser runtime on the host; documented in the public guide instead of adopted. |
| Slack notification (automation `streams/slack.py`) | deferred | Requires Slack integration on the agent-canvas host (sd-134666 has none today). Recorded for when the host gains one. |
| Marketplace registration | deferred (future plan) | Requires upstream write access to the marketplace catalog; tracked on the site roadmap. |
| Cloud `/launch` links | deferred (future plan) | `app.all-hands.dev/launch?plugins=…` is Cloud-only and VibeBB tools need host Docker, which cloud sandboxes do not provide; a launch link would hand users a broken session. |
| Docker conversation-runtime full support | deferred (future plan) | Two paths exist — (a) host docker-socket sharing defeats the isolation the runtime is for and hits the host's rootless uid-mapping issue; (b) a custom conversation image bundling every tool stack fights the per-repo digest-pin design. Design conversations stay on `local`; revisit if VibeBB moves to a shared multi-user canvas. |
| Apps / canvas extensions | adopted separately | Implemented for dashboard-agent as its own change; circuit ships no canvas UI. |

## Notes

- `OH_CONVERSATION_RUNTIME` must stay `local` for design conversations; the
  launcher now fails closed with explicit guidance when it detects a
  conversation container.
- Profile files are seeded only when absent; operator edits are never
  overwritten (`vibebb-*` LLM profiles, `vibebb-<plugin>` agent profile).
