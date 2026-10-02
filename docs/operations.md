# Operations

## SBOM attestations

`publish-circuit-images.yml` generates and attests a package-level SPDX-2.3
SBOM for the tools image and uploads the full Syft SBOM as a 90-day workflow-run
artifact. It stores the returned URL as
`sbom_attestation`, which `locked-image-check.yml` verifies when present;
an absent URL warns and continues. SBOM steps are skipped when `skip_tools`
is active.
The attested SBOM omits file entries and relationships involving files to
stay below the 16 MiB limit.

## Launcher-side verification

`CIRCUIT_VERIFY_ATTESTATION` accepts `auto` (the default), `require`, or
`off`. Before pulling a lock-provided image, and on every `prewarm`, the
launcher uses `gh attestation verify` with the lock entry and publisher
workflow. `auto` prints one note and skips for an image override, missing
attestation, missing `gh`, or failed `gh auth status`; once verification
starts, failure or timeout prevents the pull. `require` makes skip conditions
errors, while `off` never verifies. Ordinary invocations do not re-verify a
locally present image, and `--warn` doctor paths never verify.

## Development environment

```bash
uv sync
uv run python scripts/verify_all.py --stage docs
uv run python scripts/verify_all.py --stage fast
python3 -m circuit.doctor
```

`pdfplumber==0.11.10` supplies the datasheet lane's word geometry, table
extraction, and vector-object counts. Poppler remains an independent text lane
for deterministic comparison. `pillow==12.3.0` is pinned for checking that
mechanically supporting words have visible ink in the fresh page render;
transitively available Pillow was made explicit because visibility is part of
the PartSpec acceptance boundary. Tesseract remains available for extraction,
but OCR-only text cannot mechanically satisfy PartSpec checks.

PartSpec verification re-derives cited pages from PDF bytes for every check.
Stored extraction JSON is limited to artifact-integrity and vision-evidence
checks; mechanical readings require matching Poppler and pdfplumber support in
a tight bbox or bound table cell and visible pixels in the re-derived PNG.
Dimension and pin-table values bind to individual cells, orderable rows bind
exact MPN/package-designator/pin-count triples, and package drawing pages bind
to a drawing ID and optional revision. Project-library verification is
recomputed from recorded inputs by the project gate; a stored passing verdict
is not authoritative. See [ADR-0025](adr/ADR-0025-part-library-evidence-authority.md).

Datasheet acquisition failures use a hash-bound HumanRequest. A provided
datasheet must be attached to the same user message as the response and is
checked for target MPN, requested revision, and required sections in both
Poppler and pdfplumber lanes before it can proceed. Confidential datasheets,
extractions, review crops, and review packets belong under
`<project>/.confidential/`; the generated `.gitignore` and tool guard are
policy safeguards, not a security boundary. Verification rejects a
confidential datasheet or extraction path outside that store. See
[ADR-0033](adr/ADR-0033-human-requests-and-confidential-datasheets.md).

Pinout geometry is freshly derived for the four supported leaded and no-lead
quad/dual families, then compared with footprint pad order and symbol names.
Package pin-1 corners always use top-view coordinates; per-pin view metadata
is recorded without changing pin interpretation. Review packets show a
pinout-view question and name-at-position comparison; see
[ADR-0027](adr/ADR-0027-pinout-orientation-oracles.md).

Datasheet visual reads must use `circuit_vision_read` crops tied to the source
PDF hash, page, bbox, and rasterizer. Record an answer and a multi-sentence
impression for every image, including the control image; failed submissions
write no answers. The fixed table prompt returns normalized rows of cell
strings, which PartSpec checks compare with the cited pin and orderable cells.
Pinout labels are also checked against freshly derived geometry when available.
Control identity is stored in exclusive, hash-bound `.vision-control/` sidecars
outside batches and authoring lanes, so later processes can re-derive reads.
Agent pre-tool guards deny references to those sidecars as context isolation,
not as a security boundary; see
[ADR-0028](adr/ADR-0028-tool-managed-vision-and-blind-authoring.md).
Image reads and author commits are recorded by their post-tool hooks when
observation logging is enabled. Configure
`vibebb-part-author-a` and `vibebb-part-author-b` independently for model
diversity; provisioning clones the active model profile, so identical models
are reported as such rather than treated as independent. The lanes use
Poppler and pdfium respectively, seal their PartSpecs with overall
impressions, and are compared only after both commits. Library verification
requires a valid authoring comparison; review packets bind sealed hashes and
show per-image and author impressions as human context, not as scores. See
[ADR-0028](adr/ADR-0028-tool-managed-vision-and-blind-authoring.md).

Land-pattern and library-verification MCP calls accept an optional
`rule_profile`, resolved from `<library_dir>/rules/<profile_id>.json`; for
land-pattern calls without an explicit library directory, the default is
`<part-spec directory>/library`. The Python API accepts an already-resolved
`EffectiveRules`. Built-in profiles are `builtin:ipc7351b` and
`builtin:kicad-generator`. User profiles form a parent-hash-bound chain and
must keep evidence paths project-relative. Organization and product profiles
need both a non-empty rationale and hash-verified evidence. Change a profile
by writing a new profile file with the parent's current SHA-256 rather than
editing an existing parent in place.

A tuned footprint uses a sibling
`<footprint>.kicad_mod.lineage.json` file. Bind the current footprint and base
hashes, name each changed pad field, give a reason, and include project-relative
evidence references with SHA-256 values. The verifier recomputes the changes
and keeps pinout, pin-1, lead-containment, clearance, courtyard, and silk
checks active. Review packets show lineage evidence and intentional deviations;
editing the lineage or effective rule chain invalidates the packet.

Library verification can run the pinned KLC checker as a GPL subprocess and
the KiCad test-board oracles. KLC findings are read from JUnit XML only;
functional groups are errors and naming/metadata findings are warnings.
Unavailable tools or unusable reports fail closed. Test-board DRC filters only
the expected `unconnected_items` finding; assembly and geometry oracles remain
authoritative. Footprint and symbol vision comparison results are advisory,
but mismatches require a blind human yes/no answer. Every packet-listed overlay
or comparison image must have a current hash-bound `vision_review` record with
a valid impression before approval. These review records are stored next to
their images at the `review_record_path` listed in `review.json`.
The image fetches KLC with a depth-one Git fetch of the pinned commit, verifies
the checked-out `HEAD` against that commit, and removes the checkout metadata.

## 3D model generation and inspection

`cadquery-ocp-novtk==8.0.1.0.0` supplies the Open Cascade STEP operations; no
additional system package is needed beyond `uv sync`. OCP imports are confined
to `src/circuit/occt.py`. The tools image installs this dependency through the
project's exported requirements and checks `import OCP` during its self-check.
The wheel bundles OCCT `libTK*.so` shared libraries; its OCP package metadata
and upstream OCCT LGPL-2.1-with-exception reference are recorded in
`THIRD_PARTY_NOTICES.md`.

Generated models use PartSpec nominal dimensions and emit separate body,
terminal, and exposed-pad solids with a deterministic STEP header and a
hash-bound generation manifest. Only `.step` and `.stp` are accepted, and
footprint model transforms must be identity. Supplied manufacturer/user STEP
files are imported through the existing provenance/license gate; the workflow
does not fetch models from manufacturers or aggregators.

Model verification checks units, solid closure and validity, positive volume,
round-trip geometry, body dimensions, terminal-to-pad bijection, courtyard,
and pin-1 marker evidence. Terminal geometry is measured in the `z=[0, 0.02]`
mm slab; terminal bboxes must fit their pads within `0.025 mm`, and row pitch
must match the PartSpec within `0.01 mm`. The KiCad export oracle independently
checks placement at 0 and 90 degrees by matching exported terminal regions to
the referenced STEP terminal regions, with relative volume tolerance `1e-4`
and center tolerance `0.02 mm`. It does not assume copper-pad centers equal
physical terminal centers because land pads can extend beyond the terminals.
Generated and imported models are also cross-checked for body extents,
terminal centers, and pin-1 quadrant.

The 3D vision comparison reuses the tool-managed vision batch and answer path.
It binds the PartSpec, footprint, STEP, and render hashes, and compares the
datasheet drawing with a same-scale KiCad render plus mirrored control.
Every image requires an answer and valid impression. Missing, stale, or
failed-control evidence is an error; a mismatch is advisory but requires a
human question and cannot override deterministic findings. See
[ADR-0031](adr/ADR-0031-step-model-generation-and-inspection.md).

Every project-library part also requires a human review bound to the current
PDF, PartSpec, symbol, footprint, 3D-model hashes, and verification settings.
Build a packet with `circuit_library_review_packet` or
`python -m circuit library-review packet`; open `01-blind.html` first and
answer its questions from the supplied evidence before consulting
`02-review.html`. Submit a user event beginning with
`CIRCUIT-LIBRARY-REVIEW <packet_id>` and the decision grammar shown in the
packet. `circuit_library_review_status` or
`python -m circuit library-review status` recomputes the current packet ID and
reports whether a valid user-event approval applies. Any relevant byte change
invalidates approval. `circuit_library_review_apply` applies corrections from
a validated reject only when the old values still match; corrections are
validated and recorded for regression checks.

The user-event hook stores a hash-bound pointer, and the write-protection hook
blocks normal agent writes to the event store. This is a policy barrier, not a
cryptographic identity mechanism: arbitrary code execution under the same
account can forge event files. Deterministic verification failures cannot be
overridden by human approval. Three-dimensional visual review is not included.
Human review remains mandatory unless the metrics-backed relaxation gate
described in [ADR-0032](adr/ADR-0032-library-release-evidence.md) explicitly
supports it. Configure `--review-scope relaxed` or the corresponding MCP
`review_scope` only after `circuit_library_metrics` has written a fresh,
hash-bound `library/library-metrics.json`; otherwise the review gate returns
`review_relaxation_not_supported_by_metrics`. The current default scope is
`full`.

## Golden corpus, mutation gate, and escape metrics

The sealed golden corpus lives at `library/corpus/corpus.json`; a project-local
`<project>/library/corpus/corpus.json` takes precedence when present. Truth
files and approvals are separately hash-bound. New entries start unconfirmed,
and corpus truth is not available to authoring lanes. Score with
`circuit_corpus_score`; a missing PDF or unconfirmed truth is not a pass.

Seeded critical mutations exercise symbols, footprints, PartSpecs, and STEP
models against the deterministic verification stack. A critical mutation is
detected only when at least two independent counting oracle families report
it: evidence, pin bijection, orientation, land geometry, export, model geometry,
or rule profile. Vision and integrity families are reported but do not count.
The `counting_family_count` field reports only those counting families.
Integrity findings identify hash, seal, manifest, approval, lineage, and stale
record problems; they do not show that consistently bound content agrees with
its evidence. A `single_oracle` or undetected critical mutation fails the
mutation gate; neither review approval nor metrics may waive that result.
Metamorphic checks cover PDF DPI, rotation, bottom-view mirroring, and export
invariants. The
PartSpec unit-conversion relation is explicitly skipped because PartSpec has no
unit field to transform. Pin validation compares Class A (cell-bound PartSpec)
with Class B (an independent machine-readable pin source such as IBIS or BSDL);
the two sources are not interchangeable or merged.

`circuit_library_metrics` computes the accepted human-confirmed sample size,
critical correction escapes, the exact one-sided 95% Clopper–Pearson upper
bound, family detection rates, and per-operator mutation outcomes.
`circuit_mutation_report` reads and validates the latest seeded mutation
report. The metrics snapshot binds the corpus manifest and mutation report
SHA-256 values and is recomputed against current inputs at the review gate.
Human review may be relaxed only when at least 299 accepted parts have an
upper bound below 0.01, the bound report has no critical `single_oracle`
mutation, and the metrics snapshot remains bound to the current corpus and
mutation report. Until all conditions hold, the review gate fails closed with
`review_relaxation_not_supported_by_metrics`.

See [ADR-0032](adr/ADR-0032-library-release-evidence.md) for the evidence and
release policy.

## Command line

`python -m circuit` is the unified dispatcher matching the sibling repos'
`python -m wire` / `python -m mech` convention; every subcommand prints a JSON
verdict to stdout and the verdict is fail-closed:

```text
python -m circuit doctor [--warn]
python -m circuit intake --brief BRIEF --intake INTAKE
python -m circuit sch-lint SCHEMATIC [--output PATH]
python -m circuit connectivity --brief BRIEF --out PATH [--netlist NETLIST]
python -m circuit author --brief BRIEF --workdir DIR [--intake INTAKE]
python -m circuit library-review packet --part-spec SPEC --symbol-lib LIB \
  --symbol-name SYMBOL --footprint FOOTPRINT --library-dir LIBRARY
python -m circuit library-review status --part-spec SPEC --symbol-lib LIB \
  --symbol-name SYMBOL --footprint FOOTPRINT --library-dir LIBRARY
```

`author` execs `scripts/e2e_authoring.py` and reports a JSON verdict; the e2e
script itself also fails closed — a bad brief, an intake block, or any step
error writes `{"verdict": "fail", "stage", "detail"}` to
`e2e-authoring.json` and stdout instead of a traceback. On success the run
also writes `workdir/provenance.json` (schema_version 1, license, generator
version, brief/intake SHA-256, tool versions) — the same provenance record
shape the sibling repos emit. `circuit_launcher.py` forwards `author`,
`intake`, and `sch-lint` to the dispatcher inside the tools image; other
first arguments still exec `python3 -m circuit.<arg>`.

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
`circuit.cern.commit`. Because the SDK v1.50.1 server image build requires
root-privileged apt/useradd on the base image, the tools image's default user is
root. For standalone runs specify `--user circuit`; in the server image use the
`openhands` user created by the SDK. Docker itself does not guarantee
determinism, so published digests are locked.

## CI/CD and digest lock

| Workflow | Role |
|---|---|
| `ci.yml` | fast verification plus tools image/smoke/standard verification depending on change scope |
| `publish-circuit-images.yml` | GHCR tools/server publishing, post-publish smoke, lock update bot PR |
| `locked-image-check.yml` | Digest-pinned image and launcher smoke verification on main push and weekly |
| `main-ci-failure-issue.yml` | Filing CI/image failure Issues on main and closing them on green |
| `check-dependency-updates.yml` | Weekly PPA/PyPI/GitHub/CERN/action update report |
| `workflow-lint.yml` | actionlint workflow validation and zizmor static analysis, uploaded to code scanning |

The only required secret is `GITHUB_TOKEN`. Require `fast`, `docker-smoke`,
`plugin-load`, and `zizmor` in branch protection. The publish workflow publishes
`ghcr.io/vibebb/circuit-tools` and `ghcr.io/vibebb/circuit-server`, and creates
`docker/image-digests.json` for the first time via a bot PR. Filling a missing
lock with placeholders is forbidden. Because GITHUB_TOKEN events do not start
workflows, the publish workflow dispatches the lock-branch CI itself and
merges the lock PR synchronously once that run succeeds; when the Actions
policy lands the bot PR's `pull_request` run as `action_required`, it approves
the run via the Actions API. After merging, it dispatches `ci.yml` and
`locked-image-check.yml` on main as observational runs recorded in the step
summary.

The publisher creates a GitHub build-provenance attestation for the
`circuit-tools` image and stores its URL in the `circuit_tools` lock entry,
which is mirrored into the plugin lock. When the URL is present,
`locked-image-check.yml` verifies the image digest against
`publish-circuit-images.yml` before pulling it. The workflow preserves its
image-internal Konnect smoke and also prewarms the locked image through
`circuit_launcher.py`, runs `doctor` and the shipped LED-loop authoring gate,
and uploads its reports even on failure. Older locks without attestation
metadata emit a warning and continue; a failed provenance verification fails
the image check.

The scheduled dependency check writes Markdown and JSON reports under the
runner's temporary directory, adds the run URL to the Markdown and step
summary, and exposes the JSON `outdated_count` as the workflow's `outdated`
output and `unknown_count`. Fetch failures remain unknown rather than
outdated, and the report Issue stays open until both counts are zero.
The report can be checked locally as follows. `--dry-run` prints it to stdout
without changing GitHub Issues.

```bash
uv run python scripts/check_dependency_updates.py --dry-run
```

`verify_all.py --stage standard` requires `CIRCUIT_TOOLS_IMAGE` and runs Docker
integration in addition to the fast checks. Within a stage, barrier-marked
commands (currently `uv sync --locked`) run alone and consecutive non-barrier
commands run in parallel up to `--jobs` workers; `--jobs 1` restores the
previous sequential order and `--list` dumps the command table with barrier
flags.

For direct PyPI dependencies, the resolved version in `uv.lock` is reported as
the current value rather than the specifier in `pyproject.toml`. For specifiers
with an upper bound, the latest release within the range is compared and the
latest release outside the range is noted. Transitive drift is reported from
`uv lock --upgrade --dry-run` (`Update`/`Add`/`Remove` lines for packages that
are not direct dependencies). Candidates deferred due to constraints such as
the SDK are recorded with a reason and a re-check deadline
in `scripts/dependency_update_deferrals.json`, and until the deadline they are
counted as `保留（記録済み）` (deferred, recorded) rather than as updates.
Candidates past their deadline return to the update candidates as
`保留期限切れ` (deferral expired). Malformed JSON is fail-closed and reported as
FAIL. The script keeps the repo-specific targets in a constants block at the
top, and the shared check functions mirror the sibling repositories' checker
so fixes port 1:1.

The check also covers the `[tool.uv] required-version` pin (compared to the
latest `uv` release on PyPI), Python minor pins (`requires-python`, any
`PYTHON_VERSION` ARG or `uv python install` lines in the Dockerfile, and
`python-version:` entries in workflows — all compared to the latest stable
CPython minor from `git ls-remote --tags`), and the Dockerfile `FROM` images:
`ubuntu` tags from the Docker Hub tags API and the `ghcr.io/astral-sh/uv`
image tag against the latest `uv` release. `@sha256:` digest suffixes on
`FROM` references are ignored for tag comparison.

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

### OpenHands SDK v1.49.5 adoption record

- Checked on: 2026-09-23
- Update: v1.49.4 → v1.49.5
- Primary source: [v1.49.5 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.49.5)
- Release delta:
  - `openhands/sdk/utils/masking.py` adds `PreserveDataUrls` /
    `SkipSecretMasking`; `message.py` `image_urls` and `openhands-tools`
    `browser_use` `screenshot_data` now keep image `data:` URLs intact
    under secret masking — beneficial for the render/vision path.
  - `mcp/tool.py` normalizes mcp 2.x snake_case ↔ camelCase wire keys
    (forward-compat only; `fastmcp<4` still caps `mcp<2.0`).
  - `model_features.py` adds the `gpt-6` family and `gpt-5.2-codex`;
    `telemetry.py` adds `UsageSnapshot`; extensions metadata utf-8 fix.
- Reason for adoption: pin alignment to the latest patch; the plugin
  boundary (AgentDefinition, skills, hooks, `.mcp.json`) is unchanged.
- Feature evaluation (checked against the plugin boundary):
  - MCP `ToolAnnotations` adopted: every `circuit_*` tool now declares
    `annotations.title` plus `readOnlyHint`/`destructiveHint`/
    `idempotentHint`/`openWorldHint` so MCP clients (including
    AgentCanvas) can gate calls on honest write semantics.
    `circuit_konnect_call` is the only `destructiveHint: true` tool
    (arbitrary Konnect ops mutate the live board).
  - MCP tool `outputSchema`/`structuredContent` not adopted: tools return
    a `CallToolResult` JSON text envelope; a typed output schema
    duplicates contracts already documented in the input schemas.
  - `prompt`/`agent` hook types not adopted: hooks stay stdlib `command`
    only (deterministic/fail-closed invariant).
  - plugin.json `$schema` not adopted: tolerated-but-unenforced by the
    SDK loader (`extra="allow"`).
  - mcp 2.x remains deferred: v1.49.5 keeps the `fastmcp<4`
    (`fastmcp-slim: mcp<2.0`) constraint; see
    `scripts/dependency_update_deferrals.json`.
- Named feature review (checked 2026-09-24 against `openhands-sdk` /
  `openhands-tools` 1.49.5 sources plus upstream `main` —
  `sdk/subagent/schema.py`, `sdk/subagent/registry.py`,
  `sdk/conversation/impl/local_conversation.py`,
  `sdk/conversation/secret_registry.py`, `sdk/security/`,
  `sdk/llm/llm_profile_store.py`, `tools/task/manager.py`, agent-server
  `conversation_service.py`/`conversation_router.py`):
  - `EnsembleSecurityAnalyzer` / `LLMSecurityAnalyzer` /
    `ToolShieldLLMSecurityAnalyzer` / `GraySwanAnalyzer` not adoptable at
    plugin boundary: `AgentDefinition` has no `security_analyzer` field;
    analyzers attach via `Conversation.state` or the agent-server
    `POST /conversations/{id}/security_analyzer` route — server-side only.
    Plugin-side substitute adopted: the `safety-rail` `pre_tool_use` hook.
  - `permission_mode: confirm_risky` **switched to `never_confirm`**:
    `task/manager.py` never calls `set_security_analyzer` on the child
    `LocalConversation`, so every sub-agent action was `UNKNOWN` and
    `ConfirmRisky(confirm_unknown=True)` auto-resumed — zero gating plus
    status churn. All circuit sub-agents now declare `never_confirm`;
    revisit if the SDK propagates the parent's analyzer.
  - `SecretRegistry` adoptable via contract docs: `${VAR}` /
    `${VAR:-default}` in `mcp_config` resolves through
    `secret_registry.get_secret_value` before env, and the launcher
    inherits the process env for the stdio MCP servers, so a
    canvas-registered `CIRCUIT_*` secret reaches `circuit_*` tool code
    end-to-end. Registry values also reach bash commands that name the
    key.
  - `StuckDetector` already effective: `stuck_detection=True` is the
    `LocalConversation` default, including task sub-agents; thresholds
    are Conversation init params (not plugin-settable), and
    `max_iteration_per_run` remains the repo-side bound.
  - Persistent memory (`AgentContext(load_memory=True)`) not adoptable
    per sub-agent (the factory builds `AgentContext` without it);
    top-level conversation only — seeded via `.openhands/memory/MEMORY.md`
    for hosts that enable Canvas "Settings > Agent Context".
  - Model routing adopted via profile convention: `Router` itself is
    server-side; authoring sub-agents declare `model: vibebb-author`,
    circuit-review `model: vibebb-review` (profiles resolved from
    `~/.openhands/profiles/` via `LLMProfileStore`; a missing profile
    hard-fails the `task` spawn). The session_start
    `ensure-llm-profiles` hook clones the conversation's
    `active_profile` into `vibebb-author.json`/`vibebb-review.json` when
    they are absent, so `task` delegation works out of the box and
    operators can re-point each lane afterwards; `model: inherit`
    remains the local fallback. `profile_store_dir` stays discouraged
    (splits provider-connections resolution).
  - `SwitchLLMTool` / agent profiles (`mcp_server_refs`, `secret_refs`) /
    critic not adoptable at plugin boundary — server-side scoping;
    circuit-review already plays the critic role at L2.
  - `condenser:` frontmatter not adopted: sub-agents get a summarizing
    condenser by default at factory time (`default_condenser`).

### OpenHands SDK v1.49.6 adoption record

- Checked on: 2026-09-25
- Update: v1.49.5 → v1.49.6
- Primary source: [v1.49.6 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.49.6)
- Release delta:
  - Meta-profile routing: `llm/meta_profile_store.py`,
    `tool/builtins/classify_and_switch_llm.py`, agent-server
    `meta_profiles_router.py`, example `59_route_task_to_model.py` — a
    `ClassifyAndSwitchLLMTool` builtin routes task classes to saved LLM
    profiles via `~/.openhands/meta-profiles/` (or
    `$OH_PERSISTENCE_DIR/meta-profiles`); a classifier miss fails loudly.
  - `verified_models.py`: adds `gpt-6-sol`, `gpt-6-luna`,
    `claude-opus-5-5`; drops `claude-opus-4-8`.
  - `hooks/executor.py`: a non-string `decision` in hook JSON is treated
    as no decision.
  - `llm.py`: friendly error on an invalid API key; refresh the key and
    retry once on a 401.
  - `mcp/oauth.py`, `mcp/utils.py`, agent-server `mcp_oauth_store.py` /
    `mcp_router.py`: OAuth token refresh fixes; agent-server Windows
    crash fix.
- Reason for adoption: pin alignment to the latest patch; the plugin
  boundary (AgentDefinition, skills, hooks, `.mcp.json`) is unchanged.
- Feature evaluation (checked against the plugin boundary):
  - Meta-profile routing not adoptable at plugin boundary: routing is a
    conversation-level builtin plus operator-side config; AgentDefinition
    frontmatter cannot pin a meta-profile per sub-agent. An operator may
    define a meta-profile whose classes resolve to the existing
    `vibebb-author` / `vibebb-review` profiles; no repo change.
  - New verified models: no action — `model:` resolves named profiles
    through `LLMProfileStore`, never raw model strings, so the
    `claude-opus-4-8` removal only surfaces as a Canvas warning on
    operator profiles that still point at it.
  - Non-string hook `decision` hardening: inherent — all circuit hooks
    emit `decision` as a string; malformed hook JSON now records no
    decision instead of crashing the decision parse, matching the
    fail-closed posture.
  - LLM 401 refresh-and-retry / friendly invalid-key error: inherent —
    runtime resilience, no repo change.
  - MCP OAuth refresh fixes: n/a — `circuit_*`/`konnect_*` servers are
    stdio subprocesses with no OAuth surface.
  - `permission_mode` stays `never_confirm`: the v1.49.5 finding stands
    (sub-agent conversations still get no security analyzer from
    `task/manager.py`).
  - mcp 2.x remains deferred: v1.49.6 keeps the `fastmcp<4`
    (`fastmcp-slim: mcp<2.0`) constraint; see
    `scripts/dependency_update_deferrals.json`.
- AgentCanvas 1.23.0 → 1.24.0 (verification host): the canvas now calls
  the agent-server runtime directly instead of `/api/cloud-proxy` (fixes
  405 on verification confirm and compact-context), keeps MCP OAuth
  credentials on saves, skips consent when tokens still work, and warns
  on unavailable models in saved LLM profiles. Runtime-surface only —
  no repo change.

### OpenHands SDK v1.50.0 adoption review

- Checked on: 2026-09-30
- Update: v1.49.6 → v1.50.0
- Primary source: [v1.50.0 release](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.50.0)
- Release delta: reviewed all 25 commits from v1.49.6 through v1.50.0.
- Feature evaluation (checked against the plugin boundary):
  - MCP startup failures now keep the conversation alive and report absent
    plugin tools; inherent in the SDK. The doctor hook already reports image
    availability and circuit agents remain fail-closed.
  - Managed-proxy budget denials stop without retry backoff; inherent.
  - The refresh-on-401 hook applies to agent-server images built from this
    pin; the publish workflow builds the server image.
  - Optional Canvas app backends, the client-owned browser event stream,
    goal mode, TypeScript-only changes, and internal refactors are not
    applicable to this plugin.
  - anyio 4.14.2 is picked up by the lock refresh. Async secret resolution,
    null cache-token handling, profile pre-flight system ordering, condenser
    prompt preservation, provider-gated prompt cache keys, and aiosqlite
    0.22.1 arrive with the SDK; no plugin code change is needed.
  - MCP input schemas are hand-written without `anyOf`; the schema fix does
    not affect them. Vision helper documentation confirms the existing review
    boundary; no vision routing change is adopted.
  - OpenAPI tool-metadata exemptions and documentation, CI, and release
    changes are n/a; the helper refactor is internal.
  - The SDK still requires `fastmcp>=3.2.0,<4`, which requires `mcp<2`;
    MCP 2.x remains deferred in
    `scripts/dependency_update_deferrals.json`.
- KiCad and library update review:
  - KiCad core moved from `202609250241+83b5faf3d5~189~ubuntu26.04.1` to
    `202609290253+1dd7ad3604~189~ubuntu26.04.1`; the compare includes 255
    commits. Reviewed changes cover schematic editing, symbol/reference
    handling, PCB routing and DRC, Gerber parsing, import/export, and 3D
    viewing. No plugin API or fixture format change is required; schematic
    ERC was checked against the existing fixture.
  - Footprints moved to
    `202609270717+b5e7a752f~14~ubuntu26.04.1`; its change sets the fiducial
    property in the fiducial generator. Symbols moved to
    `202609271717+716edc43f~12~ubuntu26.04.1`; its change adds the
    ISL28291FRUZ operational amplifier symbol. These are additive library
    updates and do not alter the fixture's existing library references.
  - The CERN library submodule moved from
    `4fc6742b43f7b8d59f48c80de7c424fe7841b40b` to
    `7618368c1cc70478024ed84882d54c0dade7dc86` (2026-09-30). Three
    synchronization commits carry the same upstream conversion source
    `6b6a01e0`; the library files and generated checksums are refreshed while
    the upstream license files remain unchanged.
  - All three KiCad Debian assets were fetched from Launchpad and their
    SHA-256 values recomputed for the Dockerfile. The existing schematic ERC
    and Docker integration smoke passed on the updated image. The build
    verified all three asset checksums; `kicad-cli sch erc
    --exit-code-violations --format json` returned zero violations for
    `fixtures/smoke-board/board.kicad_sch` (`kicad_version` 10.99.0), with
    no `_cvpcb.kiface` undefined-symbol failure. The Konnect integration
    smoke passed.
- AgentCanvas v1.24.0 (2026-09-25) remains the latest release and was
  evaluated with the v1.49.6 update. OpenHands/OpenHands#17822 (inline
  artifact previews) remains deferred until release. SDK #5360/#5367
  (DeepSeek vision serialization) remains deferred until #5367 ships;
  circuit-review must not use DeepSeek vision before then. SDK #5351 is n/a
  because plugin agents do not disable default tools. SDK #5381 is closed,
  not planned; circuit sub-agents launch only this plugin's own fail-closed
  MCP server.
- Vision path: vision-capable `vibebb-review` / `vibebb-author` profiles
  receive images opened by `file_editor view` and MCP `ImageContent` directly.
  `inspect_image_with_vision` covers only images in the latest user message,
  not rendered workspace files; no repo change.
- uv 0.12.19 → 0.12.21:
  - 0.12.20 lockfile reuse for semantically equivalent declarations,
    repeated-requirement `--require-hashes`, failed-upgrade restoration,
    XDG_CONFIG_DIRS fix, and panic fixes are inherent.
  - 0.12.20 lockfile-normalization, pylock.toml group/path fixes, and
    tool-install-locks dedupe are preview-only and n/a.
  - 0.12.21 OpenSSL 3.5.9, omitted empty `[manifest]` tables,
    post-/pre-release compatibility fix, and `uv python pin --rm` global-file
    fix are inherent; preview `resolution-inputs` is n/a.
- Reason for adoption: pin alignment to the requested SDK/tool versions and
  the latest dependency-checker KiCad/CERN candidates; the plugin's fail-closed
  authoring boundary remains unchanged.
- Verification: `uv sync --locked --all-groups`, Ruff check/format, Pyright,
  `pytest -q`, `scripts/verify_all.py --stage fast`, plugin load, and docs
  verification passed (311 tests passed, 4 skipped). The dependency checker
  was rerun with `GH_TOKEN` from `gh auth token` after the unauthenticated
  request returned HTTP 403; it reported only deferred items and no update
  candidates. AnyIO resolves to 4.15.1. See
  `/home/ubuntu/work/verify/electrical-circuit-agent-deps.log` for full output.

### OpenHands runtime surfaces

Runtime policy surfaces the plugin declares but the host executes:

- `permission_mode: never_confirm` on every circuit sub-agent (see the
  v1.49.5 record above for the `confirm_risky` finding).
- `model:` resolves through `LLMProfileStore` (`~/.openhands/profiles/`):
  `vibebb-author` for circuit-brief/schematic/layout, `vibebb-review`
  for circuit-review. Create the profiles (canvas LLM settings or
  `LLMProfileStore.save`) before invoking the agents — a missing profile
  raises `ValueError` at task spawn. Fall back with `model: inherit`.
- Secrets: `${VAR}` / `${VAR:-default}` in `mcp_config` expands through
  the conversation `SecretRegistry` before env; the launcher passes the
  process env through to the stdio MCP servers, so a canvas-registered
  `CIRCUIT_*` secret reaches `circuit_*`/`konnect_*` tool code
  end-to-end.
- The `safety-rail` `pre_tool_use` hook (`hooks/scripts/safety_rail.py`)
  denies a deterministic denylist on terminal commands: root/home `rm
  -rf`, block-device writes, power commands, and the git operations the
  working agreement bans. Advisory depth, not a security analyzer — it
  passes everything it does not positively recognize.
- `.openhands/memory/MEMORY.md` seeds the project-tier persistent
  memory loaded when the host enables `AgentContext(load_memory)`; the
  agent maintains the index, keep the seed to durable facts only.
- `StuckDetector` is on by default for every conversation including
  task sub-agents.

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

Installing plugin assets does not reinstall the `circuit` Python package, and
the host interpreter is not guaranteed to carry KiCad or the package
dependencies, so every entry point — `.mcp.json`, each sub-agent's
`mcp_config`, and the session_start doctor hook — runs through
`plugins/circuit/scripts/circuit_launcher.py`, which execs the module inside
docker (`docker run --rm -i --network none --user uid:gid`, workspace mounted
at its own path). The launcher resolves the tools image in order:

1. `$CIRCUIT_TOOLS_IMAGE` (a full ref, optionally digest-pinned)
2. `plugins/circuit/tools-image.json`,
   `plugins/circuit/skills/*/tools-image.json` (the pin ships inside the
   plugin, rewritten by the publish workflow), or the repo cache's
   `docker/image-digests.json` (`circuit_tools` entry)
3. none resolvable, or the pinned ref cannot be pulled -> error
   (docker-only: the launcher never falls back to a local build)

and mounts the first matching source tree read-only at `/plugin-src`
(`PYTHONPATH`):

1. `$CIRCUIT_SRC`
2. newest `~/.openhands/cache/extensions/electrical-circuit-agent-*/src`
   (the vendored snapshot matching the installed plugin)
3. `/opt/circuit/src` (the circuit-tools image layout)
4. `<repo>/src` in a repository checkout
5. otherwise the image's own baked package is used

Cache candidates are searched under both `$HOME` and the account's real
home, so a `HOME` override applied to the container cannot blind the
launcher. Inside the container `HOME`/`TMPDIR`/`XDG_*` are pinned to `/tmp`:
the image runs as the host uid, whose passwd entry and home do not exist
there, and a forwarded host home left fontconfig/KiCad without writable
directories. In `--warn` doctor mode (the SessionStart hook) the launcher
reports a missing local image instead of pulling it inside the 60s hook
timeout — run `circuit_launcher.py prewarm` to fetch it.

The KiCad API socket (`KICAD_API_SOCKET`, default `/tmp/circuit-kicad.sock`) is
bind-mounted into the container when it exists on the host; `konnect` runs
inside the image as an unmodified AGPL binary spawned by `circuit.mcp_server`.

`circuit.doctor` reports the resolved package path under `circuit-import` and
fails `package-features` when expected capabilities (`circuit.sch_lint`,
`kicad_cli.src_sha256` caching) are absent — run
`python3 plugins/circuit/scripts/circuit_launcher.py doctor` after install to
verify. Missing tools fail closed inside the container rather than falling
back to the host.

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
to `observations/circuit/vision-tool-events.jsonl` for cross-checking. All
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
`file_editor view` calls — to `observations/circuit/image-observations.jsonl`
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
placement using less than 30% of the sheet (`sheet_underutilized`),
label-only connectivity with no wires (`label_only_connectivity`), and
power flags stacked within 15 mm of each other (`power_flag_crowded`). Because
symbol property `at` values are absolute sheet coordinates, schematics written
by hand or by generated scripts tend to place every label at the sheet origin
— ERC and connectivity cannot detect that defect, this gate can. Run it after
schematic authoring and before ERC (`python3 -m circuit.sch_lint file.kicad_sch`);
the design flow treats an error verdict as a stop, and the authoring prompts
instruct the agent to repair warning findings through Konnect ops (field
position resets, label moves, `edit_sheet`, component moves) and re-lint.
`inject_title_block` also wraps over-long comments into consecutive
`(comment N ...)` fields so no printed line overruns the frame, and e2e
authoring picks the smallest `paper` size that fits the placement grid
(`titleblock.paper_for_part_count`). Host-side schematic edits
(`inject_title_block`, `set_paper_size`, `fit_sheet`) re-parse the result
as a complete root s-expression and write it atomically (temp file +
rename), so a crash or a concurrent reader can never observe a truncated
file; keep them out of the way of a live Konnect authoring session —
single-writer discipline. For `item_out_of_bounds` findings on labels,
`python -m circuit fit-sheet` (`circuit.fit_sheet.clamp_labels`) moves
each offending label to the nearest in-sheet point — labels alone,
because a label outside the sheet cannot be attached to anything, while
symbol or wire positions are electrical and must be re-placed through
Konnect ops. e2e authoring runs the clamp between title-block injection
and the lint gate.

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

Before ERC, `e2e_authoring.py` injects the schematic title block (`title`,
`date`, `rev`) with `circuit.titleblock.inject_title_block`, so generated
renders carry document metadata and the `title_block_incomplete` sch_lint
warning does not fire. The sch_lint gate verdict, warning count, and full
findings are also recorded under the `sch_lint` key in
`e2e-authoring.json` so warnings stay visible in the run summary.

Two similarly named JSON artifacts are easy to confuse:
`<name>.connectivity.json` is the netlist-vs-brief **gate report**
(`ConnectivityReport`, written under `circuit-reports/` by e2e authoring),
while `<name>.connectivity-source.json` is the wire-agent **`ConnectivitySource`
import contract** emitted by `circuit_connectivity_export` /
`python -m circuit connectivity`. The gate report is a verdict; the export is
a data contract — they share no schema. New exports should keep the
`-source` suffix so readers and glob rules can tell them apart.

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

Later on 2026-09-23 the symbols pin moved to the 09-23 symbols build
`202609231218+2ad44fc37~12~ubuntu26.04.1` (core and footprints unchanged;
the PPA dropped the previous symbols build once the newer one published).
The `_cvpcb.kiface` ERC failure was re-tested on a minimal image built with
the updated symbol set: `kicad-cli sch erc` on
`fixtures/smoke-board/board.kicad_sch` returns a clean report
(`kicad_version 10.99.0`, no violations), so the update was adopted. The
librarian + SHA-256 mechanism stays in place.

On 2026-09-25 the pins moved to the 09-25 core
`202609250241+83b5faf3d5~189~ubuntu26.04.1` and symbols
`202609251227+151fb6a8c~12~ubuntu26.04.1` (footprints unchanged), and the
CERN submodule moved to `4fc6742b43f7b8d59f48c80de7c424fe7841b40b`
("Update KiCad libraries", 2026-09-25 01:14 UTC). The `_cvpcb.kiface` ERC
failure was re-tested on a minimal image built with the new core and
symbol set: `kicad-cli sch erc` on `fixtures/smoke-board/board.kicad_sch`
returns a clean report (`kicad_version 10.99.0`, no violations, no
`undefined symbol` crash), so the update was adopted. The librarian +
SHA-256 mechanism stays in place.

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

## CI runner network auditing

CI and image-publishing jobs use `step-security/harden-runner` in audit-only mode. It observes network egress without blocking requests; per-run insights are available in the GitHub Actions job summary.

## Digest-lock PR verification

The publisher dispatches `ci.yml` and `workflow-lint.yml` on the lock branch, then polls the authoritative required-check set for up to 30 minutes. Non-required failures do not block publishing; a concluded required-check failure or a PR closed without merge fails the job. A PR merged externally triggers the existing post-merge main workflows without waiting for their results. If required checks remain pending at the deadline, the publisher arms squash auto-merge with branch deletion and exits successfully so branch protection can complete the merge.

SPDX generation prefers the GHCR registry source, writes temporary data under
the runner's temporary directory, and disables file metadata. The publisher
removes file entries and relationships involving files to produce the
package-level SPDX-2.3 SBOM. A guard reports disk space and the attested SBOM
size after transformation and fails above 16 MiB; the full Syft SBOM is
uploaded as a 90-day workflow-run artifact.
