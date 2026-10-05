from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import cast

from circuit import advisory, corpus, datasheet, model3d, partspec
from circuit.mutation import library_mutation_fixture, run_mutations

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_ROOT = _REPO_ROOT / "tests" / "data" / "corpus_parts"
_FIXTURE_DIR = _FIXTURE_ROOT / "tps62130-vqfn16"
_MANIFEST_PATH = _REPO_ROOT / "library" / "corpus" / "corpus.json"
_IMPRESSION = (
    "The crop is legible and the cited marks are distinguishable from the "
    "surrounding linework. The drawing context is visible and no clipping "
    "obscures the reading. A reviewer comparing the crop against the spec text "
    "would find the values consistent with what is claimed. The resolution is "
    "adequate for the glyphs at this zoom, and nothing in the frame looks "
    "surprising or out of place for this kind of datasheet figure."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real-part mutation matrix against a cached corpus datasheet."
    )
    parser.add_argument("--entry", required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--export-oracle", action="store_true")
    parser.add_argument(
        "--shard-index",
        type=int,
        default=0,
        help="0-based shard index when the mutation matrix is split across jobs",
    )
    parser.add_argument(
        "--num-shards",
        type=int,
        default=1,
        help="total shard count; each shard runs mutations whose index mods to --shard-index",
    )
    return parser


def _entry(entry_id: str) -> corpus.CorpusEntry:
    manifest = corpus.load_manifest(_MANIFEST_PATH)
    entry = next((item for item in manifest.entries if item.id == entry_id), None)
    if entry is None:
        raise ValueError(f"unknown corpus entry: {entry_id}")
    return entry


def _attach_vision_reads(spec_path: Path, extraction_path: Path) -> partspec.PartSpec:
    tests_dir = _REPO_ROOT / "tests"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    fixture_helpers = import_module("vision_fixtures")
    attach_vision_reads = cast(
        Callable[[partspec.PartSpec, Path, Path], partspec.PartSpec],
        fixture_helpers.attach_vision_reads,
    )

    spec = partspec.load_part_spec(spec_path)
    attach_vision_reads(spec, spec_path, extraction_path)
    return partspec.load_part_spec(spec_path)


def _review_pages(
    spec: partspec.PartSpec,
    extraction: datasheet.DatasheetExtraction,
    extraction_dir: Path,
    spec_dir: Path,
) -> None:
    pages = {
        reading.page
        for _, reading, _ in partspec._all_readings(spec)  # pyright: ignore[reportPrivateUsage]
        if reading.page is not None
    }
    pages.add(spec.pin_table.page)
    if spec.pinout is not None:
        pages.add(spec.pinout.page)
    for page_number in sorted(pages):
        page = next(item for item in extraction.pages if item.page == page_number)
        image_path = extraction_dir / page.png_path
        record = advisory.build_review_record(
            image_path,
            model="corpus-fixture-review",
            checklist="datasheet",
            impression=_IMPRESSION,
            findings=[],
            summary="Hash-bound datasheet fixture review.",
        )
        target = spec_dir / "evidence" / f"page-{page_number:03d}.advisory.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _prepare_baseline(
    entry: corpus.CorpusEntry,
    pdf_path: Path,
    work_dir: Path,
    fixture_dir: Path = _FIXTURE_DIR,
) -> tuple[
    partspec.PartSpec,
    Path,
    Path,
    Path,
    Path,
    partspec.PartSpecReport,
]:
    shutil.copyfile(fixture_dir / "symbol.kicad_sym", work_dir / "symbol.kicad_sym")
    footprint_path = work_dir / "footprint.kicad_mod"
    shutil.copyfile(fixture_dir / "footprint.kicad_mod", footprint_path)
    spec = partspec.load_part_spec(fixture_dir / "part.spec.json")
    spec.datasheet = spec.datasheet.model_copy(
        update={
            "path": str(pdf_path),
            "sha256": entry.datasheet.sha256,
            "revision": entry.datasheet.revision,
            "extraction_path": str(work_dir / "extraction" / "extraction.json"),
        }
    )
    spec.authoring = None
    spec_path = work_dir / "part.spec.json"
    spec_path.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    extraction_dir = work_dir / "extraction"
    pages = {
        reading.page
        for _, reading, _ in partspec._all_readings(spec)  # pyright: ignore[reportPrivateUsage]
        if reading.page is not None
    }
    pages.add(spec.pin_table.page)
    if spec.pinout is not None:
        pages.add(spec.pinout.page)
    extraction = datasheet.extract_datasheet(
        pdf_path,
        extraction_dir,
        pages=sorted(pages),
    )
    extraction_path = extraction_dir / "extraction.json"
    spec = _attach_vision_reads(spec_path, extraction_path)
    _review_pages(spec, extraction, extraction_dir, work_dir)
    spec_path.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    spec = partspec.load_part_spec(spec_path)

    report = partspec.check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    spec_check_path = work_dir / "part.spec.check.json"
    spec_check_path.write_text(
        report.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    generated = model3d.generate_model(spec, footprint_path, work_dir / "models")
    return spec, spec_path, spec_check_path, footprint_path, generated.step_path, report


def _build_matrix(
    entry_id: str,
    cache_dir: Path,
    out_path: Path,
    *,
    export_oracle: bool,
    shard_index: int = 0,
    num_shards: int = 1,
) -> bool:
    entry = _entry(entry_id)
    pdf_path = cache_dir.resolve() / f"{entry_id}.pdf"
    if not pdf_path.is_file():
        if os.environ.get("CIRCUIT_REQUIRE_CORPUS_PDFS") == "1":
            raise FileNotFoundError(f"required corpus PDF is missing: {pdf_path}")
        raise FileNotFoundError(f"corpus PDF is missing: {pdf_path}")
    if corpus.sha256(pdf_path) != entry.datasheet.sha256:
        raise ValueError(f"cached corpus PDF SHA-256 mismatch for {entry_id}")

    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_dir = _FIXTURE_ROOT / entry.id
    if not fixture_dir.is_dir():
        raise FileNotFoundError(f"corpus part fixture is missing: {fixture_dir}")
    with tempfile.TemporaryDirectory(
        prefix=f"{out_path.stem}-work-",
        dir=out_path.parent,
    ) as temporary:
        work_dir = Path(temporary)
        project_pdf_path = work_dir / "datasheets" / f"{entry.id}.pdf"
        project_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf_path, project_pdf_path)
        rules_dir = work_dir / "library" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            fixture_dir / "rules" / f"{entry.id}.json",
            rules_dir / f"{entry.id}.json",
        )
        spec, spec_path, spec_check_path, footprint_path, model_path, _part_spec_report = (
            _prepare_baseline(
                entry,
                project_pdf_path,
                work_dir,
                fixture_dir,
            )
        )

        fixture = library_mutation_fixture(
            spec_path=spec_path,
            spec_check_path=spec_check_path,
            symbol_lib=work_dir / "symbol.kicad_sym",
            symbol_name=spec.mpn,
            footprint_path=footprint_path,
            model_path=model_path,
            work_dir=work_dir / "mutation-work",
            run_export_oracle=export_oracle,
            seed=62130,
            rules_profile=entry.id,
            rules_dir=rules_dir,
            unexercised_codes=frozenset({"authoring_missing", "vision_compare_missing"}),
        )
        matrix = run_mutations(fixture, shard_index=shard_index, num_shards=num_shards)
        out_path.write_text(
            json.dumps(
                {
                    "artifact_kind": "circuit_real_mutation_matrix",
                    "entry": entry.id,
                    "shard": {"index": shard_index, "count": num_shards},
                    "pdf_sha256": entry.datasheet.sha256,
                    "synthetic_evidence": ["vision_reads", "advisory_reviews"],
                    "synthetic_evidence_counted": False,
                    "unexercised_evidence": ["blind_authoring", "vision_compare"],
                    "matrix": matrix.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return matrix.passed


def main() -> int:
    args = _parser().parse_args()
    try:
        passed = _build_matrix(
            args.entry,
            args.cache,
            args.out,
            export_oracle=args.export_oracle,
            shard_index=args.shard_index,
            num_shards=args.num_shards,
        )
    except Exception as error:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "artifact_kind": "circuit_real_mutation_matrix_failure",
                    "entry": args.entry,
                    "error": f"{type(error).__name__}: {error}",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"real-part mutation matrix failed: {error}", file=sys.stderr)
        return 1
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
