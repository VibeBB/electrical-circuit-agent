import hashlib
from pathlib import Path

from circuit.brief import DesignBrief
from circuit.libraries import LibraryRoots, check_libraries, symbol_pins
from circuit.libverify import (
    LibraryVerification,
    VerifiedFootprint,
    VerifiedSymbol,
)

SYMBOLS = """\
(kicad_symbol_lib
  (version 20241209)
  (symbol "R"
    (symbol "R_0_1"
      (pin passive line (number "1") (name "~"))
      (pin passive line (number "2") (name "~"))))
  (symbol "LED"
    (symbol "LED_0_1"
      (pin passive line (number "1") (name "K"))
      (pin passive line (number "2") (name "A"))))
  (symbol "LED_ALT" (extends "LED"))
)
"""


def _brief() -> DesignBrief:
    return DesignBrief.model_validate(
        {
            "name": "test",
            "parts": [
                {"reference": "R1", "lib_id": "Device:R", "footprint": "Device:X"},
                {"reference": "D1", "lib_id": "Device:LED", "footprint": "Device:Y"},
            ],
            "nets": [
                {"name": "N1", "pins": ["R1.1", "D1.1"]},
                {"name": "N2", "pins": ["R1.2", "D1.2"]},
            ],
            "board": {"width_mm": 10, "height_mm": 10},
        }
    )


def _roots(tmp_path: Path) -> LibraryRoots:
    symbols = tmp_path / "symbols"
    footprints = tmp_path / "footprints"
    (footprints / "Device.pretty").mkdir(parents=True)
    (footprints / "Device.pretty" / "X.kicad_mod").write_text("(footprint X)", encoding="utf-8")
    (footprints / "Device.pretty" / "Y.kicad_mod").write_text("(footprint Y)", encoding="utf-8")
    symbols.mkdir()
    (symbols / "Device.kicad_sym").write_text(SYMBOLS, encoding="utf-8")
    return LibraryRoots(symbol_dirs=[symbols], footprint_dirs=[footprints])


def _empty_roots(tmp_path: Path) -> LibraryRoots:
    symbols = tmp_path / "symbols"
    footprints = tmp_path / "footprints"
    symbols.mkdir(parents=True)
    footprints.mkdir(parents=True)
    return LibraryRoots(symbol_dirs=[symbols], footprint_dirs=[footprints])


def test_library_resolution_pass_and_extends(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    result = check_libraries(_brief(), brief_path=brief_path, roots=roots)
    assert result.verdict == "pass"
    assert result.symbols["Device:LED"].pins == ["1", "2"]
    assert symbol_pins(roots.symbol_dirs[0] / "Device.kicad_sym", "LED_ALT") == ["1", "2"]


def test_missing_symbol_library_and_footprint(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    value = _brief().model_dump(mode="json")
    value.update(
        {
            "parts": [
                {"reference": "R1", "lib_id": "Missing:R", "footprint": "Missing:X"},
                {"reference": "D1", "lib_id": "Device:LED", "footprint": "Device:Y"},
            ]
        }
    )
    brief = DesignBrief.model_validate(value)
    path = tmp_path / "brief.json"
    path.write_text("{}", encoding="utf-8")
    result = check_libraries(brief, brief_path=path, roots=roots)
    assert result.verdict == "fail"
    assert result.missing_symbol_libraries == ["Missing"]
    assert result.missing_footprint_libraries == ["Missing"]


def test_missing_symbol_footprint_and_pin(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    value = _brief().model_dump(mode="json")
    value.update(
        {
            "parts": [
                {"reference": "R1", "lib_id": "Device:R", "footprint": "Device:Missing"},
                {"reference": "D1", "lib_id": "Device:LED", "footprint": "Device:Y"},
            ],
            "nets": [{"name": "N1", "pins": ["R1.3", "D1.1"]}],
        }
    )
    brief = DesignBrief.model_validate(value)
    path = tmp_path / "brief.json"
    path.write_text("{}", encoding="utf-8")
    result = check_libraries(brief, brief_path=path, roots=roots)
    assert result.verdict == "fail"
    assert result.missing_footprints == ["Device:Missing"]
    assert result.missing_pins == {"R1": ["3"]}


def test_malformed_symbol_fails_closed(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    (roots.symbol_dirs[0] / "Device.kicad_sym").write_text("(broken", encoding="utf-8")
    path = tmp_path / "brief.json"
    path.write_text("{}", encoding="utf-8")
    result = check_libraries(_brief(), brief_path=path, roots=roots)
    assert result.verdict == "fail"
    assert any("could not parse symbol library" in reason for reason in result.reasons)


def test_search_order_prefers_first_root(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    footprints = tmp_path / "footprints" / "Device.pretty"
    footprints.mkdir(parents=True)
    for name in ("X", "Y"):
        (footprints / f"{name}.kicad_mod").write_text(f"(footprint {name})", encoding="utf-8")
    first_lib = first / "Device.kicad_sym"
    second_lib = second / "Device.kicad_sym"
    first_lib.write_text(SYMBOLS.replace('(number "2")', '(number "9")'), encoding="utf-8")
    second_lib.write_text(SYMBOLS, encoding="utf-8")
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    assert symbol_pins(first_lib, "R") == ["1", "9"]
    result = check_libraries(
        _brief(),
        brief_path=brief_path,
        roots=LibraryRoots(
            symbol_dirs=[first, second],
            footprint_dirs=[tmp_path / "footprints"],
        ),
    )
    assert result.symbols["Device:R"].pins == ["1", "9"]


def _write_project_library(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    library = tmp_path / "library"
    library.mkdir()
    symbol_path = library / "Device.kicad_sym"
    symbol_path.write_text(SYMBOLS, encoding="utf-8")
    footprint_dir = library / "Device.pretty"
    footprint_dir.mkdir()
    footprint_paths: dict[str, Path] = {}
    for name in ("X", "Y"):
        footprint_path = footprint_dir / f"{name}.kicad_mod"
        footprint_path.write_text(
            f'(footprint "{name}" (layer "F.Cu") '
            '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))',
            encoding="utf-8",
        )
        footprint_paths[name] = footprint_path
    return symbol_path, footprint_paths


def _write_project_verification(
    library: Path,
    symbol_path: Path,
    symbol_name: str,
    footprint_path: Path,
) -> None:
    verification_dir = library / "verification"
    verification_dir.mkdir(exist_ok=True)
    report = LibraryVerification(
        artifact_kind="circuit_library_verification",
        verdict="pass",
        part_spec_sha256="a" * 64,
        symbol=VerifiedSymbol(
            lib_path=symbol_path,
            name=symbol_name,
            sha256=hashlib.sha256(symbol_path.read_bytes()).hexdigest(),
        ),
        footprint=VerifiedFootprint(
            path=footprint_path,
            name=footprint_path.stem,
            sha256=hashlib.sha256(footprint_path.read_bytes()).hexdigest(),
        ),
        models=[],
        findings=[],
    )
    (verification_dir / f"{symbol_name}.verification.json").write_text(
        report.model_dump_json(),
        encoding="utf-8",
    )


def test_project_library_precedes_default_roots_and_requires_verification(
    tmp_path: Path,
) -> None:
    roots = _empty_roots(tmp_path / "defaults")
    symbol_path, footprint_paths = _write_project_library(tmp_path)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")

    unverified = check_libraries(_brief(), brief_path=brief_path, roots=roots)
    assert unverified.verdict == "fail"
    assert "unverified project library part: Device:R" in unverified.reasons
    assert "unverified project library part: Device:LED" in unverified.reasons
    assert unverified.symbol_dirs[0] == tmp_path / "library"

    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "R",
        footprint_paths["X"],
    )
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "LED",
        footprint_paths["Y"],
    )
    verified = check_libraries(_brief(), brief_path=brief_path, roots=roots)
    assert verified.verdict == "pass"
    assert verified.symbols["Device:R"].library_path == symbol_path
    assert verified.footprints["Device:X"] == footprint_paths["X"]


def test_project_nickname_conflicts_and_stale_verification_fail(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path / "defaults")
    symbol_path, footprint_paths = _write_project_library(tmp_path)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "R",
        footprint_paths["X"],
    )
    _write_project_verification(
        tmp_path / "library",
        symbol_path,
        "LED",
        footprint_paths["Y"],
    )

    (footprint_paths["X"]).write_text(
        '(footprint "X" (layer "F.Cu") (pad "1" smd rect (at 0.1 0) (size 1 1) (layers "F.Cu")))',
        encoding="utf-8",
    )
    result = check_libraries(_brief(), brief_path=brief_path, roots=roots)
    assert result.verdict == "fail"
    assert "unverified project library part: Device:R" in result.reasons

    conflict = check_libraries(_brief(), brief_path=brief_path, roots=roots)
    assert "library_nickname_conflict" in conflict.reasons
