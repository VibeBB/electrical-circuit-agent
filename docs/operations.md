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

The image contains the KiCad nightly PPA, the Konnect release, the IBM Semeru
Open JRE (Eclipse OpenJ9), the FreeRouting JAR, and the CERN submodule. The
JRE is extracted to `/opt/jre` and resolved via `JAVA_HOME`/`PATH`; the JAR is
placed at `/opt/freerouting/freerouting.jar`, which Konnect v0.12.1 discovers
via its built-in search roots (see ADR-0021). The CERN commit is recorded in
`/opt/circuit/libraries/cern-kicad-libs.commit` and the OCI label
`circuit.cern.commit`. Because the SDK v1.49.4 server image build requires
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
4. For the Semeru JRE and FreeRouting, check the latest releases of
   `ibmruntimes/semeru<major>-binaries` and `freerouting/freerouting`, refresh
   the version and SHA-256 ARGs together with `THIRD_PARTY_NOTICES.md` in the
   same change, and re-verify the LICENSE asset for FreeRouting. A Semeru
   major-series migration (a new `semeru<N>-binaries` repository) is a manual
   decision and is not flagged by the weekly check.
5. Update the CERN submodule and record the commit and fetch date in
   `libraries/README.md` and `THIRD_PARTY_NOTICES.md`.
6. To change the `openhands-sdk` or `openhands-tools` PyPI pins, update
   `pyproject.toml`, `uv.lock`, and this document.
7. Run docs, fast, image build, and smoke, and record the results in the
   handoff.

### FreeRouting v2.4.1 + Semeru JRE 27.0.0.0 adoption record

- Checked on: 2026-09-23
- Update: new adoption (previously deferred in `docs/konnect-tools.md`)
- Primary sources:
  - [FreeRouting v2.4.1 release](https://github.com/freerouting/freerouting/releases/tag/v2.4.1)
  - [Semeru 27 binaries jdk-27.0.0.0 release](https://github.com/ibmruntimes/semeru27-binaries/releases/tag/jdk-27.0.0.0)
- Reason for adoption: Konnect v0.12.1's Specctra/FreeRouting tool family
  (`check_freerouting`, `export_specctra_dsn`, `route_specctra_dsn`,
  `plan_specctra_ses_import`, `apply_specctra_ses`) requires a Java runtime
  and the FreeRouting JAR, both previously absent from the image. The
  deferral was lifted to enable the autorouting path.
- Verification: container PoC confirmed `check_freerouting` reports
  `engine_found`, `java_available`, and `native_mcp_available` against
  `/opt/freerouting/freerouting.jar` on Semeru OpenJ9 27.0.0.0.
- Known upstream limitation (ADR-0021): `export_specctra_dsn` fails on
  boards saved by KiCad 11 nightly because Konnect v0.12.1 parses footprint
  positions as `(at x y)` while KiCad 11 serializes `(transform (translate
  ...) (rotate ...) (scale ...))`. DSN export, SES routing, and SES import
  remain unusable until upstream parses `transform`; `check_freerouting`
  itself is green.

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
    profile; it is adopted for user-attached images only, while workspace
    renders go through the MCP `ImageContent` / `file_editor view` path
    (ADR-0013).
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

### OpenHands SDK v1.49.4 adoption record

- Checked on: 2026-09-23
- Update: v1.49.3 → v1.49.4
- Primary source: [v1.49.4 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.49.4)
- Release notes summary:
  - `openhands-sdk` now resolves this server's own `LookupSecret` URLs
    in-process (#5026), and deleting the active ACP profile resets
    `agent_settings` (#5205).
  - Dependency bumps: `agent-client-protocol` to `>=0.12.1,<0.13.0` and
    `joserfc` to `>=1.7.5`.
- Reason for adoption: pin alignment to the latest patch; no public API
  surface used by this repository changed (no module added or removed in
  `openhands-sdk`/`openhands-tools`/`openhands-workspace`).
- Feature evaluation (checked against the plugin boundary):
  - The `LookupSecret` in-process resolution and the ACP profile reset are
    agent-server side fixes; they arrive via the next `circuit-server` image
    publish, which derives `sdk_version` from the installed pin and rebuilds
    on the SDK tag; `docker/image-digests.json` is updated by that bot flow,
    not by hand.
  - mcp 2.x remains deferred: v1.49.4 keeps the `fastmcp<4`
    (`fastmcp-slim: mcp<2.0`) constraint; see
    `scripts/dependency_update_deferrals.json`.

## Plugin and tests

Locally, `python3 -m circuit.mcp_server` can be started as a stdio MCP server.
`circuit_api_server_start` accepts only one `.kicad_pcb`; call
`circuit_api_server_stop` first when switching. Docker integration tests can be
run optionally with
`CIRCUIT_TOOLS_IMAGE=circuit-tools:dev uv run pytest tests/integration -m docker`
and are skipped in environments without the image.

### Runtime package resolution

The normative host procedure is: run the digest-pinned `circuit-server` image
(see "Docker image") and install the plugin. Nothing else is required — the
image supplies KiCad/konnect/libraries, and the plugin supplies the assets and
the launcher described below.

Installing plugin assets does not reinstall the `circuit` Python package, so a
plain `python3 -m circuit.mcp_server` can silently import a stale site-packages
copy that lacks tools the assets expect (observed as missing `circuit_sch_lint`
and missing `src_sha256` caching). Every entry point — `.mcp.json`, each
sub-agent's `mcp_config`, and the session_start doctor hook — therefore runs
through `plugins/circuit/scripts/circuit_launcher.py`, which prepends the first
matching source tree to `PYTHONPATH` and then execs the module:

1. `$CIRCUIT_SRC`
2. newest `~/.openhands/cache/extensions/electrical-circuit-agent-*/src`
   (the vendored snapshot matching the installed plugin)
3. `/opt/circuit/src` (the circuit-server image layout)
4. `<repo>/src` in a repository checkout
5. otherwise the already-installed package, with a stderr warning

`circuit.doctor` reports the resolved package path under `circuit-import` and
fails `package-features` when expected capabilities (`circuit.sch_lint`,
`kicad_cli.src_sha256` caching) are absent — run
`python3 plugins/circuit/scripts/circuit_launcher.py doctor` after install to
verify. On hosts not running the image, set `CIRCUIT_SRC` explicitly and
provide `kicad-cli`, `konnect`, and the CERN libraries separately; missing
tools remain fail-closed rather than being installed by the plugin.

The doctor `cern-libraries` check searches the following locations in order
and reports the first populated library (a directory containing `SchLib` and
`PcbLib`), so non-image hosts that install the libraries under `$HOME` still
pass:

1. `$CIRCUIT_CERN_LIBS`
2. `/opt/circuit/libraries/cern-kicad-libs` (image layout)
3. `~/opt/circuit/libraries/cern-kicad-libs`
4. `~/.openhands/cache/extensions/electrical-circuit-agent-*/libraries/cern-kicad-libs`
   (vendored snapshot, when the submodule content is present)

When none match, the check reports every searched path instead of failing
silently on a single fixed path.

### Plugin hardening

Each sub-agent's frontmatter records the library-protection hook and
`max_budget_per_run: 3.0`. The plugin-level Stop hook reads
`circuit-reports/design-report.json` under the working directory (up to depth
4) and presents each verdict as additional context at the end. KiCad/CERN
libraries covered by the `circuit-library-guard` skill are read-only; use
`register_*_library` instead of editing. Slash command `argument-hint`s specify
input files and export types.

### Plugin isolation on shared hosts

On an agent-server where several plugins are installed, ambient plugin hooks
from other products fire inside every conversation (their PreToolUse,
PostToolUse, and Stop hooks have no workspace scoping), which can deny tool
calls and inject foreign context mid-verification. `plugins: []` in the
conversation request does not suppress installed plugins. Before running an
end-to-end design verification on such a host, disable every installed plugin
except `circuit` (and re-enable them afterwards):

```bash
curl -X PATCH -H 'Content-Type: application/json' \
  -d '{"enabled": false}' \
  "$BASE/api/plugins/installed/<plugin-name>"
```

List installed plugins with `GET /api/plugins/installed` and verify isolation
by confirming that hook executions in the conversation events only reference
the circuit plugin root.

### Advisory visual review

`circuit_render` returns the PNG both as a JSON path and as an MCP
`ImageContent` block, so a vision-capable model sees the render directly in
the tool result (`file_editor view` on a PNG works the same way). The SDK
converts `mcp.types.ImageContent` to `data:` URLs and drops image blocks when
the model is not vision-capable, so the text result still carries alone.
`inspect_image_with_vision` (auto-attached when the model is non-vision and a
vision-capable saved profile exists) only inspects images in the latest user
message — the path for user-attached board photos or screenshots, not
workspace renders. A plugin `post_tool_use` hook records each
`inspect_image_with_vision` call's profile, model, question, and response hash
to `.openhands/circuit/vision-tool-events.jsonl` for cross-checking. All
visual evidence is advisory for human judgement (ADR-0012): it must never be
promoted to an ERC/DRC verdict, and when no vision path is available the
review records `advisory visual review skipped` and continues.

`circuit_render` covers three kinds (ADR-0016): `board3d` (`pcb render`
camera — six orthographic `side` views, `rotate`/`pan`/`pivot`, `zoom`,
`perspective`, `floor`, `background`, `quality`), `schematic` (`sch export
png`, per-page PNGs), and `layers` (`pcb export png`, per-layer PNGs); up to
four images per call are attached as `ImageContent`. `circuit_diff` accepts
`format: png|svg` for a visual diff. `circuit_export` kind `fp_svg` plots
each `.kicad_mod` in a footprint library directory to SVG. Base64 image
payloads inside `circuit_konnect_call` results are written to
`$CIRCUIT_KONNECT_IMAGE_DIR` (default `circuit-reports/konnect-images/`) and
replaced by `{"image_path", "sha256"}` provenance records — the Konnect
binary itself stays unmodified.

### Intake attachments and evidence binding

User-attached images are materialized to `<workspace>/intake/attachments/`
by the `intake-attachments` hook (session_start, user_prompt_submit, stop;
ADR-0017). The hook scans the agent-canvas event store
`~/.openhands/agent-canvas/dev_conversations/<session_id>/events/` —
override with `$CIRCUIT_AGENT_EVENTS_DIR` — decodes each `data:` image to
`<sha256[:12]>.<ext>`, and appends provenance to `manifest.jsonl`; the
output dir is overridable via `$CIRCUIT_INTAKE_ATTACHMENTS_DIR`. When the
events directory is unreachable (remote runtimes) the hook exits quietly
and the fallback is dropping files into `intake/` manually. `Assumption`
and `OpenQuestion` records may bind such a file with an `evidence` field
(`kind`, `path`, `sha256`, `note`); `check_intake` verifies existence and
hash — fail-closed.

### Visual review records and image observation provenance

A vision-capable review writes `circuit-reports/review-visual-<slug>.advisory.json`
per inspected image: `tool: "vision_review"`, `stage: "review"`, and `detail`
following `VisualReviewDetail` (`image_path`, `image_sha256`, `model`,
`checklist`, `findings[]` with a fixed category vocabulary and optional
normalized `bbox`; ADR-0018). Records are advisory and aggregate into the
design report like any advisory file. Separately, the `record-image-observation`
hook logs every image the model saw — `circuit_render`, `circuit_diff`, and
`file_editor view` calls — to `.openhands/circuit/image-observations.jsonl`
(path + sha256; override `$CIRCUIT_IMAGE_OBSERVATIONS`), complementing the
`vision-tool-events.jsonl` log of delegated `inspect_image_with_vision` calls.

### Existing-drawings ingestion and rasterizers

`circuit_import` wraps `kicad-cli sch import`/`pcb import` for foreign CAD
sources (Altium/Eagle/CADSTAR/EasyEDA(+Pro)/LTspice/PADS/DipTrace/PCAD/OrCAD
schematics; PADS/Altium/Eagle/CADSTAR/Fabmaster/PCAD/SolidWorks boards); the
importer's JSON report is written next to the output as
`<output>.import.json`. `circuit_rasterize` turns `.pdf` (pdftoppm, per-page
PNG) and `.svg` (rsvg-convert) into PNGs for the vision lane — the tools
image installs `poppler-utils` + `librsvg2-bin` (unpinned Ubuntu 26.04 apt;
override binaries via `$CIRCUIT_PDFTOPPM`/`$CIRCUIT_RSVG_CONVERT`;
ADR-0019). `circuit_stackup` writes `<stem>-stackup.json` plus a
deterministic section-diagram `<stem>-stackup.svg` (stdlib generator) — the
cross-section approximation of record; rasterize it for the vision lane.

### Schematic readability lint

`circuit_sch_lint` is a deterministic gate on the authored `.kicad_sch`:
it flags Reference/Value properties placed more than 30 mm from their symbol
(`property_far_from_symbol`), positioned items outside the sheet bounds
(`item_out_of_bounds`), missing property positions, and unparsable files as
fail-closed errors. Warning-severity findings cover readability defects ERC
cannot see: hidden or on-symbol Reference/Value properties (`property_hidden`,
`property_on_symbol`), empty title-block fields (`title_block_incomplete`),
placement using less than 30% of the sheet (`sheet_underutilized`), and
label-only connectivity with no wires (`label_only_connectivity`). Because
symbol property `at` values are absolute sheet coordinates, schematics written
by hand or by generated scripts tend to place every label at the sheet origin
— ERC and connectivity cannot detect that defect, this gate can. Run it after
schematic authoring and before ERC (`python3 -m circuit.sch_lint file.kicad_sch`);
the design flow treats an error verdict as a stop, and the authoring prompts
instruct the agent to repair warning findings through Konnect ops (field
position resets, label moves, `edit_sheet`, component moves) and re-lint.

### Canvas profile scoping

Agent Canvas v1.19+ supports `mcp_server_refs` (an agent profile can restrict
the MCP servers it exposes) and v1.20 adds profile secret scoping
(`profile_secret_scope_v1`). These are Canvas-side profile features — useful
to scope the `circuit` or `konnect` MCP servers to the layout/schematic
profiles that need them and to give a dedicated vision profile its own API
key — but they change Canvas profile configuration, not this repository, so
they are recorded here as configuration options only.

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

`kicad-cli pcb drc` on the nightly toolchain can report
`lib_footprint_mismatch` violations when a footprint stored in the board
differs from the installed library source (observed with CERN-library
footprints under 2026-09 nightly packages). These are library-skew warnings
inherent to the moving nightly libraries, not design errors; they count as
DRC warnings and do not by themselves change a passing verdict.

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

On 2026-09-23 the pins moved to the 09-23 core
`202609230242+3f88267300~189~ubuntu26.04.1`, symbols
`202609221218+1565b6644~12~ubuntu26.04.1`, and footprints
`202609222017+55d9dd1a3~14~ubuntu26.04.1`. The `_cvpcb.kiface` ERC failure was
re-tested on the built image: `kicad-cli sch erc` and the docker integration
tests pass, so the update was adopted. The librarian + SHA-256 mechanism stays
in place.

## Sockets and permissions

Pass a filesystem path such as `/tmp/circuit-smoke.sock` to the server's
`--socket`. Pass `ipc:///tmp/circuit-smoke.sock` to the client. Run the image as
non-root and make `$HOME` and `$HOME/.config/kicad` writable.

## Vision lane diagnostics

`python3 -m circuit.doctor` reports the best available vision lane as
`vision=<lane>`: `model` (vision-capable conversation model configured),
`profile` (a dedicated vision agent profile), `materialize-only`
(intake images can be materialized but no vision reader is set up), or
`none`. The check is warn-level: `none` emits `status: "warn"` and never
fails the diagnostic; vision stays advisory (ADR-0020). Available
rasterizers (`pdftoppm`, `rsvg-convert` — poppler-utils / librsvg2-bin,
ADR-0019) are listed in the detail line.

Model capability matrix observed on the deployment environment:

| Agent profile | Model | Vision |
| --- | --- | --- |
| `default` | kimi-k2.6 | no |
| `kimi-k3-vision` | kimi-k3 | yes |

Canvas guidance: for image-heavy sessions (sketch/photo intake, render
review) prefer starting the conversation on the `kimi-k3-vision` agent
profile; for a mid-session lane, use the `switch_llm` pattern from
ADR-0017 (`subagent_vision` model parameter). Per-profile API keys are
scoped via the provider-backed secrets mechanism documented in
"Canvas profile scoping" — the same profile selection keeps the
session-scoped key bound to the right provider lane.

With P5 the phased vision-deepening plan is fully implemented; ADR-0016
through ADR-0020 are the normative records (the research plan document
was removed).
