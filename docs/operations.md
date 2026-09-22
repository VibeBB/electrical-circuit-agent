# Operations

## Development environment

```bash
uv sync
uv run python scripts/verify_all.py --stage docs
uv run python scripts/verify_all.py --stage fast
python3 -m circuit.doctor
```

## Docker image

```bash
docker build -f docker/circuit-tools.Dockerfile -t circuit-tools:dev .
docker image inspect circuit-tools:dev --format '{{.Size}}'
docker run --rm \
  --user circuit \
  -v "$PWD/fixtures/smoke-board:/work:ro" \
  circuit-tools:dev \
  python3 /opt/circuit/bin/smoke_kicad11_konnect.py
```

The image contains the KiCad nightly PPA, the Konnect release, and the CERN
submodule. The CERN commit is recorded in
`/opt/circuit/libraries/cern-kicad-libs.commit` and the OCI label
`circuit.cern.commit`. Because the SDK v1.49.3 server image build requires
root-privileged apt/useradd on the base image, the tools image's default user is
root. For standalone runs specify `--user circuit`; in the server image use the
`openhands` user created by the SDK. Docker itself does not guarantee
determinism, so published digests are locked.

## CI/CD and digest lock

| Workflow | Role |
|---|---|
| `ci.yml` | fast verification plus tools image/smoke/standard verification depending on change scope |
| `publish-circuit-images.yml` | GHCR tools/server publishing, post-publish smoke, lock update bot PR |
| `locked-image-check.yml` | Digest-pinned image verification on main push and weekly |
| `main-ci-failure-issue.yml` | Filing CI/image failure Issues on main and closing them on green |
| `check-dependency-updates.yml` | Weekly PPA/PyPI/GitHub/CERN/action update report |
| `workflow-lint.yml` | zizmor static analysis of the workflows, uploaded to code scanning |

The only required secret is `GITHUB_TOKEN`. Make `fast` a required check in
branch protection. The publish workflow publishes
`ghcr.io/vibebb/circuit-tools` and `ghcr.io/vibebb/circuit-server`, and creates
`docker/image-digests.json` for the first time via a bot PR. Filling a missing
lock with placeholders is forbidden. Because GITHUB_TOKEN events do not start
workflows, the publish workflow dispatches the lock-branch CI itself and
merges the lock PR synchronously once that run succeeds; when the Actions
policy lands the bot PR's `pull_request` run as `action_required`, it approves
the run via the Actions API. After merging, it dispatches `ci.yml` and
`locked-image-check.yml` on main as observational runs recorded in the step
summary.

The dependency update report can be checked locally as follows. `--dry-run`
prints the report to stdout without changing GitHub Issues.

```bash
uv run python scripts/check_dependency_updates.py --dry-run
```

`verify_all.py --stage standard` requires `CIRCUIT_TOOLS_IMAGE` and runs Docker
integration in addition to the fast checks.

For direct PyPI dependencies, the resolved version in `uv.lock` is reported as
the current value rather than the specifier in `pyproject.toml`. For specifiers
with an upper bound, the latest release within the range is compared and the
latest release outside the range is noted. Candidates deferred due to
constraints such as the SDK are recorded with a reason and a re-check deadline
in `scripts/dependency_update_deferrals.json`, and until the deadline they are
counted as `保留（記録済み）` (deferred, recorded) rather than as updates.
Candidates past their deadline return to the update candidates as
`保留期限切れ` (deferral expired). Malformed JSON is fail-closed and reported as
FAIL.

## Updating pins

1. Check the KiCad versions in the resolute Packages index of the PPA; the
   core, symbols, and footprints packages are all fetched from the Launchpad
   librarian and pinned by SHA-256, so refresh the download URLs and hashes
   together.
2. Update the `KICAD_NIGHTLY_VERSION`, footprints, and symbols pins together
   with `THIRD_PARTY_NOTICES.md` and ADR-0002 in the same change.
3. Verify the Konnect release asset, commit, SHA-256, and LICENSE against
   primary sources.
4. Update the CERN submodule and record the commit and fetch date in
   `libraries/README.md` and `THIRD_PARTY_NOTICES.md`.
5. To change the `openhands-sdk` or `openhands-tools` PyPI pins, update
   `pyproject.toml`, `uv.lock`, and this document.
6. Run docs, fast, image build, and smoke, and record the results in the
   handoff.

### Konnect v0.12.1 adoption record

- Checked on: 2026-09-20
- Update: v0.11.0 → v0.12.1
- Primary source: [v0.12.1 release notes](https://github.com/mixelpixx/Konnect/releases/tag/v0.12.1)
- Release notes summary:
  - v0.11.1 improved KiCad CLI discovery and MCP tool catalogue client compatibility.
  - v0.12.0 added board-targeted IPC, live-board verification, and stale-file fallback suppression.
  - v0.12.1 added connectivity/no-connect/junction preservation, fail-closed placement,
    real-connection verification in netlists, and native footprint flipping.
- Reason for adoption: safety of schematic-to-PCB authoring, identity of the
  target board, and fail-closed behavior based on observed results are useful
  for the current conversational design path.
- Note: the release notes do not claim explicit KiCad 11 support. KiCad 11
  nightly compatibility in this project continues to be verified by image
  smoke and direct `kicad-cli` checks.

### OpenHands SDK v1.49.3 adoption record

- Checked on: 2026-09-22
- Update: v1.49.2 → v1.49.3
- Primary source: [v1.49.3 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.49.3)
- Release notes summary:
  - `AgentContext.resolve_auto_skills()` was added and
    `RemoteWorkspace.load_skills_from_agent_server()` now accepts
    `base_context=` so remote skill sync preserves the caller's context,
    including `disabled_skills`.
  - Responses stream deltas now stamp the output `item_id`.
  - `deepseek-v4.1-flash` and `nemotron-3-nano-omni-30b-a3b-reasoning` were
    added to the verified-model list.
  - Agent-server side: the Docker host gateway is exposed, and MCP OAuth
    credentials can be passed inline on the agent.
- Reason for adoption: pin alignment to the latest patch; no public API
  surface used by this repository changed (no module added or removed in
  `openhands-sdk`/`openhands-tools`/`openhands-workspace`).
- Feature evaluation (checked against the plugin boundary):
  - `inspect_image_with_vision` (VisionInspectTool) inspects only images
    attached to the latest user message via a saved vision-capable LLM
    profile; circuit-review reads workspace PNG renders through files, so
    it is not adopted.
  - `resolve_auto_skills`/`disabled_skills` and
    `load_skills_from_agent_server` serve remote-workspace skill sync;
    this repo loads plugin skills locally, so they are not adopted.
  - Agent Plugins `mcp.json` portable format (root `plugin.json` +
    `dev.openhands/` layout) would require restructuring the plugin; the
    Claude Code format remains supported, so migration is deferred.
  - `switch_llm` lets the agent switch LLM profiles mid-run; sub-agents
    keep `model: inherit`, so it is not adopted.
  - `PlanningFileEditorTool` restricts edits to `.agents_tmp/PLAN.md`;
    circuit-brief writes the real brief file, so it is not adopted.
  - The agent-server changes arrive via the next `circuit-server` image
    publish, which derives `sdk_version` from the installed pin and
    rebuilds on the SDK tag; `docker/image-digests.json` is updated by
    that bot flow, not by hand.
  - mcp 2.x remains deferred: v1.49.3 keeps the `fastmcp<4`
    (`fastmcp-slim: mcp<2.0`) constraint; see
    `scripts/dependency_update_deferrals.json`.

## Plugin and tests

Locally, `python3 -m circuit.mcp_server` can be started as a stdio MCP server.
`circuit_api_server_start` accepts only one `.kicad_pcb`; call
`circuit_api_server_stop` first when switching. Docker integration tests can be
run optionally with
`CIRCUIT_TOOLS_IMAGE=circuit-tools:dev uv run pytest tests/integration -m docker`
and are skipped in environments without the image.

### Plugin hardening

Each sub-agent's frontmatter records the library-protection hook and
`max_budget_per_run: 3.0`. The plugin-level Stop hook reads
`circuit-reports/design-report.json` under the working directory (up to depth
4) and presents each verdict as additional context at the end. KiCad/CERN
libraries covered by the `circuit-library-guard` skill are read-only; use
`register_*_library` instead of editing. Slash command `argument-hint`s specify
input files and export types.

### Brief intake and library gate

When generating a design brief from conversation, first validate the brief and
intake sidecar produced by `circuit-brief`. `circuit_brief_intake_check`
inspects the brief's SHA-256, the R/A/Q source mapping, and open questions, and
does not pass anything other than `ready` to authoring.
`circuit_brief_library_check` directly parses the installed libraries and
inspects the pins referenced by every symbol, footprint, and net.

The KiCad/CERN library search roots can be overridden with:

```text
CIRCUIT_KICAD_SHARE=/usr/share/kicad-nightly
CIRCUIT_CERN_LIBS=/opt/circuit/libraries/cern-kicad-libs
```

To pass an intake sidecar to E2E authoring, specify `--intake PATH`.

### Design brief authoring

The KiCad 11 nightly E2E runs a jobset in addition to direct ERC/DRC and checks
report consistency. The top/bottom PNGs under `circuit-reports/render/` and the
diff JSON are auxiliary evidence for human review and do not change verdicts.

To run Konnect authoring, the netlist connectivity gate, ERC, PCB update, DRC,
and export from a design brief, run the following inside the tools image or in
an environment with the same PATH:

```bash
docker run --rm --user circuit \
  -v "$PWD:$PWD" -w "$PWD" circuit-tools:dev \
  python3 scripts/e2e_authoring.py \
  --brief tests/data/brief_led_loop.json \
  --workdir /tmp/circuit-led-loop
```

Each Konnect call and kicad-cli gate is saved to `authoring.jsonl`, and the
final decision is saved to `circuit-reports/design-report.json` as UTF-8 JSON.
Connectivity compares the output of `kicad-cli sch export netlist --format
kicadsexpr` against the design brief; Konnect's analysis results are treated as
advisory evidence, including short detection.

All 234 Konnect v0.12.1 tools are managed in `docs/konnect-tools.md` and
`plugins/circuit/skills/circuit-konnect/references/konnect-tools.json`.
Advisory failures during authoring are recorded but do not stop the E2E; the
`design-report.json` verdict is determined only from the kicad-cli
connectivity/ERC/DRC JSON.

As of 2026-09-20, the resolute package
`202609200244+21f1f53428~189~ubuntu26.04.1` failed with
`kicad-cli sch erc` on `_cvpcb.kiface` with
`undefined symbol: _ZN18PCB_TUNING_PATTERN10SetNetCodeEi`.
Upstream commit `21f1f53428` moved `PCB_TUNING_PATTERN::SetNetCode` out-of-line,
and the immediately following `7e4fac2d` reverted it as a build failure fix, but
the 09-20 package still has an inconsistency between `_cvpcb.kiface` and the
runtime library.

Because no fixed core package remains in the PPA index, we use the
[Launchpad librarian 09-19 build](https://launchpad.net/~kicad/+archive/ubuntu/kicad-dev-nightly/+files/kicad-nightly_202609190245+6d837080a5~189~ubuntu26.04.1_amd64.deb)
`202609190245+6d837080a5~189~ubuntu26.04.1` pinned by SHA-256. On this build ERC
and netlist export succeed, and the ERC integration test was returned to a
normal passing test.

The PPA drops package versions once newer builds publish, which removed the
pinned `kicad-nightly-symbols` version and broke the image build on
2026-09-22. The symbols and footprints packages are therefore also fetched
from the librarian and pinned by SHA-256
(`202609181937+a82391d3d~12~ubuntu26.04.1` and
`202609171319+1df46f29b~14~ubuntu26.04.1`), keeping the same verified set while
making the build independent of PPA retention.

On 2026-09-22 the pins moved to the fixed 09-21 core
`202609210241+6e93fd642e~189~ubuntu26.04.1` and symbols
`202609211218+422f3fe0e~12~ubuntu26.04.1` (footprints unchanged). The 09-21
build contains the `7e4fac2d` revert; `kicad-cli sch erc` and the docker
integration tests were verified passing on the built image before the update.
The librarian + SHA-256 mechanism stays in place so the build does not depend
on PPA retention.

## Sockets and permissions

Pass a filesystem path such as `/tmp/circuit-smoke.sock` to the server's
`--socket`. Pass `ipc:///tmp/circuit-smoke.sock` to the client. Run the image as
non-root and make `$HOME` and `$HOME/.config/kicad` writable.
