from __future__ import annotations

import gzip
import shutil
import stat
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from circuit.libsource import (
    ImportReport,
    LibrarySourceError,
    LicenseInfo,
    SourceInfo,
    SourceInfoInput,
    SourceOrigin,
    import_library_item,
    load_library_provenance,
    record_library_item,
)

DATA = Path(__file__).parent / "data" / "library"
RETRIEVED = datetime(2026, 1, 1, tzinfo=UTC)


def _license(spdx: str = "MIT", attribution: str = "Fixture author") -> LicenseInfo:
    return LicenseInfo(
        spdx=spdx,
        attribution=attribution,
        redistribution="allowed",
    )


def _source(
    origin: SourceOrigin = "manufacturer",
    *,
    license_info: LicenseInfo | None = None,
) -> SourceInfoInput:
    return SourceInfoInput(
        origin=origin,
        vendor="Fixture vendor",
        url="https://example.invalid/item",
        retrieved_at=RETRIEVED if origin in ("manufacturer", "third_party") else None,
        license=license_info or _license(),
    )


def _fake_kicad_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake-kicad-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import shutil\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if os.environ.get('FAKE_KICAD_FAIL'):\n"
        "    sys.stderr.write('fixture parser failure')\n"
        "    raise SystemExit(2)\n"
        "args = sys.argv[1:]\n"
        "source = Path(args[2])\n"
        "output = Path(args[4])\n"
        "if output.exists():\n"
        "    sys.stderr.write('output path must not exist before upgrade')\n"
        "    raise SystemExit(3)\n"
        "output.mkdir(parents=True, exist_ok=True)\n"
        "if args[0] == 'fp':\n"
        "    shutil.copytree(source, output / source.name)\n"
        "else:\n"
        "    shutil.copy2(source, output / source.name)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("CIRCUIT_KICAD_CLI", str(script))
    monkeypatch.delenv("FAKE_KICAD_FAIL", raising=False)
    return script


def _footprint_source(tmp_path: Path) -> Path:
    source = tmp_path / "Modern.kicad_mod"
    shutil.copyfile(DATA / "modern.kicad_mod", source)
    return source


def _import(
    source_path: Path,
    library_dir: Path,
    *,
    source: SourceInfoInput | None = None,
    members: list[str] | None = None,
    symbol_names: list[str] | None = None,
    replace: bool = False,
) -> ImportReport:
    return import_library_item(
        source_path,
        library_dir,
        "Fixture",
        source=source or _source(),
        members=members,
        symbol_names=symbol_names,
        replace=replace,
    )


def test_license_and_source_models_require_attribution_and_timestamp() -> None:
    with pytest.raises(ValidationError, match="spdx or license_ref"):
        LicenseInfo(attribution="Fixture", redistribution="allowed")
    with pytest.raises(ValidationError, match="require retrieved_at"):
        SourceInfoInput(
            origin="manufacturer",
            vendor="Fixture",
            license=_license(),
        )


def test_import_footprint_copies_original_and_writes_sorted_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_kicad_cli(tmp_path, monkeypatch)
    source_path = _footprint_source(tmp_path)
    original_bytes = source_path.read_bytes()
    library_dir = tmp_path / "project" / "library"

    report = _import(source_path, library_dir)

    assert report.imported[0].artifact == "footprint"
    assert report.imported[0].name == "Modern"
    assert (library_dir / "Fixture.pretty" / "Modern.kicad_mod").read_bytes() == original_bytes
    original = library_dir / report.source_original_path
    assert original.read_bytes() == original_bytes
    source_path.write_text("changed", encoding="utf-8")
    assert original.read_bytes() == original_bytes
    manifest_path = library_dir / "provenance.json"
    first_text = manifest_path.read_text(encoding="utf-8")
    provenance = load_library_provenance(library_dir)
    assert [entry.name for entry in provenance.entries] == ["Modern"]
    assert provenance.entries[0].source.original_path == report.source_original_path
    assert manifest_path.read_text(encoding="utf-8") == first_text


def test_import_parser_failure_leaves_only_immutable_source_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_kicad_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KICAD_FAIL", "1")
    source_path = _footprint_source(tmp_path)
    library_dir = tmp_path / "library"

    with pytest.raises(LibrarySourceError, match="fixture parser failure"):
        _import(source_path, library_dir)

    assert list((library_dir / "sources").rglob("Modern.kicad_mod"))
    assert not (library_dir / "Fixture.pretty").exists()
    assert not (library_dir / "provenance.json").exists()


def test_import_symbol_selection_includes_extends_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_kicad_cli(tmp_path, monkeypatch)
    source = tmp_path / "symbols.kicad_sym"
    shutil.copyfile(DATA / "symbols.kicad_sym", source)
    library_dir = tmp_path / "library"

    with pytest.raises(LibrarySourceError, match="symbol_names is required"):
        _import(source, library_dir)
    report = _import(source, library_dir, symbol_names=["Derived"])
    assert {entry.name for entry in report.imported} == {"Base", "Derived"}
    assert "Base" in (library_dir / "Fixture.kicad_sym").read_text(encoding="utf-8")
    with pytest.raises(LibrarySourceError, match="symbol already exists"):
        _import(source, library_dir, symbol_names=["Derived"])
    replaced = _import(source, library_dir, symbol_names=["Derived"], replace=True)
    assert {entry.name for entry in replaced.imported} == {"Base", "Derived"}


def test_zip_import_selects_members_without_extracting_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_kicad_cli(tmp_path, monkeypatch)
    source = tmp_path / "bundle.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("deep/modern.kicad_mod", (DATA / "modern.kicad_mod").read_bytes())
        archive.writestr("README.txt", "unselected")
    library_dir = tmp_path / "library"

    report = _import(
        source,
        library_dir,
        members=["deep/modern.kicad_mod"],
    )

    assert report.imported[0].name == "Modern"
    assert (library_dir / "Fixture.pretty" / "Modern.kicad_mod").is_file()


@pytest.mark.parametrize(
    "member",
    [
        "../outside.kicad_mod",
        "/absolute.kicad_mod",
        "C:\\absolute.kicad_mod",
    ],
)
def test_zip_rejects_traversal_and_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    _fake_kicad_cli(tmp_path, monkeypatch)
    source = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(member, (DATA / "modern.kicad_mod").read_bytes())
    library_dir = tmp_path / "library"

    with pytest.raises(LibrarySourceError, match="unsafe ZIP member path"):
        _import(source, library_dir)

    assert not (library_dir / "Fixture.pretty").exists()


def test_zip_rejects_symlinks_entry_limit_and_expansion_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_kicad_cli(tmp_path, monkeypatch)
    library_dir = tmp_path / "library"
    symlink_archive = tmp_path / "symlink.zip"
    symlink = zipfile.ZipInfo("link.kicad_mod")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(symlink_archive, "w") as archive:
        archive.writestr(symlink, "target")
    with pytest.raises(LibrarySourceError, match="symlinks"):
        _import(symlink_archive, library_dir)

    many_archive = tmp_path / "many.zip"
    with zipfile.ZipFile(many_archive, "w") as archive:
        for index in range(1001):
            archive.writestr(f"{index}.txt", "")
    with pytest.raises(LibrarySourceError, match="more than 1000"):
        _import(many_archive, library_dir)

    large_archive = tmp_path / "large.zip"
    with zipfile.ZipFile(large_archive, "w") as archive:
        archive.writestr("large.txt", b"x" * 128)
    monkeypatch.setattr("circuit.libsource._MAX_ZIP_BYTES", 64)
    with pytest.raises(LibrarySourceError, match="200 MB"):
        _import(large_archive, library_dir)


def test_import_forces_official_and_cern_licenses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_kicad_cli(tmp_path, monkeypatch)
    kicad_share = tmp_path / "kicad"
    cern_root = tmp_path / "cern"
    monkeypatch.setenv("CIRCUIT_KICAD_SHARE", str(kicad_share))
    monkeypatch.setenv("CIRCUIT_CERN_LIBS", str(cern_root))
    official_source = kicad_share / "footprints" / "Fixture.pretty" / "Modern.kicad_mod"
    official_source.parent.mkdir(parents=True)
    shutil.copyfile(DATA / "modern.kicad_mod", official_source)
    library_dir = tmp_path / "project" / "library"
    official_license = LicenseInfo(
        spdx="CC-BY-SA-4.0",
        terms_url="https://www.kicad.org/libraries/license/",
        attribution="KiCad Libraries Team",
        redistribution="allowed",
    )
    with pytest.raises(LibrarySourceError, match="official library license"):
        _import(
            official_source,
            library_dir,
            source=_source("kicad_official", license_info=_license()),
        )
    report = _import(
        official_source,
        library_dir,
        source=_source("kicad_official", license_info=official_license),
    )
    assert report.imported[0].source.license == official_license

    cern_source = cern_root / "PcbLib" / "Fixture.pretty" / "Modern.kicad_mod"
    cern_source.parent.mkdir(parents=True)
    shutil.copyfile(DATA / "modern.kicad_mod", cern_source)
    cern_license = _license("CERN-OHL-P-2.0", "CERN fixture")
    cern_report = _import(
        cern_source,
        tmp_path / "cern-project",
        source=_source("cern", license_info=cern_license),
    )
    assert cern_report.imported[0].source.license.spdx == "CERN-OHL-P-2.0"


def test_import_refuses_protected_destinations_and_out_of_root_official_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kicad_share = tmp_path / "kicad"
    cern_root = tmp_path / "cern"
    monkeypatch.setenv("CIRCUIT_KICAD_SHARE", str(kicad_share))
    monkeypatch.setenv("CIRCUIT_CERN_LIBS", str(cern_root))
    source = _footprint_source(tmp_path)
    with pytest.raises(LibrarySourceError, match="protected library destination"):
        _import(source, kicad_share / "not-a-library")

    official = _source(
        "kicad_official",
        license_info=LicenseInfo(
            spdx="CC-BY-SA-4.0",
            terms_url="https://www.kicad.org/libraries/license/",
            attribution="KiCad Libraries Team",
            redistribution="allowed",
        ),
    )
    with pytest.raises(LibrarySourceError, match="outside its allowed root"):
        _import(source, tmp_path / "outside", source=official)


@pytest.mark.parametrize(
    ("suffix", "content", "valid"),
    [
        (".step", b"ISO-10303-21;\n", True),
        (".step", b"not a STEP", False),
        (".stpz", gzip.compress(b"ISO-10303-21", mtime=0), True),
        (".stpz", b"not gzip", False),
        (".wrl", b"#VRML V2.0 utf8\n", True),
        (".wrl", b"not VRML", False),
    ],
)
def test_import_3d_model_magic(
    tmp_path: Path,
    suffix: str,
    content: bytes,
    valid: bool,
) -> None:
    source = tmp_path / f"model{suffix}"
    source.write_bytes(content)
    library_dir = tmp_path / "library"
    if valid:
        report = _import(source, library_dir, source=_source("generated"))
        assert report.imported[0].artifact == "model3d"
    else:
        with pytest.raises(LibrarySourceError, match="magic"):
            _import(source, library_dir, source=_source("generated"))


def test_stl_is_retained_but_not_linked(tmp_path: Path) -> None:
    source = tmp_path / "model.stl"
    source.write_bytes(b"not converted")
    library_dir = tmp_path / "library"

    report = _import(source, library_dir, source=_source("generated"))

    assert report.findings[0].code == "stl_not_linked"
    assert not (library_dir / "Fixture.3dshapes" / "model.stl").exists()
    assert (library_dir / report.source_original_path).read_bytes() == b"not converted"


def _generated_source(origin: Literal["generated", "derived"] = "generated") -> SourceInfo:
    return SourceInfo(
        origin=origin,
        vendor="Circuit Agent",
        license=_license("BSD-3-Clause", "Circuit Agent"),
        original_path="sources/generated.kicad_mod",
        original_sha256="a" * 64,
    )


def test_record_generated_item_and_append_transformation(tmp_path: Path) -> None:
    library_dir = tmp_path / "library"
    artifact = library_dir / "Fixture.pretty" / "Generated.kicad_mod"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("(footprint Generated)", encoding="utf-8")
    entry = record_library_item(
        library_dir,
        artifact,
        artifact="footprint",
        name="Generated",
        source=_generated_source(),
        transformation="generated from specification",
        part_spec_sha256="b" * 64,
        derived_from=["Fixture:Source"],
    )
    old_hash = entry.sha256
    artifact.write_text("(footprint Generated (attr smd))", encoding="utf-8")
    updated = record_library_item(
        library_dir,
        artifact,
        artifact="footprint",
        name="Generated",
        source=None,
        transformation="added SMD attribute",
    )

    assert updated.sha256 != old_hash
    assert updated.transformations == [
        "generated from specification",
        "added SMD attribute",
    ]
    assert updated.derived_from == ["Fixture:Source"]
    assert load_library_provenance(library_dir).entries[0].sha256 == updated.sha256


def test_record_library_item_rejects_invalid_updates(tmp_path: Path) -> None:
    library_dir = tmp_path / "library"
    artifact = library_dir / "item.kicad_mod"
    library_dir.mkdir()
    artifact.write_text("item", encoding="utf-8")
    with pytest.raises(LibrarySourceError, match="generated or derived"):
        record_library_item(
            library_dir,
            artifact,
            artifact="footprint",
            name="Item",
            source=None,
            transformation="created",
        )
    record_library_item(
        library_dir,
        artifact,
        artifact="footprint",
        name="Item",
        source=_generated_source(),
        transformation="created",
    )
    with pytest.raises(LibrarySourceError, match="origin cannot change"):
        record_library_item(
            library_dir,
            artifact,
            artifact="footprint",
            name="Item",
            source=_generated_source("derived"),
            transformation="changed source",
        )
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(LibrarySourceError, match="inside library_dir"):
        record_library_item(
            library_dir,
            outside,
            artifact="footprint",
            name="Outside",
            source=_generated_source(),
            transformation="created",
        )
