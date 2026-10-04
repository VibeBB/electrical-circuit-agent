---
name: circuit-library-authoring
description: Author and approve project-owned circuit-library parts from independently checked evidence.
version: 0.1.0
license: BSD-3-Clause
triggers:
  - circuit library authoring
  - PartSpec
  - KiCad footprint
  - KiCad symbol
  - circuit library part
---

# Circuit library authoring

Use this ordered workflow for every project-owned library part. Keep all source
paths, hashes, provenance, licenses, generated artifacts, and gate reports
under the project. Never edit files under `libraries/`, the installed KiCad
libraries, or the CERN library tree. Never read `library/corpus` or
`.vision-control`; corpus truth and vision-control identities are not
authoring evidence.

1. **Search manufacturer sources and reusable library items.** Search the
   manufacturer's official pin database first—such as ST STM32 Open Pin Data,
   AMD/Xilinx package pinouts, or Microchip ATDF—then official IBIS/BSDL and
   CAD/library sources. Run
   `circuit_library_candidates` for installed and project candidates. Prefer a
   valid, matching product-tuned candidate when its lineage and evidence pass.
   For external CAD, verify the source URL, retrieval date, license,
   attribution, and redistribution terms before use; record a usable source
   with `circuit_library_import` or a generated/derived artifact with
   `circuit_library_record`. Do not auto-download from aggregators or import
   an item with unknown provenance or license. **Stop** if no source can be
   used legally or the candidate's identity/package cannot be established.

2. **Acquire and bind the target datasheet.** Confirm manufacturer, exact MPN,
   requested revision, and required package, pinout, pin-table, land-pattern,
   and orderable-table sections. Extract a candidate PDF with
   `circuit_datasheet_extract`. If the PDF is unavailable, NDA-blocked, or
   mismatched, create a `datasheet_acquisition` HumanRequest with
     `circuit_human_request_create` and stop. Read only its trusted response
     with `circuit_human_request_status`; a provided PDF must be attached to
   that same user response and marked confidential when required. Call the
   `circuit_datasheet_check_received` MCP tool with `pdf_path` set to the
   received PDF and `request_path` set to its stored acquisition request.
   Findings are normal tool results; fail closed on any finding or `isError`
   response, and do not use a PDF whose target MPN, revision, or required
   sections do not validate. Never send confidential paths or content to web
   tools.

3. **Derive mechanical and visual evidence.** Run
   `circuit_datasheet_extract` for the source PDF and confirm the independent
   Poppler and pdfplumber mechanical lanes agree on the required evidence. Use
   `circuit_vision_read` for every required drawing/table image and answer
   every item with `circuit_vision_answer`, including the control image.
   Every answer must include a substantive impression describing legibility,
   ambiguity, and anything surprising; missing impressions are incomplete
   evidence. **Stop** if extraction, lane agreement, rendering, or a required
   vision read fails.

4. **Blind-author two PartSpecs and compare only after sealing.** Use the SDK
   task tool set to delegate the same source evidence independently to
   `circuit-part-author-a` and `circuit-part-author-b`. Lane A must use its
   pdftoppm-backed vision reads and lane B its pdfium-backed reads. Each lane
   must answer its vision items with impressions and seal with
   `circuit_part_author_commit`. Once both have committed, run
   `circuit_part_author_compare`, then `circuit_part_spec_check` on the
   selected PartSpec, writing its report as sibling `part.spec.check.json`.
   Do not reveal one lane's work to the other before both
   commits. **Stop** and create a `library_review` HumanRequest if a
   content disagreement cannot be resolved from the datasheet; stop for a
   new acquisition request if the source or PartSpec does not match the
   requested MPN, revision, or package.

5. **Choose reusable candidates before generating.** Run
   `circuit_library_candidates` for the checked PartSpec. Review candidate
   identity, provenance, license, and lineage; prefer valid product-layer
   tuned candidates over generic candidates where they match the PartSpec.
   **Stop** rather than copying an item with invalid lineage, incompatible
   package identity, or unclear redistribution rights.

6. **Derive the land pattern.** Run `circuit_land_pattern` with the selected
   density and, when appropriate, the project rule profile. Preserve the
   PartSpec, source citations, and generated land-pattern report together.
   **Stop** on contradictory or unsupported dimensions; do not tune away a
   deterministic finding.

7. **Create or import the library artifacts.** Generate a project-owned
   footprint with `circuit_footprint_write` and a symbol with
   `circuit_symbol_write`, using the accepted, checked PartSpec and the
   selected footprint identifier. The tools record generated provenance,
   including input and output hashes; pass the same density and rule profile
   used in step 6 through `rules_path`. Hand-written KiCad S-expressions are
   prohibited; manufacturer CAD may only enter through
   `circuit_library_import`. Record any additional derived transformation with
   `circuit_library_record`.
   Generate the model with `circuit_model_generate` and inspect it with
   `circuit_model_inspect`. A reported `model_terminals_unseparable` requires
   a `library_review` HumanRequest describing the unseparable terminals and
   asking for authoritative next-step guidance; stop. **Stop** on any
   symbol/footprint/model mapping that cannot be grounded in source evidence.

8. **Run deterministic library checks and export oracle.** Call
   `circuit_library_verify` with the PartSpec, symbol library/name,
   footprint, model required, and the project library directory. Pass
   manufacturer pin databases through `pin_sources`, selecting the exact ATDF
   pinout by name. Match device/package identities after punctuation
   normalization; exact matches or prefix/base-name matches with a shorter
   identity of at least six alphanumeric characters are accepted. Mark derived
   sources with their upstream lineage; do not count derivatives as
   independent.
   Keep the test-board and export checks enabled. **Stop** on every error,
   missing oracle, or unresolved deterministic contradiction; a HumanRequest,
   substitute permit, vision impression, author agreement, or review approval
   cannot waive it.

9. **Compare library artwork with datasheet images.** Run
   `circuit_vision_compare` separately for the footprint and symbol, and
   `circuit_model_compare` for the STEP model. Answer every returned image
   through `circuit_vision_answer` with an impression. Treat these
   comparisons as advisory human context, not as a deterministic pass.
   **Stop** for human clarification when a comparison exposes an unresolved
   evidence disagreement; rerun the deterministic checks after any artifact
   change.

10. **Build the blind review packet.** After verification passes, create a
    fresh packet with `circuit_library_review_packet`. Read its review
    instructions and provide the agent assessment and recommendation with
    the cited artifacts and residual unknowns. Do not claim a review is
    complete from packet creation alone. **Stop** if packet creation fails,
    its hashes do not match the verified artifacts, or the packet omits
    required instructions or evidence.

11. **Wait for current hash-bound approval.** Ask the human to answer the
    packet's blind questions and submit the documented
    `CIRCUIT-LIBRARY-REVIEW <packet_id>` response. Run
    `circuit_library_review_status` against the same PartSpec, symbol,
    footprint, model, and settings. Report completion only when it returns
    approved for the current hashes. If artifacts change, rebuild the packet
    and obtain a new approval. A rejection may be applied only through
    `circuit_library_review_apply`; re-run verification and request review
    again. **Stop** on rejection, stale approval, missing response, or any
    non-approved status.

## HumanRequest stop conditions

Create a complete HumanRequest and stop for any unobtainable or mismatched
datasheet, substitute permission, alternative evidence, unseparable model
terminals, or unresolved lane disagreement. Choose the matching request kind:
`datasheet_acquisition`, `substitute_permission`, `alternative_evidence`, or
`library_review`. For acquisition, include attempted sources, requested
revision, required sections, and any current source hash. For substitute
permission, bind the exact target and substitute MPNs, datasheet hashes,
requested scope, unverifiable fields, and similarity evidence. For alternative
evidence, bind each file hash to JSON-pointer coverage, unknown fields, and
the measurement method when relevant. For every kind, include known facts,
unknown fields, a substantive multi-sentence agent assessment, a
recommendation with rationale, and at least two alternatives with their
risks. Check only trusted user responses with `circuit_human_request_status`.
No response, denial, stale binding, or request/response mismatch authorizes
the next step.
