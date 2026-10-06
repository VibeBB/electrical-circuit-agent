# Modules

Every module in `src/circuit`, its purpose, and its public functions/classes
(one line each; see the source docstrings for detail).


## `__init__.py`

circuit-agent package.


## `__main__.py`

Package marker.


## `advisory.py`

Advisory Konnect observations kept separate from deterministic gates.

- `VisualFinding` — One advisory observation from a visual review of an image.
- `VisualReviewDetail` — `detail` payload of a `vision_review` AdvisoryResult record.
- `parse_visual_review` — Return the typed detail when `result` is a vision_review record.
- `AdvisoryResult:`
- `merge_detail:`
- `review_record_path` — `review-visual-<slug>.advisory.json` next to the image (or out_dir).
- `build_review_record` — Build the typed `vision_review` advisory record for one image.
- `write_review_record` — Validate and write `review-visual-<slug>.advisory.json`; returns it.

## `apiserver.py`

Lifecycle management for a headless KiCad API server.

- `ApiServerError` — Raised when the KiCad API server cannot be managed.
- `ApiServerState:`
- `start:`
- `status:`
- `stop:`

## `authoring.py`

Blind, hash-sealed dual authoring for datasheet-derived PartSpecs.

- `AuthoringError` — Raised when authoring inputs or sealed records are not trustworthy.
- `AuthoringIssue:`
- `AuthoringDisagreement:`
- `AuthoringComparison:`
- `commit_lane:`
- `normalize` — Flatten authored PartSpec values while excluding evidence references.
- `compare_runs:`
- `write_comparison:`

## `brief.py`

Machine-readable design brief models and helpers.

- `Placement:`
- `Part:`
- `Net:`
- `Board:`
- `DesignBrief:`
- `load_brief:`
- `brief_sha256:`
- `expected_nets:`

## `cli.py`

circuit command line interface.

- `cmd_doctor:`
- `cmd_intake:`
- `cmd_sch_lint:`
- `cmd_fit_sheet:`
- `cmd_review_record` — Write `review-visual-<slug>.advisory.json` for a reviewed image.
- `cmd_datasheet_revision_check:`
- `cmd_library_review:`
- `cmd_connectivity:`
- `cmd_firmware_export:`
- `cmd_firmware_check:`
- `cmd_record:`
- `cmd_ux:`
- `cmd_author:`
- `main:`

## `confidential.py`

Helpers for project-local confidential artifacts.

- `project_root_for:`
- `confidential_root_for:`
- `ensure_confidential_store:`

## `board_geometry.py`

Board geometry handoff for mechanical-agent (`*.board-geometry.json`, optional IDF 3.0).

- `BoardGeometry:`
- `board_geometry:`
- `idf_files:`
- `write_board_geometry:`
- `board_geometry_result:`

## `connectivity.py`

Emit the wire-agent ConnectivitySource contract (*.connectivity.json).

- `ConnectivityError` — Raised when a ConnectivitySource payload cannot be built.
- `signal_class:`
- `pin_key:`
- `connectivity_source:`
- `write_connectivity:`
- `connectivity_result:`
- `connectivity_failure:`
- `main:`

## `connplace.py`

Connector mating-envelope and board-edge placement checks.

- `ConnectorPlacementFinding:`
- `ConnectorPlacementReport:`
- `check_connector_placement:`

## `corpus.py`

Hash-checked golden corpus inputs and read-only scoring.

- `CorpusError` — Raised when a corpus manifest or truth file is invalid.
- `CorpusDatasheet:`
- `CorpusEntry:`
- `CorpusManifest:`
- `CorpusDimension:`
- `CorpusDimensions:`
- `CorpusPad:`
- `CorpusMechanicalHole:`
- `CorpusConnector:`
- `CorpusTruth:`
- `CorpusApproval:`
- `CorpusFinding:`
- `CorpusScore:`
- `sha256:`
- `load_manifest:`
- `load_truth:`
- `approval_packet_id:`
- `score_part` — Compare parsed artifact values with truth without reading or writing files.
- `score_entry:`

## `datasheet.py`

Dual-lane extraction of PDF datasheets and deterministic page artifacts.

- `DatasheetError` — Raised when a datasheet extraction cannot be completed safely.
- `PdfWord:`
- `LaneResult:`
- `PageExtraction:`
- `DatasheetExtraction:`
- `word_has_ink:`
- `words_by_ink:`
- `poppler_words:`
- `pdfplumber_words:`
- `extract_datasheet` — Extract text, tables, and page images from a PDF datasheet.
- `load_extraction:`
- `page_words` — Load successful lane words for a page in the requested lane order.
- `page_tables` — Load extracted table rows for a page.
- `check_received` — Check both text extraction lanes against a datasheet acquisition request.

## `doctor.py`

Installation diagnostics for the circuit plugin.

- `checks:`
- `main:`

## `firmware.py`

Firmware link: MCU pin connectivity out, firmware pin map check in.

- `FirmwareLinkError` — Raised when firmware connectivity cannot be built.
- `McuPin:`
- `Mcu:`
- `CircuitFirmwareConnectivity:`
- `PinmapEntry:`
- `FreePad:`
- `FirmwarePinmap` — Mirror of firmware-agent's ``FirmwarePinmap`` (``*.fw-pinmap.json``).
- `FirmwareCheckReport:`
- `sha256_file:`
- `pad_of` — KiCad pin function -> pad name (``GPIO26_ADC0`` -> ``GPIO26``).
- `firmware_connectivity:`
- `write_firmware_connectivity:`
- `load_pinmap:`
- `check_firmware_pinmap:`

## `drawing_sheet.py`

ISO 7200 drawing sheet (``.kicad_wks``) and project text variables.

- `sheet_text` — Deterministic ``.kicad_wks`` text for the ISO 7200 sheet.
- `variables` — Project text variables the sheet prints; unset fields print a dash.
- `apply` — Write ``<stem>.kicad_wks`` beside ``project`` and point the project at it.

## `fit_sheet.py`

Clamp out-of-bounds net labels back inside the sheet frame.

- `FitSheetError` — Raised when the schematic cannot be clamped safely.
- `clamp_labels` — Move every out-of-bounds label item to the nearest in-sheet point.
- `main:`

## `gerber.py`

Fail-closed parsers for the KiCad Gerber and Excellon subset used by export checks.

- `ExportParseError` — Raised when a manufacturing export uses an unsupported encoding.
- `GerberFeature:`
- `GerberFile:`
- `DrillHit:`
- `ExcellonFile:`
- `parse_gerber:`
- `parse_excellon:`

## `humanrequest.py`

Hash-bound requests for human decisions and evidence.

- `HumanRequestError` — Raised when a stored request or response binding is invalid.
- `RequestSubject:`
- `RequestEvidence:`
- `RequestAlternative:`
- `DatasheetAcquisitionDetails:`
- `SubstitutePermissionDetails:`
- `AlternativeEvidenceFile:`
- `AlternativeEvidenceDetails:`
- `LibraryReviewDetails:`
- `HumanRequest:`
- `HumanResponse:`
- `SubstitutePermit:`
- `build_request:`
- `request_directory:`
- `find_request_path:`
- `write_request:`
- `load_request:`
- `load_responses:`

## `intake.py`

Conversation provenance checks for design briefs.

- `Requirement:`
- `EvidenceRef` — Provenance binding to an intake evidence file (image, document, CAD file).
- `Assumption:`
- `OpenQuestion:`
- `Intake:`
- `IntakeReport:`
- `load_intake:`
- `check_intake:`
- `check_evidence` — Verify every declared evidence file exists and matches its sha256.

## `kicad_cli.py`

Fail-closed wrappers around kicad-cli.

- `KicadCliError` — Raised when kicad-cli cannot produce a valid result.
- `CompletedRun:`
- `Violation:`
- `Report:`
- `command_prefix:`
- `run:`
- `version:`
- `erc:`
- `drc:`
- `export_netlist:`
- `DiffReport:`
- `JobsetResult:`
- `export:`
- `ImportResult:`
- `import_file:`
- `export_stackup:`
- `render:`
- `render_schematic:`
- `render_layers:`
- `diff:`
- `jobset_run:`
- `reports_equivalent:`
- `version_about:`

## `klc.py`

Subprocess integration for the pinned KiCad Library Convention checker.

- `KlcViolation:`
- `KlcReport:`
- `run_klc` — Run the pinned upstream checker and parse only its JUnit XML report.

## `landpattern.py`

IPC-7351B land-pattern calculations for supported surface-mount packages.

- `LandPatternError` — Raised when a package cannot produce a safe supported land pattern.
- `Rect:`
- `LandPatternResult:`
- `bga_pin_positions:`
- `standard_pin_placements` — Return top-left-anchored counter-clockwise pin placements.
- `compute_land_pattern` — Compute supported IPC-7351B pads in KiCad top-view coordinates.
- `lead_rects` — Return maximum-material package lead rectangles in top-view coordinates.

## `liaison.py`

Sister Liaison Protocol (SLP) v2 — circuit's local strict mirror.

- `InputRef:`
- `ArtifactOut:`
- `GateVerdict:`
- `UxRequestV2:`
- `UxResponseV2:`
- `MalformedEntry:`
- `InboxEntry:`
- `UxInboxResult:`
- `ux_inbox` — Scan ``<root>/liaison/`` for requests targeting circuit.
- `UxRespondResult:`
- `ux_respond` — Validate and write ``liaison/<id>.ux-response.json`` for a request.

## `libitems.py`

Strict readers for KiCad footprint and symbol library items.

- `LibItemError` — Raised when a KiCad library item is malformed or unsupported.
- `PadDef:`
- `GraphicDef:`
- `ModelRef:`
- `FootprintDef:`
- `SymPin:`
- `SymbolDef:`
- `parse_footprint` — Parse a KiCad 6-10 footprint file.
- `parse_symbol` — Parse a symbol from a library, resolving any ``extends`` parent.

## `libmetrics.py`

Hash-bound library review escape-rate metrics.

- `LibraryMetricsError` — Raised when stored metrics cannot authorize a relaxed review scope.
- `CorrectionRecord:`
- `ReviewerCatchTrialMetrics:`
- `LibraryMetrics:`
- `clopper_pearson_upper95` — Return the exact one-sided 95% Clopper-Pearson upper confidence bound.
- `compute_metrics` — Compute metrics for a project root containing the PR-B review journal.
- `require_relaxation_supported` — Validate that a stored metrics snapshot still binds current source reports.

## `libraries.py`

Deterministic KiCad symbol, footprint, and pin resolution.

- `LibraryRoots:`
- `default_roots:`
- `SymbolInfo:`
- `LibraryAuthoringRequest:`
- `LibraryReport:`
- `symbol_pins:`
- `check_libraries:`

## `libraryvision.py`

Build hash-bound visual comparisons for library footprints and symbols.

- `compare_library_item:`
- `compare_model:`

## `libreuse.py`

Deterministic search for reusable KiCad library items.

- `CandidateModel:`
- `FunctionalCheck:`
- `FootprintCandidate:`
- `SymbolCandidate:`
- `CandidateReport:`
- `find_candidates` — Find geometrically and electrically compatible KiCad library items.

## `libreview.py`

Hash-bound human review packets and approval decisions for library parts.

- `BlindQuestion:`
- `ReviewFinding:`
- `ReviewPacket:`
- `ReviewCorrection:`
- `ReviewTrialPlant:`
- `ReviewTrialState:`
- `ReviewRegressionCase:`
- `ReviewDecision:`
- `ReviewStatus:`
- `CorrectionResult:`
- `packet_id:`
- `current_packet_id:`
- `blind_questions:`
- `load_decisions` — Load current-packet pointers and revalidate the referenced user events.
- `review_status:`
- `review_trial_outcomes` — Return one latest caught/missed result per reviewer and private trial.
- `export_correction_regression_fixtures` — Export correction regressions as deterministic mutation fixture records.
- `apply_corrections:`
- `correction_regressions:`
- `build_review_packet` — Build fresh deterministic and human-review evidence for a library part.

## `libsource.py`

Library source attribution, safe imports, and deterministic provenance.

- `LibrarySourceError` — Raised when a library source cannot be safely validated or imported.
- `LicenseInfo:`
- `SourceInfoInput` — Caller-provided source data before the imported original is hashed.
- `SourceInfo:`
- `ProvenanceEntry:`
- `LibraryProvenance:`
- `ImportFinding:`
- `ImportReport:`
- `assert_safe_destination:`
- `load_library_provenance` — Load provenance, returning an empty manifest when none exists.
- `import_library_item` — Safely import a KiCad library item and record its source provenance.
- `record_library_item` — Register a generated or derived library artifact, or update its history.

## `libtestboard.py`

KiCad-backed test-board oracles for library items.

- `TestBoardCheck:`
- `TestBoardFinding:`
- `TestBoard:`
- `write_model_export_board:`
- `build_test_board` — Build a temporary project, run KiCad's oracles, and retain their outputs.

## `libverify.py`

Verification of authored KiCad symbols, footprints, models, and provenance.

- `VerifyFinding:`
- `VerifiedSymbol:`
- `VerifiedFootprint:`
- `VerifiedModelInspection:`
- `VerifiedModel:`
- `ModelInspectionReport:`
- `ModelCrossCheckGeometry:`
- `ModelCrossCheckFinding:`
- `ModelCrossCheckReport:`
- `VerificationInputs:`
- `LibraryVerification:`
- `nominal_body_box:`
- `functional_findings` — Return non-configurable pad, orientation, lead, and clearance findings.
- `cross_check_models:`
- `inspect_model_file` — Inspect one STEP file against its PartSpec and footprint geometry.
- `verify_library_part` — Verify a library part against its current PartSpec and generated land pattern.

## `libwriter.py`

Deterministic KiCad symbol and footprint writers.

- `LibWriterError` — Raised when deterministic library output cannot be produced safely.
- `WriteResult:`
- `write_footprint:`
- `write_symbol:`

## `lineage.py`

Hash-bound lineage and deterministic pad-change records.

- `PadChange:`
- `FootprintBase:`
- `FootprintLineage:`
- `lineage_path_for:`
- `pad_changes` — Return stable base-to-current pad changes grouped by pad number.
- `change_matches:`
- `compare_recorded_changes` — Return actual unrecorded changes and recorded changes no longer present.

## `mcp_args.py`

Package marker.


## `mcp_collect.py`

Package marker.


## `mcp_konnect.py`

Package marker.


## `mcp_server.py`

Low-level stdio MCP server for circuit operations.

- `tool_specs:`

## `model3d.py`

Deterministic nominal package model generation.

- `Model3dError` — Raised when a nominal model cannot be generated.
- `GeneratedModel:`
- `ExpectedTerminal:`
- `footprint_to_board_xy:`
- `expected_terminals:`
- `connector_body_bounds:`
- `generate_model:`

## `modeloracle.py`

KiCad STEP-export checks for footprint 3D models.

- `ModelExportFinding:`
- `ModelExportRun:`
- `ModelExportReport:`
- `verify_model_export` — Compare KiCad's board STEP export with the referenced model placement.

## `mutation.py`

Seeded library-artifact mutations and independent-oracle accounting.

- `MutationError` — Raised when a mutation cannot be applied or a finding is unmapped.
- `library_verifier` — Build a verifier backed by the production library and model oracles.
- `library_mutation_fixture` — Load a library-part fixture and bind it to the real verification stack.
- `Mutation:`
- `MutationFinding:`
- `MutationOutcome:`
- `MutationReport:`
- `MutationArtifacts:`
- `MutationFixture:`
- `MutationOperator:`
- `family_for_code:`
- `run_mutations` — Run seeded mutations against the fixture's complete verification stack.

## `netlist.py`

Minimal fail-closed parser for KiCad's s-expression netlist.

- `NetlistError` — Raised when a KiCad netlist is malformed or unsupported.
- `Component:`
- `Netlist:`
- `parse_netlist:`
- `ConnectivityReport:`
- `check_connectivity:`

## `occt.py`

Narrow Open CASCADE boundary for STEP model operations.

- `OcctError` — Raised when Open CASCADE cannot safely process a model.
- `Shape:`
- `Bounds:`
- `SolidFacts:`
- `ShapeFacts:`
- `SlabRegion:`
- `MarkerEvidence:`
- `read_step:`
- `write_step:`
- `inspect:`
- `slab_regions:`
- `pin1_marker:`
- `face_color_marker:`
- `transform:`
- `solids:`
- `fuse:`
- `box:`
- `cylinder:`
- `cylinder_cut:`
- `compound:`

## `packageid.py`

Package marker.

- `PackageIdentity:`
- `resolve_package_identity` — Resolve package facts from two fresh PDF text-extraction lanes.
- `sibling_package_mpn:`
- `check_package_identity:`

## `partspec.py`

Agent-authored part specifications and deterministic datasheet checks.

- `CellRef:`
- `Reading:`
- `Dimension:`
- `ExposedPad:`
- `TabSpec:`
- `BallGrid:`
- `ConnectorBoardEdge:`
- `ConnectorMatingEnvelope` — Use KiCad's 3D frame: z=0 is board top; positive z points away from the mounted copper side.
- `ConnectorContactRow:`
- `ConnectorMechanicalFeature:`
- `ConnectorNumbering:`
- `ConnectorVariantSelection:`
- `ConnectorKeepout:`
- `ConnectorSpec:`
- `PackageSpec` — Package dimensions use the KiCad top view: pin 1 at top-left, +x right, +y down.
- `expected_signal_pin_numbers:`
- `LandPad:`
- `LandPattern:`
- `PinSpec` — A pin's optional view identifies the datasheet drawing view for its reading.
- `OrderableVariant:`
- `PinoutDrawing:`
- `PinTable:`
- `DatasheetErratum:`
- `DatasheetRef:`
- `SubstitutionRef:`
- `PartSpec:`
- `ParsedDimension:`
- `SpecFinding:`
- `PartSpecReport:`
- `parse_dimension_text` — Parse common datasheet dimension notation into numeric bounds.
- `load_part_spec:`
- `part_spec_sha256:`
- `rederive_pages:`
- `check_part_spec` — Cross-check every authored reading against extraction and provenance.

## `paths.py`

Runtime paths shared by the circuit tools.


## `pinout.py`

Deterministic geometry and name checks for datasheet pinout drawings.

- `PinoutLabel:`
- `PinoutGeometry:`
- `PinoutIssue:`
- `normalized:`
- `to_top_view:`
- `winding:`
- `derive_pinout:`
- `compare_orientation:`
- `names_equal:`
- `diagnose_permutation:`

## `pinsource.py`

Independent, deterministic pin-source parsing and comparison.

- `PinSourceError` — Raised when a machine-readable pin source cannot be parsed.
- `PinSourcePin:`
- `PinSource:`
- `PinSourceInput:`
- `PinSourceFinding:`
- `PinSourceComparison:`
- `source_from_part_spec:`
- `parse_ibis:`
- `parse_bsdl:`
- `parse_pin_source:`
- `parse_stm32_open_pin_data:`
- `parse_amd_package_file:`
- `parse_microchip_atdf:`
- `compare_pin_sources:`

## `raster.py`

Rasterize PDF/SVG intake files to PNG for the vision lane.

- `RasterizeError` — Raised when a file cannot be rasterized.
- `glyph_signature` — Return a fixed-size, ink-normalized glyph signature.
- `normalized_cross_correlation` — Compute zero-mean normalized cross-correlation without array dependencies.
- `rasterize` — Rasterize `source` (.pdf or .svg) to PNG pages in `out_dir`.

## `records.py`

Typed writers for the VibeBB Record Protocol (VRP) v1.

- `sentence_count:`
- `impression_is_prose` — Reject a terse status line where a long-form impression is required.
- `sha256_file:`
- `tree_sha256` — sha256 of a file, or of a directory's sorted (relative path, sha256) list.
- `ArtifactRef:`
- `EvidenceRef:`
- `DecisionOption:`
- `EvidenceInput:`
- `DecisionInput` — What the agent supplies; evidence paths are hashed by the writer.
- `StageImpressionInput:`
- `VisionFinding:`
- `VisionReviewInput:`
- `DecisionRecord:`
- `StageImpression:`
- `VisionReview:`
- `records_dir:`
- `record_decision:`
- `record_impression:`
- `record_vision_review:`
- `records_summary` — Counts per log plus the last Stop-hook verdict; informational only.

## `report.py`

Design report construction from deterministic gate results.

- `DesignReport:`
- `build_design_report:`
- `write_report:`

## `revwatch.py`

Datasheet revision polling and errata evidence binding.

- `DatasheetRevisionSnapshot:`
- `RevisionInvalidationRecord:`
- `DatasheetRevisionCheck:`
- `fetch_current_revision` — Fetch and hash the manufacturer's current PDF over HTTPS.
- `approval_blocker:`
- `check_revision` — Compare a PartSpec datasheet binding with the manufacturer's current source.

## `ruleprofile.py`

Layered, hash-bound manufacturing rule profiles.

- `RuleProfileError:`
- `EvidenceRef:`
- `GoalOverride:`
- `PasteRule:`
- `RuleProfile:`
- `RuleProfileDigest:`
- `EffectiveRules:`
- `profile_sha256:`
- `load_rules` — Resolve a profile to its built-in root and verify each profile and evidence file.

## `sch_lint.py`

Deterministic readability lint for .kicad_sch files.

- `SchLintError` — Raised when a schematic cannot be linted.
- `SchLintFinding:`
- `SchLintReport:`
- `lint_schematic:`
- `lint_file:`
- `main:`

## `sexpr.py`

Minimal generic s-expression tokenizer and parser.

- `QuotedString` — A string atom whose serialized form must be quoted.
- `SExprError` — Raised when an s-expression is malformed.
- `quoted` — Mark a string value that must remain quoted when serialized.
- `parse_text` — Parse one root s-expression and return it as a list.
- `serialize` — Serialize an s-expression, quoting atoms that require it.

## `stackup.py`

Deterministic stackup diagram: stackup JSON -> drawing-style SVG section.

- `stackup_svg` — Render a `pcb export stackup --format json` payload as a section SVG.
- `write_stackup_diagram` — Write `stackup_svg(data)` to `out`, returning the path.

## `titleblock.py`

Inject or update the ``(title_block ...)`` element of a ``.kicad_sch`` file.

- `TitleBlockError` — Raised when the schematic text cannot be edited safely.
- `list_end` — Return the index just past the ``)`` closing the ``(`` at ``start``.
- `paper_for_part_count` — Return the smallest landscape sheet fitting the diagonal placement
- `atomic_write` — Validate then atomically replace the schematic file.
- `set_paper_size` — Set the schematic's ``(paper ...)`` element to ``size`` (e.g. "A3").
- `inject_title_block` — Write ``title``/``date``/``rev`` (and ``company`` when given) into the

## `visionread.py`

Create hash-bound datasheet crops for vision-capable authoring agents.

- `prompt_for_kind:`
- `VisionReadError` — Raised when a vision read cannot be created or loaded safely.
- `VisionReadRequest:`
- `VisionToken:`
- `VisionGlyphFinding:`
- `VisionReadItem:`
- `VisionBatch:`
- `VisionAnswerRecord:`
- `VisionAnswerInput:`
- `LoadedVisionRead:`
- `VisionComparisonEvidence:`
- `render_datasheet_crop:`
- `create_comparison_batch:`
- `create_read_batch:`
- `record_answers:`
- `load_vision_read:`
- `find_comparison_evidence:`

## `workspace.py`

Workspace path validation.

- `workspace_root:`
- `workspace_path:`
- `reject_symlinks:`
