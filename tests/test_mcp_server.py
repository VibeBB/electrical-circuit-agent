import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent, TextContent
from PIL import Image

from circuit import mcp_server
from circuit.advisory import AdvisoryResult
from circuit.datasheet import DatasheetExtraction, PageExtraction
from circuit.kicad_cli import DiffReport, JobsetResult
from circuit.landpattern import Density
from circuit.libsource import ImportReport, SourceInfoInput
from circuit.libverify import LibraryVerification, VerifiedFootprint, VerifiedSymbol
from circuit.netlist import ConnectivityReport
from circuit.partspec import (
    CellRef,
    DatasheetRef,
    Dimension,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PinSpec,
    PinTable,
    Reading,
)
from circuit.report import DesignReport
from circuit.sch_lint import SchLintReport
from pinout_fixtures import pinout_drawing


def test_mcp_server_lists_expected_tools() -> None:
    names = {name for name, _, _ in mcp_server._TOOLS}  # pyright: ignore[reportPrivateUsage]
    assert names == {
        "circuit_api_server_start",
        "circuit_api_server_status",
        "circuit_api_server_stop",
        "circuit_brief_validate",
        "circuit_brief_intake_check",
        "circuit_brief_library_check",
        "circuit_netlist_export",
        "circuit_connectivity_check",
        "circuit_connectivity_export",
        "circuit_firmware_export",
        "circuit_firmware_check",
        "circuit_doctor",
        "circuit_design_report",
        "circuit_erc",
        "circuit_drc",
        "circuit_render",
        "circuit_diff",
        "circuit_jobset_run",
        "circuit_export",
        "circuit_import",
        "circuit_stackup",
        "circuit_rasterize",
        "circuit_datasheet_extract",
        "circuit_vision_read",
        "circuit_vision_compare",
        "circuit_model_generate",
        "circuit_model_inspect",
        "circuit_model_compare",
        "circuit_vision_answer",
        "circuit_part_author_commit",
        "circuit_part_author_compare",
        "circuit_part_spec_check",
        "circuit_land_pattern",
        "circuit_library_candidates",
        "circuit_library_import",
        "circuit_library_record",
        "circuit_library_verify",
        "circuit_library_review_packet",
        "circuit_library_review_status",
        "circuit_library_review_apply",
        "circuit_corpus_score",
        "circuit_konnect_call",
        "circuit_kicad_version",
        "circuit_sch_lint",
        "circuit_fit_sheet",
    }
    verification_schema = next(
        schema
        for name, _, schema in mcp_server._TOOLS  # pyright: ignore[reportPrivateUsage]
        if name == "circuit_library_verify"
    )
    assert "part_spec_check_path" not in verification_schema["properties"]
    assert verification_schema["properties"]["test_board"]["default"] is True
    assert verification_schema["properties"]["rule_profile"]["type"] == "string"
    land_pattern_schema = next(
        schema
        for name, _, schema in mcp_server._TOOLS  # pyright: ignore[reportPrivateUsage]
        if name == "circuit_land_pattern"
    )
    assert land_pattern_schema["properties"]["rule_profile"]["type"] == "string"
    assert "library_dir" in land_pattern_schema["properties"]
    candidate_schema = next(
        schema
        for name, _, schema in mcp_server._TOOLS  # pyright: ignore[reportPrivateUsage]
        if name == "circuit_library_candidates"
    )
    assert candidate_schema["properties"]["product"]["type"] == "string"
    assert {
        name
        for name, _, _ in mcp_server._TOOLS  # pyright: ignore[reportPrivateUsage]
        if name.startswith("circuit_library_review_")
    } == {
        "circuit_library_review_packet",
        "circuit_library_review_status",
        "circuit_library_review_apply",
    }
    vision_answer_schema = next(
        schema
        for name, _, schema in mcp_server._TOOLS  # pyright: ignore[reportPrivateUsage]
        if name == "circuit_vision_answer"
    )
    assert vision_answer_schema["properties"]["answers"]["additionalProperties"]["required"] == [
        "answer",
        "impression",
    ]
    compare_schema = next(
        schema
        for name, _, schema in mcp_server._TOOLS  # pyright: ignore[reportPrivateUsage]
        if name == "circuit_vision_compare"
    )
    assert compare_schema["properties"]["kind"]["enum"] == [
        "compare_footprint",
        "compare_symbol",
    ]
    assert {
        "part_spec_path",
        "symbol_lib_path",
        "symbol_name",
        "footprint_path",
    } <= set(compare_schema["required"])
    model_generate_schema = next(
        schema
        for name, _, schema in mcp_server._TOOLS  # pyright: ignore[reportPrivateUsage]
        if name == "circuit_model_generate"
    )
    assert set(model_generate_schema["required"]) == {"part_spec_path", "footprint_path"}
    model_inspect_schema = next(
        schema
        for name, _, schema in mcp_server._TOOLS  # pyright: ignore[reportPrivateUsage]
        if name == "circuit_model_inspect"
    )
    assert set(model_inspect_schema["required"]) == {
        "part_spec_path",
        "footprint_path",
        "model_path",
    }
    model_compare_schema = next(
        schema
        for name, _, schema in mcp_server._TOOLS  # pyright: ignore[reportPrivateUsage]
        if name == "circuit_model_compare"
    )
    assert set(model_compare_schema["required"]) == {
        "part_spec_path",
        "footprint_path",
        "model_path",
    }
    corpus_score_schema = next(
        schema
        for name, _, schema in mcp_server._TOOLS  # pyright: ignore[reportPrivateUsage]
        if name == "circuit_corpus_score"
    )
    assert set(corpus_score_schema["required"]) == {
        "entry_id",
        "part_spec_path",
        "symbol_lib_path",
        "symbol_name",
        "footprint_path",
        "model_path",
    }


def test_corpus_score_mcp_dispatch_and_lane_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CIRCUIT_AUTHORING_LANE", raising=False)
    captured: dict[str, object] = {}
    workspace = Path(__file__).resolve().parents[1]

    def score_entry(*args: object, **kwargs: object) -> dict[str, str]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"verdict": "not_available"}

    monkeypatch.setattr(mcp_server.corpus, "score_entry", cast(Any, score_entry))
    arguments = {
        "entry_id": "lm358-soic8",
        "part_spec_path": str(workspace / "tests" / "part.json"),
        "symbol_lib_path": str(workspace / "tests" / "symbols.kicad_sym"),
        "symbol_name": "LM358",
        "footprint_path": str(workspace / "tests" / "lm358.kicad_mod"),
        "model_path": str(workspace / "tests" / "lm358.step"),
    }
    result = asyncio.run(mcp_server.call_tool("circuit_corpus_score", arguments))
    assert result.isError is False
    assert captured["args"] == (
        workspace / "library" / "corpus",
        "lm358-soic8",
        workspace / "tests" / "part.json",
        workspace / "tests" / "lm358.kicad_mod",
        workspace / "tests" / "symbols.kicad_sym",
        "LM358",
        workspace / "tests" / "lm358.step",
    )
    assert captured["kwargs"] == {}

    monkeypatch.setenv("CIRCUIT_AUTHORING_LANE", "a")
    refused = asyncio.run(mcp_server.call_tool("circuit_corpus_score", arguments))
    assert refused.isError is True
    error = next(block.text for block in refused.content if isinstance(block, TextContent))
    assert "author lanes cannot run the golden corpus scorer" in error


def test_vision_compare_dispatch_returns_both_image_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "vision-reads" / "comparison"
    image_dir = out_dir / "images"
    image_dir.mkdir(parents=True)
    image_paths = [image_dir / "read.png", image_dir / "control.png"]
    for path in image_paths:
        Image.new("RGB", (4, 4), "white").save(path)
    batch = mcp_server.visionread.VisionBatch(
        artifact_kind="circuit_vision_read_batch",
        batch_id="comparison",
        created_at="2026-01-01T00:00:00Z",
        lane="main",
        profile="test",
        model="test",
        pdf_path="part.pdf",
        pdf_sha256="a" * 64,
        items=[
            mcp_server.visionread.VisionReadItem(
                read_id="read",
                field="library.footprint",
                kind="compare_footprint",
                page=1,
                bbox=(1, 1, 2, 2),
                crop_bbox=(0, 0, 3, 3),
                dpi=300,
                rasterizer="pdftoppm",
                image_path="images/read.png",
                image_sha256=hashlib.sha256(image_paths[0].read_bytes()).hexdigest(),
                prompt="compare",
                prompt_sha256=hashlib.sha256(b"compare").hexdigest(),
            ),
            mcp_server.visionread.VisionReadItem(
                read_id="control",
                field="control",
                kind="compare_footprint",
                page=1,
                bbox=(1, 1, 2, 2),
                crop_bbox=(0, 0, 3, 3),
                dpi=300,
                rasterizer="pdftoppm",
                image_path="images/control.png",
                image_sha256=hashlib.sha256(image_paths[1].read_bytes()).hexdigest(),
                prompt="compare",
                prompt_sha256=hashlib.sha256(b"compare").hexdigest(),
                control=True,
            ),
        ],
        control_salt="salt",
        control_answer_sha256="b" * 64,
        control_read_sha256="c" * 64,
    )
    captured: dict[str, object] = {}

    def compare(*args: object, **kwargs: object) -> mcp_server.visionread.VisionBatch:
        captured["args"] = args
        captured.update(kwargs)
        return batch

    monkeypatch.setattr(mcp_server.libraryvision, "compare_library_item", compare)
    result, returned_paths = mcp_server._authoring_tool(  # pyright: ignore[reportPrivateUsage]
        "circuit_vision_compare",
        {
            "part_spec_path": str(tmp_path / "part.json"),
            "kind": "compare_footprint",
            "symbol_lib_path": str(tmp_path / "symbols.kicad_sym"),
            "symbol_name": "TEST",
            "footprint_path": str(tmp_path / "test.kicad_mod"),
            "out_dir": str(out_dir),
        },
    ) or (None, [])

    assert captured["kind"] == "compare_footprint"
    assert result is not None
    assert result["batch_id"] == "comparison"
    assert returned_paths == image_paths


def test_model_compare_dispatch_uses_model_vision_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "vision-reads" / "model"
    out_dir.mkdir(parents=True)
    batch = cast(
        mcp_server.visionread.VisionBatch,
        SimpleNamespace(
            batch_id="model-comparison",
            items=[
                SimpleNamespace(
                    read_id="model-read",
                    kind="compare_model",
                    prompt="compare model",
                    bindings={"model_sha256": "a" * 64},
                    image_path="images/model.png",
                )
            ],
        ),
    )
    captured: dict[str, object] = {}

    def compare(*args: object, **kwargs: object) -> mcp_server.visionread.VisionBatch:
        captured["args"] = args
        captured.update(kwargs)
        return batch

    monkeypatch.setattr(mcp_server.libraryvision, "compare_model", compare)
    result, returned_paths = mcp_server._authoring_tool(  # pyright: ignore[reportPrivateUsage]
        "circuit_model_compare",
        {
            "part_spec_path": str(tmp_path / "part.json"),
            "footprint_path": str(tmp_path / "test.kicad_mod"),
            "model_path": str(tmp_path / "test.step"),
            "out_dir": str(out_dir),
        },
    ) or (None, [])

    assert captured["args"] == (
        tmp_path / "part.json",
        tmp_path / "test.kicad_mod",
        tmp_path / "test.step",
    )
    assert captured["lane"] == "main"
    assert result is not None
    assert result["batch_id"] == "model-comparison"
    assert returned_paths == [out_dir / "images" / "model.png"]


def test_model_generate_and_inspect_tools_write_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    spec_path = tmp_path / "part.json"
    spec_path.write_text(_library_tool_spec().model_dump_json(), encoding="utf-8")
    footprint_path = tmp_path / "test.kicad_mod"
    footprint_path.write_text("(footprint TEST)\n", encoding="utf-8")
    model_path = tmp_path / "test.step"
    model_path.write_text("STEP fixture", encoding="utf-8")
    generated: dict[str, object] = {}

    def generate(
        _spec: PartSpec,
        selected_footprint: Path,
        output_dir: Path,
    ) -> mcp_server.model3d.GeneratedModel:
        generated["footprint"] = selected_footprint
        generated["output_dir"] = output_dir
        return mcp_server.model3d.GeneratedModel(
            step_path=output_dir / "test.step",
            manifest_path=output_dir / "test.step.gen.json",
            step_sha256="a" * 64,
            spec_sha256="b" * 64,
            footprint_sha256="c" * 64,
            generator_version="1",
            marker="top_left",
            marker_note=None,
        )

    def inspect(
        _spec: PartSpec,
        selected_footprint: Path,
        selected_model: Path,
        *,
        tolerance_mm: float,
    ) -> mcp_server.libverify.ModelInspectionReport:
        assert selected_footprint == footprint_path
        assert selected_model == model_path
        assert tolerance_mm == pytest.approx(0.05)
        return mcp_server.libverify.ModelInspectionReport(
            verdict="pass",
            path=selected_model,
            sha256="d" * 64,
            facts=None,
            findings=[],
        )

    monkeypatch.setattr(mcp_server.model3d, "generate_model", generate)
    monkeypatch.setattr(mcp_server.libverify, "inspect_model_file", inspect)

    async def exercise() -> None:
        generated_result = await mcp_server.call_tool(
            "circuit_model_generate",
            {
                "part_spec_path": str(spec_path),
                "footprint_path": str(footprint_path),
                "output_dir": str(tmp_path / "generated"),
            },
        )
        assert generated_result.isError is False
        generated_report = tmp_path / "circuit-reports" / "part.model-generation.json"
        assert generated_report.is_file()
        assert generated["footprint"] == footprint_path
        assert generated["output_dir"] == tmp_path / "generated"

        inspected_result = await mcp_server.call_tool(
            "circuit_model_inspect",
            {
                "part_spec_path": str(spec_path),
                "footprint_path": str(footprint_path),
                "model_path": str(model_path),
                "tolerance_mm": 0.05,
            },
        )
        assert inspected_result.isError is False
        assert (tmp_path / "circuit-reports" / "test.inspection.json").is_file()

    asyncio.run(exercise())


def test_vision_read_mcp_result_contains_only_paths_prompts_and_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("CIRCUIT_AUTHORING_LANE", "b")
    pdf_path = tmp_path / "part.pdf"
    pdf_path.write_bytes(b"synthetic pdf")
    page_png = tmp_path / "page.png"
    Image.new("RGB", (100, 100), "white").save(page_png)
    extraction_path = tmp_path / "extraction.json"
    extraction = DatasheetExtraction(
        artifact_kind="circuit_datasheet_extraction",
        pdf_path=pdf_path.name,
        pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        page_count=1,
        pages=[
            PageExtraction(
                page=1,
                width_pt=100,
                height_pt=100,
                png_path=page_png.name,
                png_sha256=hashlib.sha256(page_png.read_bytes()).hexdigest(),
                dpi=72,
                text_layer=True,
                lanes=[],
                tables_path=None,
                table_count=0,
                vector_objects=0,
                drawing_page=False,
                order_similarity=1,
            )
        ],
        tools={},
    )
    extraction_path.write_text(extraction.model_dump_json(), encoding="utf-8")

    def render(
        _pdf: Path,
        output: Path,
        _page: int,
        bbox: tuple[float, float, float, float],
        *_args: object,
    ) -> None:
        Image.new(
            "RGB",
            (max(1, round(bbox[2] - bbox[0])), max(1, round(bbox[3] - bbox[1]))),
            "white",
        ).save(output)

    monkeypatch.setattr(mcp_server.visionread, "_render_pdfium", render)

    def control_image(path: Path, size: tuple[int, int]) -> str:
        Image.new("RGB", size, "white").save(path)
        return "ABC234"

    monkeypatch.setattr(
        mcp_server.visionread,
        "_control_image",
        control_image,
    )
    requests = [
        {
            "field": f"table.{index}",
            "page": 1,
            "bbox": [float(index + 1), 1.0, float(index + 2), 2.0],
            "kind": "table",
        }
        for index in range(7)
    ]

    result = asyncio.run(
        mcp_server.call_tool(
            "circuit_vision_read",
            {
                "extraction_path": str(extraction_path),
                "out_dir": str(tmp_path / "vision"),
                "requests": requests,
            },
        )
    )

    assert result.isError is False
    text = next(block.text for block in result.content if isinstance(block, TextContent))
    payload = json.loads(text)
    assert len(payload["items"]) == 8
    assert len([block for block in result.content if isinstance(block, ImageContent)]) == 8
    assert all("answer" not in item for item in payload["items"])
    assert all("image_path" in item and "prompt" in item for item in payload["items"])


def test_output_path_defaults_to_report_directory(tmp_path: Path) -> None:
    path = mcp_server._output_path(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "board.kicad_pcb", None, "drc"
    )
    assert path == tmp_path / "circuit-reports" / "board.drc.json"


def _library_tool_spec() -> PartSpec:
    reading = Reading(page=1, bbox=(0, 0, 1, 1), vision="1", vision_record="vision.json")

    def dimension(value: float) -> Dimension:
        return Dimension(nom=value, reading=reading)

    package = PackageSpec(
        family="gullwing_dual",
        drawing_id="TEST",
        pin_count=2,
        pitch=dimension(0.65),
        body_length=dimension(2.0),
        body_width=dimension(1.5),
        height=dimension(0.5),
        lead_span=dimension(3.0),
        lead_length=dimension(0.5),
        lead_width=dimension(0.3),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=Reading(
            page=1,
            bbox=(0, 0, 1, 1),
            vision="top-left",
            vision_record="vision.json",
        ),
    )
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="TEST-1",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="part.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="extraction.json",
        ),
        package=package,
        pinout=pinout_drawing({"1": "PIN1", "2": "PIN2"}),
        pins=[
            PinSpec(
                number=str(number),
                name=f"PIN{number}",
                electrical_type="passive",
                reading=Reading(
                    page=1,
                    vision=f"{number} PIN{number}",
                    vision_record="vision.json",
                ),
            )
            for number in range(1, 3)
        ],
        orderable=[
            OrderableVariant(
                mpn="TEST-1",
                package_designator="TEST",
                pin_count=2,
                row=CellRef(table=0, row=1, col=0),
                reading=Reading(
                    page=1,
                    vision="TEST-1 TEST",
                    vision_record="vision.json",
                ),
            )
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
    )


def test_library_mcp_tools_create_reports(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    spec_path = tmp_path / "part.json"
    spec_path.write_text(_library_tool_spec().model_dump_json(), encoding="utf-8")
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    artifact_path = library_dir / "generated.txt"
    artifact_path.write_text("generated artifact", encoding="utf-8")
    source_path = tmp_path / "source.kicad_mod"
    source_path.write_text("fixture", encoding="utf-8")
    symbol_path = tmp_path / "symbol.kicad_sym"
    symbol_path.write_text("{}", encoding="utf-8")
    footprint_path = tmp_path / "footprint.kicad_mod"
    footprint_path.write_text("{}", encoding="utf-8")
    captured_sources: list[SourceInfoInput] = []
    verification_options: list[bool] = []
    verification_rule_chains: list[list[str] | None] = []
    candidate_products: list[str | None] = []

    def fake_import(
        source_file: Path,
        destination: Path,
        nickname: str,
        *,
        source: SourceInfoInput,
        **_kwargs: Any,
    ) -> ImportReport:
        captured_sources.append(source)
        return ImportReport(
            library_dir=destination,
            nickname=nickname,
            source_original_path="sources/abc/source.kicad_mod",
            imported=[],
            findings=[],
        )

    monkeypatch.setattr(mcp_server.libsource, "import_library_item", fake_import)

    def fake_candidates(_spec: PartSpec, **kwargs: Any) -> mcp_server.libreuse.CandidateReport:
        candidate_products.append(cast(str | None, kwargs.get("product")))
        return mcp_server.libreuse.CandidateReport(
            artifact_kind="circuit_library_candidates",
            part_spec_sha256="a" * 64,
            reference_source="ipc7351b",
            footprints=[],
            symbols=[],
        )

    monkeypatch.setattr(mcp_server.libreuse, "find_candidates", fake_candidates)

    def fake_verify(_spec: PartSpec, **kwargs: Any) -> LibraryVerification:
        verification_options.append(cast(bool, kwargs["test_board"]))
        rules = kwargs["rules"]
        verification_rule_chains.append(
            rules.chain if isinstance(rules, mcp_server.ruleprofile.EffectiveRules) else None
        )
        report = LibraryVerification(
            artifact_kind="circuit_library_verification",
            verdict="pass",
            part_spec_sha256="b" * 64,
            inputs=mcp_server.libverify.VerificationInputs(
                part_spec_path=Path("part.json"),
                symbol_lib=Path("symbol.kicad_sym"),
                symbol_name=cast(str, kwargs["symbol_name"]),
                footprint_path=Path("footprint.kicad_mod"),
                density="nominal",
                tolerance_mm=0.02,
                model_required=True,
            ),
            symbol=VerifiedSymbol(
                lib_path=cast(Path, kwargs["symbol_lib"]),
                name=cast(str, kwargs["symbol_name"]),
                sha256=None,
            ),
            footprint=VerifiedFootprint(
                path=cast(Path, kwargs["footprint_path"]),
                name=cast(Path, kwargs["footprint_path"]).stem,
                sha256=None,
            ),
            models=[],
            findings=[],
        )
        output_path = cast(Path | None, kwargs.get("output_path"))
        if output_path is not None:
            output_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return report

    monkeypatch.setattr(mcp_server.libverify, "verify_library_part", fake_verify)

    async def exercise() -> None:
        land_pattern = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_land_pattern",
                {
                    "part_spec_path": str(spec_path),
                    "library_dir": str(library_dir),
                    "rule_profile": "builtin:kicad-generator",
                },
            ),
        )
        assert land_pattern.isError is False
        land_pattern_report = json.loads(
            (tmp_path / "circuit-reports" / "part.land-pattern.json").read_text(encoding="utf-8")
        )
        assert land_pattern_report["rule_chain"] == ["builtin:kicad-generator"]

        candidates = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_library_candidates",
                {
                    "part_spec_path": str(spec_path),
                    "product": "controller-board",
                },
            ),
        )
        assert candidates.isError is False
        assert candidate_products == ["controller-board"]
        assert (tmp_path / "circuit-reports" / "part.library-candidates.json").is_file()

        imported = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_library_import",
                {
                    "source_path": str(source_path),
                    "library_dir": str(library_dir),
                    "nickname": "Fixture",
                    "origin": "manufacturer",
                    "vendor": "Example",
                    "retrieved_at": "2026-01-01T00:00:00Z",
                    "license": {
                        "spdx": "MIT",
                        "attribution": "Example",
                        "redistribution": "allowed",
                    },
                },
            ),
        )
        assert imported.isError is False
        assert captured_sources[0].origin == "manufacturer"
        assert (tmp_path / "circuit-reports" / "Fixture.library-import.json").is_file()

        recorded = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_library_record",
                {
                    "library_dir": str(library_dir),
                    "artifact_path": str(artifact_path),
                    "artifact": "footprint",
                    "name": "Generated",
                    "transformation": "Generated by Konnect",
                    "origin": "generated",
                    "vendor": "Konnect",
                    "license": {
                        "spdx": "MIT",
                        "attribution": "Example",
                        "redistribution": "allowed",
                    },
                    "part_spec_path": str(spec_path),
                },
            ),
        )
        assert recorded.isError is False
        assert (library_dir / "provenance.json").is_file()
        assert (tmp_path / "circuit-reports" / "library.library-record.json").is_file()

        verified = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_library_verify",
                {
                    "part_spec_path": str(spec_path),
                    "symbol_lib_path": str(symbol_path),
                    "symbol_name": "TEST-1",
                    "footprint_path": str(footprint_path),
                    "library_dir": str(library_dir),
                    "rule_profile": "builtin:kicad-generator",
                },
            ),
        )
        assert verified.isError is False
        assert (tmp_path / "circuit-reports" / "part.library-verification.json").is_file()
        assert verification_options == [True]
        assert verification_rule_chains == [["builtin:kicad-generator"]]

        def build_packet(
            _spec_path: Path,
            *,
            symbol_lib: Path,
            symbol_name: str,
            footprint_path: Path,
            library_dir: Path,
            density: Density,
            tolerance_mm: float = 0.02,
            model_required: bool = True,
            out_dir: Path,
        ) -> mcp_server.libreview.ReviewPacket:
            del symbol_lib, symbol_name, footprint_path, density, tolerance_mm
            del model_required, out_dir
            return mcp_server.libreview.ReviewPacket(
                artifact_kind="circuit_library_review_packet",
                packet_id="a" * 16,
                packet_dir=library_dir / "reviews" / "part" / ("a" * 16),
                approvable=True,
                inputs={},
                findings=[],
                unknowns=[],
            )

        def current_packet(
            _spec_path: Path,
            *,
            symbol_lib: Path,
            symbol_name: str,
            footprint_path: Path,
            library_dir: Path | None,
            density: Density,
            tolerance_mm: float,
            model_required: bool,
        ) -> str:
            del (
                symbol_lib,
                symbol_name,
                footprint_path,
                library_dir,
                density,
                tolerance_mm,
                model_required,
            )
            return "a" * 16

        def approved_status(
            _library_dir: Path,
            _spec: PartSpec,
            _packet_id: str,
            *,
            spec_path: Path,
        ) -> mcp_server.libreview.ReviewStatus:
            del spec_path
            return mcp_server.libreview.ReviewStatus(
                artifact_kind="circuit_library_review_status",
                packet_id="a" * 16,
                state="approved",
                reasons=[],
                decisions=[],
            )

        def no_decisions(
            _library_dir: Path, _packet_id: str
        ) -> list[mcp_server.libreview.ReviewDecision]:
            return []

        monkeypatch.setattr(mcp_server.libreview, "build_review_packet", build_packet)
        monkeypatch.setattr(mcp_server.libreview, "current_packet_id", current_packet)
        monkeypatch.setattr(mcp_server.libreview, "review_status", approved_status)
        monkeypatch.setattr(mcp_server.libreview, "load_decisions", no_decisions)
        packet = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_library_review_packet",
                {
                    "part_spec_path": str(spec_path),
                    "symbol_lib_path": str(symbol_path),
                    "symbol_name": "TEST-1",
                    "footprint_path": str(footprint_path),
                    "library_dir": str(library_dir),
                },
            ),
        )
        assert packet.isError is False
        assert (tmp_path / "circuit-reports" / "part.library-review-packet.json").is_file()

        status = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_library_review_status",
                {
                    "part_spec_path": str(spec_path),
                    "symbol_lib_path": str(symbol_path),
                    "symbol_name": "TEST-1",
                    "footprint_path": str(footprint_path),
                    "library_dir": str(library_dir),
                },
            ),
        )
        assert status.isError is False
        assert (tmp_path / "circuit-reports" / "part.library-review-status.json").is_file()

        applied = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_library_review_apply",
                {
                    "part_spec_path": str(spec_path),
                    "library_dir": str(library_dir),
                    "packet_id": "a" * 16,
                    "event_sha12": "b" * 12,
                },
            ),
        )
        assert applied.isError is False
        assert (tmp_path / "circuit-reports" / "part.library-review-apply.json").is_file()

        decisions = [
            mcp_server.libreview.ReviewDecision(
                packet_id="a" * 16,
                decision="reject",
                reviewer="Reviewer",
                answers={},
                corrections=[
                    mcp_server.libreview.ReviewCorrection(
                        pointer="/manufacturer",
                        old="Example",
                        new="Corrected",
                        reason="source correction",
                        page=1,
                    )
                ],
                event_path=tmp_path / "event-a.json",
                event_sha256="c" * 64,
                event_mtime_ns=1,
                event_name="event-a.json",
                valid=True,
                reasons=[],
            ),
            mcp_server.libreview.ReviewDecision(
                packet_id="a" * 16,
                decision="reject",
                reviewer="Reviewer",
                answers={},
                corrections=[
                    mcp_server.libreview.ReviewCorrection(
                        pointer="/manufacturer",
                        old="Example",
                        new="Corrected",
                        reason="source correction",
                        page=1,
                    )
                ],
                event_path=tmp_path / "event-b.json",
                event_sha256="b" * 64,
                event_mtime_ns=2,
                event_name="event-b.json",
                valid=True,
                reasons=[],
            ),
        ]
        selected: list[str | None] = []

        def selected_decisions(
            _library_dir: Path, _packet_id: str
        ) -> list[mcp_server.libreview.ReviewDecision]:
            return decisions

        def apply_selected(
            _spec_path: Path,
            decision: mcp_server.libreview.ReviewDecision,
        ) -> mcp_server.libreview.CorrectionResult:
            selected.append(decision.event_sha256)
            return mcp_server.libreview.CorrectionResult(
                artifact_kind="circuit_library_review_correction",
                applied=True,
                packet_id=decision.packet_id,
                applied_pointers=["/manufacturer"],
                reasons=[],
            )

        monkeypatch.setattr(mcp_server.libreview, "load_decisions", selected_decisions)
        monkeypatch.setattr(mcp_server.libreview, "apply_corrections", apply_selected)
        selected_apply = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_library_review_apply",
                {
                    "part_spec_path": str(spec_path),
                    "library_dir": str(library_dir),
                    "packet_id": "a" * 16,
                    "event_sha12": "b" * 12,
                },
            ),
        )
        assert selected_apply.isError is False
        assert selected == ["b" * 64]

    asyncio.run(exercise())


def test_stdio_server_lists_tools_and_reports_version(tmp_path: Path) -> None:
    fake = tmp_path / "kicad-cli"
    fake.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "10.99.0"; exit 0; fi\nexit 1\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)

    async def exercise() -> None:
        env = {**os.environ, "CIRCUIT_KICAD_CLI": str(fake)}
        params = StdioServerParameters(
            command="python3",
            args=["-m", "circuit.mcp_server"],
            env=env,
        )
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 45
            for tool in tools.tools:
                assert tool.annotations is not None
                assert tool.annotations.title
                assert tool.annotations.openWorldHint is False
            annotations_by_name = {tool.name: tool.annotations for tool in tools.tools}
            konnect = annotations_by_name["circuit_konnect_call"]
            doctor = annotations_by_name["circuit_doctor"]
            erc = annotations_by_name["circuit_erc"]
            review_status = annotations_by_name["circuit_library_review_status"]
            review_apply = annotations_by_name["circuit_library_review_apply"]
            assert konnect is not None and konnect.destructiveHint is True
            assert doctor is not None and doctor.readOnlyHint is True
            assert erc is not None and erc.readOnlyHint is False
            assert review_status is not None and review_status.readOnlyHint is False
            assert review_apply is not None and review_apply.readOnlyHint is False
            result = await session.call_tool("circuit_kicad_version", {})
            assert result.isError is False
            content = result.content[0]
            assert isinstance(content, TextContent)
            assert content.text == '"10.99.0"'

    asyncio.run(exercise())


def test_brief_validate_tool(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(Path(__file__).resolve().parents[1]))
    brief_path = Path(__file__).parent / "data" / "brief_led_loop.json"

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool("circuit_brief_validate", {"brief_path": str(brief_path)}),
        )
        assert result.isError is False
        assert '"brief_sha256"' in result.content[0].text

    asyncio.run(exercise())


def test_path_arguments_accept_workspace_relative_paths(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    sample = Path(__file__).parent / "data" / "brief_led_loop.json"
    (tmp_path / "brief.json").write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool("circuit_brief_validate", {"brief_path": "brief.json"}),
        )
        assert result.isError is False
        assert '"brief_sha256"' in result.content[0].text

    asyncio.run(exercise())


def test_path_arguments_reject_parent_traversal(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_brief_validate",
                {"brief_path": "../outside.json"},
            ),
        )
        assert result.isError is True
        assert "outside the workspace" in result.content[0].text

    asyncio.run(exercise())


def test_library_import_accepts_official_library_sources(tmp_path: Path, monkeypatch: Any) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    official_root = tmp_path / "kicad"
    source = official_root / "symbols" / "Fixture.kicad_sym"
    source.parent.mkdir(parents=True)
    source.write_text("fixture", encoding="utf-8")
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(workspace))
    monkeypatch.setenv("CIRCUIT_KICAD_SHARE", str(official_root))

    arguments = mcp_server._workspace_arguments(  # pyright: ignore[reportPrivateUsage]
        "circuit_library_import",
        {"source_path": str(source), "origin": "kicad_official"},
    )

    assert arguments["source_path"] == str(source.resolve())


def test_path_arguments_reject_outside_absolute_path(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_brief_validate",
                {"brief_path": str(outside)},
            ),
        )
        assert result.isError is True
        assert "outside the workspace" in result.content[0].text

    asyncio.run(exercise())


def test_path_arguments_reject_symlink_components(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_brief_validate",
                {"brief_path": str(linked / "brief.json")},
            ),
        )
        assert result.isError is True
        assert "symlink" in result.content[0].text

    asyncio.run(exercise())


def test_render_result_includes_image_content(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    # Smallest valid PNG (1x1 transparent pixel).
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c626001000000ffff03000006000557bfabd40000000049"
        "454e44ae426082"
    )
    out_path = tmp_path / "render.png"
    out_path.write_bytes(png_bytes)

    def fake_render(
        pcb: Path,
        out: Path,
        *,
        side: str,
        width: int = 1280,
        height: int = 720,
        **_: object,
    ) -> Path:
        return out_path

    monkeypatch.setattr(mcp_server.kicad_cli, "render", fake_render)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_render",
                {
                    "board_path": str(tmp_path / "board.kicad_pcb"),
                    "output_path": str(out_path),
                    "side": "top",
                },
            ),
        )
        assert result.isError is False
        assert isinstance(result.content[0], TextContent)
        image = result.content[1]
        assert isinstance(image, ImageContent)
        assert image.mimeType == "image/png"
        assert base64.b64decode(image.data) == png_bytes

    asyncio.run(exercise())


def test_render_result_text_only_when_png_missing(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))

    def fake_render(
        pcb: Path,
        out: Path,
        *,
        side: str,
        width: int = 1280,
        height: int = 720,
        **_: object,
    ) -> Path:
        return tmp_path / "render.png"

    monkeypatch.setattr(mcp_server.kicad_cli, "render", fake_render)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_render",
                {
                    "board_path": str(tmp_path / "board.kicad_pcb"),
                    "output_path": str(tmp_path / "render.png"),
                    "side": "top",
                },
            ),
        )
        assert result.isError is False
        assert len(result.content) == 1
        assert isinstance(result.content[0], TextContent)

    asyncio.run(exercise())


def test_render_schematic_kind_attaches_images(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c626001000000ffff03000006000557bfabd40000000049"
        "454e44ae426082"
    )
    out_dir = tmp_path / "sch-png"
    produced = [out_dir / f"board-{page}.png" for page in (1, 2)]
    for path in produced:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes)

    def fake_render_schematic(sch: Path, out: Path, **_: object) -> list[Path]:
        return produced

    monkeypatch.setattr(mcp_server.kicad_cli, "render_schematic", fake_render_schematic)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_render",
                {
                    "kind": "schematic",
                    "schematic_path": str(tmp_path / "board.kicad_sch"),
                    "output_dir": str(out_dir),
                },
            ),
        )
        assert result.isError is False
        assert isinstance(result.content[0], TextContent)
        payload = json.loads(result.content[0].text)
        assert payload["images"] == [str(path) for path in produced]
        images = result.content[1:]
        assert len(images) == 2
        assert all(isinstance(image, ImageContent) for image in images)

    asyncio.run(exercise())


def test_render_layers_kind_attaches_capped_images(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    png_bytes = b"\x89PNG" + b"0" * 32
    out_dir = tmp_path / "layers"
    out_dir.mkdir()
    produced = [out_dir / f"board-{index}.png" for index in range(6)]
    for path in produced:
        path.write_bytes(png_bytes)

    def fake_render_layers(pcb: Path, out: Path, **kwargs: object) -> list[Path]:
        assert kwargs["layers"] == "F.Cu,B.Cu"
        return produced

    monkeypatch.setattr(mcp_server.kicad_cli, "render_layers", fake_render_layers)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_render",
                {
                    "kind": "layers",
                    "board_path": str(tmp_path / "board.kicad_pcb"),
                    "output_dir": str(out_dir),
                    "layers": "F.Cu,B.Cu",
                },
            ),
        )
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        assert len(payload["images"]) == 6
        assert len(result.content) == 1 + 4  # inline image cap

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "kind,missing",
    [
        ("board3d", "output_path"),
        ("schematic", "schematic_path"),
        ("schematic", "output_dir"),
        ("layers", "board_path"),
        ("layers", "layers"),
    ],
)
def test_render_kind_requires_its_inputs(
    tmp_path: Path, monkeypatch: Any, kind: str, missing: str
) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))

    async def exercise() -> None:
        args: dict[str, Any] = {
            "kind": kind,
            "board_path": str(tmp_path / "board.kicad_pcb"),
            "schematic_path": str(tmp_path / "board.kicad_sch"),
            "output_path": str(tmp_path / "out.png"),
            "output_dir": str(tmp_path / "out"),
            "layers": "F.Cu",
        }
        del args[missing]
        result = cast(Any, await mcp_server.call_tool("circuit_render", args))
        assert result.isError is True
        assert isinstance(result.content[0], TextContent)
        assert missing in result.content[0].text

    asyncio.run(exercise())


def test_render_unknown_kind_errors(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_render",
                {"kind": "cross_section", "board_path": str(tmp_path / "b.kicad_pcb")},
            ),
        )
        assert result.isError is True
        assert "unknown circuit_render kind" in result.content[0].text

    asyncio.run(exercise())


def test_diff_png_attaches_image(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    png_bytes = b"\x89PNG" + b"0" * 32
    out_path = tmp_path / "diff.png"
    out_path.write_bytes(png_bytes)

    def fake_diff(
        kind: str, left: Path, right: Path, out: Path, *, format: str = "json"
    ) -> DiffReport:
        assert format == "png"
        return DiffReport(
            kind=cast(Any, kind),
            left=left,
            right=right,
            identical=False,
            exit_code=5,
            output=out_path,
            format="png",
        )

    monkeypatch.setattr(mcp_server.kicad_cli, "diff", fake_diff)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_diff",
                {
                    "kind": "pcb",
                    "left_path": str(tmp_path / "a.kicad_pcb"),
                    "right_path": str(tmp_path / "b.kicad_pcb"),
                    "output_path": str(out_path),
                    "format": "png",
                },
            ),
        )
        assert result.isError is False
        image = result.content[1]
        assert isinstance(image, ImageContent)
        assert base64.b64decode(image.data) == png_bytes

    asyncio.run(exercise())


def test_rewrite_text_block_images_extracts_payload(tmp_path: Path) -> None:
    png_bytes = b"\x89PNG" + b"payload" * 200
    encoded = base64.b64encode(png_bytes).decode("ascii")
    counter = [0]
    rewritten = mcp_server._rewrite_text_block_images(  # pyright: ignore[reportPrivateUsage]
        json.dumps({"tool": "render_schematic_png", "png_base64": encoded}),
        tmp_path / "images",
        counter,
    )
    payload = json.loads(rewritten)
    entry = payload["png_base64"]
    assert Path(entry["image_path"]).read_bytes() == png_bytes
    assert entry["sha256"] == hashlib.sha256(png_bytes).hexdigest()


def test_rewrite_text_block_images_ignores_non_image_text(tmp_path: Path) -> None:
    counter = [0]
    text = json.dumps({"result": "all good", "note": "x" * 5000})
    rewritten = mcp_server._rewrite_text_block_images(  # pyright: ignore[reportPrivateUsage]
        text, tmp_path / "images", counter
    )
    assert rewritten == text
    assert (
        mcp_server._rewrite_text_block_images(  # pyright: ignore[reportPrivateUsage]
            "not json at all", tmp_path / "images", counter
        )
        == "not json at all"
    )


def test_rewrite_base64_images_handles_data_url_and_image_blocks(
    tmp_path: Path,
) -> None:
    png_bytes = b"\x89PNG" + b"payload" * 200
    encoded = base64.b64encode(png_bytes).decode("ascii")
    counter = [0]
    rewritten = mcp_server._rewrite_base64_images(  # pyright: ignore[reportPrivateUsage]
        {
            "type": "image",
            "data": encoded,
            "mimeType": "image/png",
            "nested": [{"url": f"data:image/png;base64,{encoded}"}],
        },
        tmp_path / "images",
        counter,
    )
    assert isinstance(rewritten, dict)
    rewritten_dict = cast(dict[str, Any], rewritten)
    assert str(rewritten_dict["data"]["image_path"]).endswith(".png")
    nested = cast(list[dict[str, Any]], rewritten_dict["nested"])
    assert (
        str(cast(dict[str, Any], nested[0]["url"])["sha256"])
        == hashlib.sha256(png_bytes).hexdigest()
    )
    assert counter[0] == 2


def test_konnect_call_ops_extract_image_blocks(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    png_bytes = (
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c626001000000ffff03000006000557bfabd40000000049"
            "454e44ae426082"
        )
        + b"pad" * 400
    )
    out_path = tmp_path / "render.png"
    out_path.write_bytes(png_bytes)

    # The managed konnect subprocess cannot inherit monkeypatches, so wrap the
    # real server in a script that stubs kicad_cli.render for the child.
    wrapper = tmp_path / "konnect_stub.py"
    wrapper.write_text(
        "import asyncio\n"
        "from pathlib import Path\n"
        "from circuit import mcp_server\n"
        f"PNG = {png_bytes!r}\n"
        "def fake_render(pcb, out, **_):\n"
        "    out.write_bytes(PNG)\n"
        "    return out\n"
        "mcp_server.kicad_cli.render = fake_render\n"
        "asyncio.run(mcp_server._run())\n",
        encoding="utf-8",
    )
    _fake_konnect(tmp_path, monkeypatch)
    monkeypatch.setenv("CIRCUIT_KONNECT", f"python3 {wrapper}")
    monkeypatch.setenv("CIRCUIT_KONNECT_IMAGE_DIR", str(tmp_path / "konnect-images"))

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_konnect_call",
                {
                    "ops": [
                        {
                            "tool": "circuit_render",
                            "arguments": {
                                "kind": "board3d",
                                "board_path": str(tmp_path / "b.kicad_pcb"),
                                "output_path": str(out_path),
                                "side": "top",
                            },
                        }
                    ],
                },
            ),
        )
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        blocks = cast(list[Any], payload["results"][0]["content"])
        dict_blocks = [cast(dict[str, Any], block) for block in blocks if isinstance(block, dict)]
        image_data = cast(dict[str, str], dict_blocks[0]["data"])
        assert image_data["sha256"] == hashlib.sha256(png_bytes).hexdigest()
        extracted = Path(image_data["image_path"])
        assert extracted.is_file()
        assert extracted.read_bytes() == png_bytes

    asyncio.run(exercise())


def test_konnect_call_proxies_to_managed_subprocess(tmp_path: Path, monkeypatch: Any) -> None:
    fake = tmp_path / "kicad-cli"
    fake.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "10.99.0"; exit 0; fi\nexit 1\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("CIRCUIT_KICAD_CLI", str(fake))
    monkeypatch.setenv("CIRCUIT_KONNECT", "python3 -m circuit.mcp_server")

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_konnect_call",
                {
                    "tool": "circuit_kicad_version",
                    "socket": "ipc:///tmp/circuit-kicad.sock",
                },
            ),
        )
        assert result.isError is False
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert "10.99.0" in content.text

    asyncio.run(exercise())


def _fake_konnect(tmp_path: Path, monkeypatch: Any) -> None:
    fake = tmp_path / "kicad-cli"
    fake.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "10.99.0"; exit 0; fi\nexit 1\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("CIRCUIT_KICAD_CLI", str(fake))
    monkeypatch.setenv("CIRCUIT_KONNECT", "python3 -m circuit.mcp_server")


def test_konnect_call_runs_ops_in_one_session(tmp_path: Path, monkeypatch: Any) -> None:
    _fake_konnect(tmp_path, monkeypatch)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_konnect_call",
                {
                    "ops": [
                        {"tool": "circuit_kicad_version"},
                        {"tool": "circuit_kicad_version", "arguments": {}},
                    ],
                },
            ),
        )
        assert result.isError is False
        content = result.content[0]
        assert isinstance(content, TextContent)
        payload = json.loads(content.text)
        assert len(payload["results"]) == 2
        assert all("10.99.0" in entry["content"][0] for entry in payload["results"])

    asyncio.run(exercise())


def test_konnect_call_ops_propagate_errors(tmp_path: Path, monkeypatch: Any) -> None:
    _fake_konnect(tmp_path, monkeypatch)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_konnect_call",
                {"ops": [{"tool": "no_such_tool"}, {"tool": "circuit_kicad_version"}]},
            ),
        )
        assert result.isError is True
        content = result.content[0]
        assert isinstance(content, TextContent)
        payload = json.loads(content.text)
        assert payload["results"][0]["isError"] is True
        assert payload["results"][1]["isError"] is False

    asyncio.run(exercise())


def test_konnect_call_requires_tool_or_ops(tmp_path: Path, monkeypatch: Any) -> None:
    _fake_konnect(tmp_path, monkeypatch)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool("circuit_konnect_call", {}),
        )
        assert result.isError is True
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert "requires" in content.text

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "socket",
    ["tcp://127.0.0.1:9000", "file:///tmp/socket", "/tmp/socket", "ipc:/tmp/socket", "", None],
)
def test_konnect_call_rejects_non_ipc_socket(tmp_path: Path, monkeypatch: Any, socket: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_konnect_call",
                {"tool": "circuit_kicad_version", "socket": socket},
            ),
        )
        assert result.isError is True
        assert "must use ipc://" in result.content[0].text

    asyncio.run(exercise())


def test_konnect_call_contains_nested_tool_and_operation_paths(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))

    async def exercise() -> None:
        for arguments in [
            {
                "tool": "circuit_render",
                "arguments": {
                    "board_path": "../outside.kicad_pcb",
                    "output_path": "render.png",
                },
            },
            {
                "ops": [
                    {
                        "tool": "circuit_render",
                        "arguments": {
                            "board_path": "../outside.kicad_pcb",
                            "output_path": "render.png",
                        },
                    }
                ]
            },
        ]:
            result = cast(
                Any,
                await mcp_server.call_tool("circuit_konnect_call", arguments),
            )
            assert result.isError is True
            assert "outside the workspace" in result.content[0].text

    asyncio.run(exercise())


def _write_gate_reports(project: Path) -> Path:
    reports = project / "circuit-reports"
    reports.mkdir(parents=True)
    brief_path = Path(__file__).parent / "data" / "brief_led_loop.json"
    connectivity = ConnectivityReport(
        brief_path=brief_path,
        netlist_path=project / "circuit-reports" / "board.net",
        brief_sha256="0" * 64,
        expected={"VIN": ["J1.1", "R1.1"]},
        actual={"VIN": ["J1.1", "R1.1"]},
        missing_nets=[],
        mismatched_nets={},
        unexpected_nets=[],
        missing_parts=[],
        footprint_mismatches={},
        verdict="pass",
    )
    (reports / "board.connectivity.json").write_text(
        connectivity.model_dump_json(), encoding="utf-8"
    )
    sch_lint_report = SchLintReport(
        source=project / "board.kicad_sch",
        verdict="pass",
        errors=0,
        warnings=0,
        symbols_checked=1,
    )
    (reports / "board.sch_lint.json").write_text(
        sch_lint_report.model_dump_json(), encoding="utf-8"
    )
    (reports / "board.erc.json").write_text(
        json.dumps(
            {
                "$schema": "https://schemas.kicad.org/erc.v1.json",
                "kicad_version": "11.0",
                "sheets": [{"path": "/", "violations": []}],
            }
        ),
        encoding="utf-8",
    )
    (reports / "board.drc.json").write_text(
        json.dumps(
            {
                "$schema": "https://schemas.kicad.org/drc.v1.json",
                "kicad_version": "11.0",
                "unconnected_items": [],
                "violations": [],
                "schematic_parity": [],
            }
        ),
        encoding="utf-8",
    )
    return reports


def test_design_report_collects_pipeline_sections(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    project = tmp_path
    brief_path = project / "brief.json"
    sample = Path(__file__).parent / "data" / "brief_led_loop.json"
    brief_path.write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")
    (project / "board.kicad_sch").write_text("()", encoding="utf-8")
    (project / "board.kicad_pcb").write_text("()", encoding="utf-8")
    reports = _write_gate_reports(project)

    gerber = project / "exports" / "gerbers" / "board-F_Cu.gtl"
    gerber.parent.mkdir(parents=True)
    gerber.write_text("gerber", encoding="utf-8")
    (reports / "render-top.png").write_bytes(b"\x89PNG")
    advisory = AdvisoryResult(
        tool="run_erc",
        stage="schematic",
        status="ok",
        summary="konnect erc clean",
    )
    (reports / "erc.advisory.json").write_text(advisory.model_dump_json(), encoding="utf-8")
    (reports / "advisory.jsonl").write_text(advisory.model_dump_json() + "\n", encoding="utf-8")
    diff = DiffReport(
        kind="pcb",
        left=project / "board.kicad_pcb",
        right=project / "board.kicad_pcb",
        identical=True,
        exit_code=0,
        output=reports / "board.pcb.diff.json",
        changes=[],
    )
    (reports / "board.pcb.diff.json").write_text(diff.model_dump_json(), encoding="utf-8")
    jobset_erc = reports / "jobset-erc.json"
    jobset_erc.write_text(
        (reports / "board.erc.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    jobset = JobsetResult(
        jobset=Path("default.kicad_jobset"),
        project=project / "board.kicad_pro",
        output_dir=project / "jobset",
        exit_code=0,
        outputs=[jobset_erc],
        erc_report=jobset_erc,
        drc_report=None,
    )
    (reports / "board.jobset.json").write_text(jobset.model_dump_json(), encoding="utf-8")

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_design_report",
                {
                    "brief_path": str(brief_path),
                    "schematic_path": str(project / "board.kicad_sch"),
                    "board_path": str(project / "board.kicad_pcb"),
                },
            ),
        )
        assert result.isError is False
        report_path = reports / "board.design-report.json"
        assert report_path.is_file()
        design = DesignReport.model_validate_json(report_path.read_text(encoding="utf-8"))
        assert design.verdict == "pass"
        assert design.connectivity is not None
        assert design.sch_lint is not None
        assert design.erc is not None and design.erc.verdict == "pass"
        assert design.drc is not None and design.drc.verdict == "pass"
        assert design.exports == {"gerbers": [str(gerber)]}
        assert [str(reports / "render-top.png")] == design.renders
        assert len(design.advisory) == 2
        assert design.jobset is not None
        assert design.jobset_consistent is True
        assert "board.pcb" in design.diffs
        assert design.diffs["board.pcb"].identical is True

    asyncio.run(exercise())


def test_import_dispatches_and_returns_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    from circuit.kicad_cli import ImportResult

    def fake_import(kind: str, source: Path, output: Path, *, format: str) -> ImportResult:
        return ImportResult(
            kind=cast(Any, kind),
            source=source,
            output=output,
            report_path=output.with_name(output.name + ".import.json"),
            report={"source_format": "LTspice"},
        )

    monkeypatch.setattr(mcp_server.kicad_cli, "import_file", fake_import)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_import",
                {
                    "kind": "sch",
                    "source_path": str(tmp_path / "in.asc"),
                    "output_path": str(tmp_path / "out.kicad_sch"),
                    "format": "ltspice",
                },
            ),
        )
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        assert payload["report"] == {"source_format": "LTspice"}
        assert payload["output"].endswith("out.kicad_sch")

    asyncio.run(exercise())


def test_stackup_writes_json_and_svg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    out_dir = tmp_path / "stackup"

    def fake_stackup(board: Path, out: Path) -> dict[str, object]:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('{"layers":[]}', encoding="utf-8")
        return {"layers": [{"type": "BSLT_COPPER", "enabled": True}]}

    monkeypatch.setattr(mcp_server.kicad_cli, "export_stackup", fake_stackup)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_stackup",
                {"board_path": str(tmp_path / "board.kicad_pcb"), "output_dir": str(out_dir)},
            ),
        )
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        assert Path(payload["json_path"]).is_file()
        svg = Path(payload["svg_path"])
        assert svg.is_file() and svg.read_text(encoding="utf-8").startswith("<svg")

    asyncio.run(exercise())


def test_rasterize_attaches_pngs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c626001000000ffff03000006000557bfabd40000000049"
        "454e44ae426082"
    )
    page = tmp_path / "doc-1.png"
    page.write_bytes(png_bytes)

    def fake_rasterize(source: Path, out_dir: Path, *, dpi: int) -> list[Path]:
        return [page]

    monkeypatch.setattr(mcp_server.raster, "rasterize", fake_rasterize)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_rasterize",
                {
                    "source_path": str(tmp_path / "doc.pdf"),
                    "output_dir": str(tmp_path / "pages"),
                },
            ),
        )
        assert result.isError is False
        image = result.content[1]
        assert isinstance(image, ImageContent)
        assert image.mimeType == "image/png"

    asyncio.run(exercise())


def test_fp_svg_export_accepts_workspace_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    library = tmp_path / "workspace.pretty"
    library.mkdir()
    (library / "part.kicad_mod").write_text("(footprint)", encoding="utf-8")
    output_dir = tmp_path / "exports"
    calls: list[tuple[str, Path, Path]] = []

    def fake_export(kind: str, source: Path, output: Path) -> dict[str, str]:
        calls.append((kind, source, output))
        return {"kind": kind}

    monkeypatch.setattr(mcp_server.kicad_cli, "export", fake_export)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_export",
                {
                    "kind": "fp_svg",
                    "source_path": str(library),
                    "output_dir": "exports",
                },
            ),
        )
        assert result.isError is False
        assert calls == [("fp_svg", library, output_dir)]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("library_env", "library_subdir"),
    [
        ("CIRCUIT_KICAD_SHARE", "footprints"),
        ("CIRCUIT_CERN_LIBS", "PcbLib"),
    ],
)
def test_fp_svg_export_accepts_installed_library_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    library_env: str,
    library_subdir: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(workspace))
    monkeypatch.setenv("CIRCUIT_KICAD_SHARE", str(tmp_path / "kicad"))
    monkeypatch.setenv("CIRCUIT_CERN_LIBS", str(tmp_path / "cern"))
    library_root = Path(os.environ[library_env]) / library_subdir
    library = library_root / "installed.pretty"
    library.mkdir(parents=True)
    (library / "part.kicad_mod").write_text("(footprint)", encoding="utf-8")
    output_dir = workspace / "exports"
    calls: list[tuple[str, Path, Path]] = []

    def fake_export(kind: str, source: Path, output: Path) -> dict[str, str]:
        calls.append((kind, source, output))
        return {"kind": kind}

    monkeypatch.setattr(mcp_server.kicad_cli, "export", fake_export)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_export",
                {
                    "kind": "fp_svg",
                    "source_path": str(library),
                    "output_dir": "exports",
                },
            ),
        )
        assert result.isError is False
        assert calls == [("fp_svg", library, output_dir)]

    asyncio.run(exercise())


def test_fp_svg_export_rejects_external_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(workspace))
    monkeypatch.setenv("CIRCUIT_KICAD_SHARE", str(tmp_path / "kicad"))
    monkeypatch.setenv("CIRCUIT_CERN_LIBS", str(tmp_path / "cern"))
    outside = tmp_path / "outside.pretty"
    outside.mkdir()
    (outside / "part.kicad_mod").write_text("(footprint)", encoding="utf-8")

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_export",
                {
                    "kind": "fp_svg",
                    "source_path": str(outside),
                    "output_dir": "exports",
                },
            ),
        )
        assert result.isError is True
        assert "installed KiCad/CERN library" in result.content[0].text

    asyncio.run(exercise())


def _fail_if_called(name: str) -> Any:
    def fake(*_args: object, **_kwargs: object) -> None:
        pytest.fail(f"kicad_cli.{name} must not be called with invalid literal args")

    return fake


@pytest.mark.parametrize(
    ("tool", "arguments", "key", "value"),
    [
        (
            "circuit_render",
            {"board_path": "b.kicad_pcb", "output_path": "out.png", "side": "diagonal"},
            "side",
            "diagonal",
        ),
        (
            "circuit_render",
            {"board_path": "b.kicad_pcb", "output_path": "out.png", "background": "neon"},
            "background",
            "neon",
        ),
        (
            "circuit_render",
            {"board_path": "b.kicad_pcb", "output_path": "out.png", "quality": "ultra"},
            "quality",
            "ultra",
        ),
        (
            "circuit_diff",
            {
                "kind": "sch",
                "left_path": "a.kicad_sch",
                "right_path": "b.kicad_sch",
                "output_path": "out.json",
                "format": "pdf",
            },
            "format",
            "pdf",
        ),
        (
            "circuit_diff",
            {
                "kind": "board",
                "left_path": "a.kicad_pcb",
                "right_path": "b.kicad_pcb",
                "output_path": "out.json",
            },
            "kind",
            "board",
        ),
        (
            "circuit_export",
            {"kind": "bogus", "source_path": "b.kicad_pcb", "output_dir": "out"},
            "kind",
            "bogus",
        ),
        (
            "circuit_import",
            {"kind": "bogus", "source_path": "a.sch", "output_path": "out.kicad_sch"},
            "kind",
            "bogus",
        ),
    ],
)
def test_call_tool_rejects_invalid_literal_args(
    tmp_path: Path,
    monkeypatch: Any,
    tool: str,
    arguments: dict[str, str],
    key: str,
    value: str,
) -> None:
    monkeypatch.setattr(mcp_server.kicad_cli, "render", _fail_if_called("render"))
    monkeypatch.setattr(mcp_server.kicad_cli, "diff", _fail_if_called("diff"))
    monkeypatch.setattr(mcp_server.kicad_cli, "export", _fail_if_called("export"))
    monkeypatch.setattr(mcp_server.kicad_cli, "import_file", _fail_if_called("import_file"))

    async def exercise() -> None:
        result = cast(Any, await mcp_server.call_tool(tool, arguments))
        assert result.isError is True
        text = result.content[0].text
        assert f"'{key}'" in text
        assert repr(value) in text

    asyncio.run(exercise())


def test_render_valid_literal_args_pass_through(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    captured: dict[str, object] = {}
    out_path = tmp_path / "render.png"
    out_path.write_bytes(b"\x89PNG" + b"0" * 32)

    def fake_render(pcb: Path, out: Path, **kwargs: object) -> Path:
        captured.update(kwargs)
        return out_path

    monkeypatch.setattr(mcp_server.kicad_cli, "render", fake_render)

    async def exercise() -> None:
        result = cast(
            Any,
            await mcp_server.call_tool(
                "circuit_render",
                {
                    "board_path": str(tmp_path / "b.kicad_pcb"),
                    "output_path": str(out_path),
                    "side": "bottom",
                    "background": "",
                    "quality": "high",
                },
            ),
        )
        assert result.isError is False

    asyncio.run(exercise())

    assert captured["side"] == "bottom"
    assert captured["background"] is None
    assert captured["quality"] == "high"


def test_tool_schema_enums_match_kicad_cli_literals() -> None:
    schemas = {name: schema for name, _, schema in mcp_server._TOOLS}  # pyright: ignore[reportPrivateUsage]
    render_props = schemas["circuit_render"]["properties"]
    assert render_props["side"]["enum"] == list(mcp_server.kicad_cli.CAMERA_SIDES)
    assert render_props["background"]["enum"] == list(mcp_server.kicad_cli.RENDER_BACKGROUNDS)
    assert render_props["quality"]["enum"] == list(mcp_server.kicad_cli.RENDER_QUALITIES)
    diff_props = schemas["circuit_diff"]["properties"]
    assert diff_props["kind"]["enum"] == list(mcp_server.kicad_cli.DIFF_KINDS)
    assert diff_props["format"]["enum"] == list(mcp_server.kicad_cli.DIFF_FORMATS)
    assert schemas["circuit_export"]["properties"]["kind"]["enum"] == list(
        mcp_server.kicad_cli.EXPORT_KINDS
    )
    assert schemas["circuit_import"]["properties"]["kind"]["enum"] == list(
        mcp_server.kicad_cli.IMPORT_KINDS
    )
