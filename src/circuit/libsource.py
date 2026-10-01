"""Library source attribution, safe imports, and deterministic provenance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import kicad_cli, sexpr
from .libitems import LibItemError, parse_footprint, parse_symbol

SourceOrigin = Literal[
    "manufacturer", "kicad_official", "cern", "third_party", "generated", "derived"
]
LibraryArtifact = Literal["symbol", "footprint", "model3d"]
Redistribution = Literal["allowed", "project_only", "unknown"]
_MAX_ZIP_ENTRIES = 1000
_MAX_ZIP_BYTES = 200 * 1024 * 1024
_SUPPORTED_SUFFIXES = {
    ".kicad_sym",
    ".kicad_mod",
    ".step",
    ".stp",
    ".stpz",
    ".wrl",
    ".wrz",
    ".stl",
}


class LibrarySourceError(RuntimeError):
    """Raised when a library source cannot be safely validated or imported."""


class LicenseInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spdx: str | None = None
    license_ref: str | None = None
    terms_url: str | None = None
    attribution: str = Field(min_length=1)
    redistribution: Redistribution

    @model_validator(mode="after")
    def require_license_identifier(self) -> LicenseInfo:
        if not self.spdx and not self.license_ref:
            raise ValueError("license requires spdx or license_ref")
        return self


class _SourceInfoBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: SourceOrigin
    vendor: str = Field(min_length=1)
    url: str | None = None
    retrieved_at: datetime | None = None
    license: LicenseInfo

    @model_validator(mode="after")
    def require_retrieval_time(self) -> _SourceInfoBase:
        if self.origin in ("manufacturer", "third_party") and self.retrieved_at is None:
            raise ValueError(f"{self.origin} sources require retrieved_at")
        return self


class SourceInfoInput(_SourceInfoBase):
    """Caller-provided source data before the imported original is hashed."""


class SourceInfo(_SourceInfoBase):
    original_path: str
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProvenanceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact: LibraryArtifact
    name: str
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: SourceInfo
    derived_from: list[str] = Field(default_factory=list)
    transformations: list[str] = Field(default_factory=list)
    part_spec_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def _empty_provenance_entries() -> list[ProvenanceEntry]:
    return []


class LibraryProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_library_provenance"] = "circuit_library_provenance"
    entries: list[ProvenanceEntry] = Field(default_factory=_empty_provenance_entries)


class ImportFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class ImportReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_library_import"] = "circuit_library_import"
    library_dir: Path
    nickname: str
    source_original_path: str
    imported: list[ProvenanceEntry]
    findings: list[ImportFinding]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _assert_safe_destination(library_dir: Path) -> Path:
    from .libraries import default_roots

    destination = library_dir.resolve()
    roots = default_roots()
    protected = [*roots.symbol_dirs, *roots.footprint_dirs]
    protected.extend(root.parent for root in roots.symbol_dirs)
    repository_libraries = Path(__file__).resolve().parents[2] / "libraries"
    protected.append(repository_libraries)
    default_cern_root = Path(
        os.environ.get("CIRCUIT_CERN_LIBS", "/opt/circuit/libraries/cern-kicad-libs")
    ).resolve()
    if default_cern_root.parent.name == "libraries":
        protected.append(default_cern_root.parent)
    if any(_inside(destination, root.resolve()) for root in protected):
        raise LibrarySourceError(f"refusing protected library destination: {destination}")
    return destination


def assert_safe_destination(library_dir: Path) -> Path:
    return _assert_safe_destination(library_dir)


def _validate_origin_path(source_path: Path, origin: SourceOrigin) -> None:
    if origin not in ("kicad_official", "cern"):
        return
    from .libraries import default_roots

    roots = default_roots()
    if origin == "kicad_official":
        allowed = roots.symbol_dirs[0].parent
    else:
        if len(roots.symbol_dirs) < 2:
            raise LibrarySourceError("CERN library root is not configured")
        allowed = roots.symbol_dirs[1].parent
    if not _inside(source_path.resolve(), allowed.resolve()):
        raise LibrarySourceError(f"{origin} source is outside its allowed root: {source_path}")


def _resolved_import_source(source: SourceInfoInput) -> SourceInfoInput:
    if source.origin == "kicad_official":
        required = LicenseInfo(
            spdx="CC-BY-SA-4.0",
            terms_url="https://www.kicad.org/libraries/license/",
            attribution="KiCad Libraries Team",
            redistribution="allowed",
        )
        if source.license != required:
            raise LibrarySourceError("KiCad official imports require the official library license")
    elif source.origin == "cern":
        if source.license.spdx != "CERN-OHL-P-2.0" or source.license.license_ref is not None:
            raise LibrarySourceError("CERN imports require the CERN-OHL-P-2.0 license")
    return source


def load_library_provenance(library_dir: Path) -> LibraryProvenance:
    """Load provenance, returning an empty manifest when none exists."""

    path = library_dir / "provenance.json"
    if not path.is_file():
        return LibraryProvenance()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return LibraryProvenance.model_validate(value)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise LibrarySourceError(f"invalid provenance manifest {path}: {exc}") from exc


def _write_provenance(library_dir: Path, provenance: LibraryProvenance) -> None:
    path = library_dir / "provenance.json"
    provenance.entries.sort(key=lambda entry: (entry.artifact, entry.name))
    content = json.dumps(
        provenance.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".provenance-", suffix=".tmp", dir=library_dir
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content + "\n")
        temporary.chmod(0o644)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _safe_name(value: str, *, label: str) -> str:
    if (
        not value
        or value in (".", "..")
        or "/" in value
        or "\\" in value
        or re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None
    ):
        raise LibrarySourceError(f"unsafe {label}: {value!r}")
    return value


def _zip_member_name(raw_name: str) -> str:
    normalized = raw_name.replace("\\", "/")
    member = PurePosixPath(normalized)
    if (
        not normalized
        or member.is_absolute()
        or PureWindowsPath(raw_name).drive
        or any(part in ("..", "") for part in member.parts)
    ):
        raise LibrarySourceError(f"unsafe ZIP member path: {raw_name!r}")
    return member.as_posix()


def _extract_zip(archive_path: Path, destination: Path, members: list[str] | None) -> list[Path]:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ZIP_ENTRIES:
                raise LibrarySourceError("ZIP contains more than 1000 entries")
            total_size = sum(info.file_size for info in infos)
            if total_size > _MAX_ZIP_BYTES:
                raise LibrarySourceError("ZIP exceeds 200 MB uncompressed")
            names: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                name = _zip_member_name(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise LibrarySourceError(f"ZIP symlinks are not allowed: {name}")
                if info.flag_bits & 1:
                    raise LibrarySourceError(f"encrypted ZIP member is not supported: {name}")
                if name in names:
                    raise LibrarySourceError(f"duplicate ZIP member: {name}")
                names[name] = info
            selected_names = (
                {_zip_member_name(name) for name in members} if members is not None else set(names)
            )
            missing = selected_names - names.keys()
            if missing:
                raise LibrarySourceError(f"ZIP members not found: {', '.join(sorted(missing))}")
            if members is not None and len(selected_names) != len(members):
                raise LibrarySourceError("ZIP members contain duplicates")
            paths: list[Path] = []
            actual_total = 0
            for name in sorted(selected_names):
                info = names[name]
                if info.is_dir():
                    if members is not None:
                        raise LibrarySourceError(f"selected ZIP member is a directory: {name}")
                    continue
                target = destination.joinpath(*PurePosixPath(name).parts)
                if not _inside(target.resolve(), destination.resolve()):
                    raise LibrarySourceError(f"ZIP member escapes extraction directory: {name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        actual_total += len(chunk)
                        if actual_total > _MAX_ZIP_BYTES:
                            raise LibrarySourceError("ZIP exceeds 200 MB uncompressed")
                        output.write(chunk)
                paths.append(target)
            return paths
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, LibrarySourceError):
            raise
        raise LibrarySourceError(f"could not safely read ZIP {archive_path}: {exc}") from exc


def _copy_original(source_path: Path, library_dir: Path, sha256: str) -> Path:
    directory = library_dir / "sources" / sha256[:12]
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / source_path.name
    if destination.exists():
        if _sha256(destination) != sha256:
            raise LibrarySourceError(f"source hash-prefix collision at {destination}")
        destination.chmod(destination.stat().st_mode & ~0o222)
        return destination
    shutil.copyfile(source_path, destination)
    destination.chmod(0o444)
    return destination


def _symbol_nodes(root: list[sexpr.SExpr]) -> dict[str, list[sexpr.SExpr]]:
    return {
        node[1]: node
        for node in root[1:]
        if isinstance(node, list)
        and len(node) >= 2
        and node[0] == "symbol"
        and isinstance(node[1], str)
    }


def _symbol_parent(node: list[sexpr.SExpr]) -> str | None:
    for child in node[1:]:
        if (
            isinstance(child, list)
            and len(child) == 2
            and child[0] == "extends"
            and isinstance(child[1], str)
        ):
            return child[1]
    return None


def _required_symbol_names(root: list[sexpr.SExpr], requested: list[str]) -> list[str]:
    nodes = _symbol_nodes(root)
    ordered: list[str] = []
    visiting: set[str] = set()
    included: set[str] = set()

    def include(name: str) -> None:
        if name in included:
            return
        if name in visiting:
            raise LibrarySourceError(f"cyclic symbol inheritance: {name}")
        node = nodes.get(name)
        if node is None:
            raise LibrarySourceError(f"symbol not found: {name}")
        visiting.add(name)
        parent = _symbol_parent(node)
        if parent is not None:
            include(parent)
        visiting.remove(name)
        included.add(name)
        ordered.append(name)

    for name in requested:
        include(name)
    return ordered


def _merged_symbol_library(
    source_path: Path,
    target_path: Path,
    staged_path: Path,
    symbol_names: list[str] | None,
    replace: bool,
) -> tuple[Path, list[str]]:
    try:
        source_root = sexpr.parse_text(source_path.read_text(encoding="utf-8"))
    except (OSError, sexpr.SExprError) as exc:
        raise LibrarySourceError(f"invalid symbol library {source_path}: {exc}") from exc
    if not source_root or source_root[0] != "kicad_symbol_lib":
        raise LibrarySourceError(f"not a KiCad symbol library: {source_path}")
    source_nodes = _symbol_nodes(source_root)
    if symbol_names is None:
        if len(source_nodes) != 1:
            raise LibrarySourceError("symbol_names is required for multi-symbol libraries")
        requested = list(source_nodes)
    else:
        if not symbol_names:
            raise LibrarySourceError("symbol_names must not be empty")
        requested = list(dict.fromkeys(symbol_names))
    names = _required_symbol_names(source_root, requested)
    for name in names:
        try:
            parse_symbol(source_path, name)
        except LibItemError as exc:
            raise LibrarySourceError(str(exc)) from exc

    target_root: list[sexpr.SExpr]
    if target_path.is_file():
        try:
            target_root = sexpr.parse_text(target_path.read_text(encoding="utf-8"))
        except (OSError, sexpr.SExprError) as exc:
            raise LibrarySourceError(f"invalid destination symbol library: {exc}") from exc
        if not target_root or target_root[0] != "kicad_symbol_lib":
            raise LibrarySourceError(f"not a KiCad symbol library: {target_path}")
    else:
        target_root = [
            "kicad_symbol_lib",
            ["version", "20241209"],
            ["generator", "circuit_agent"],
        ]
    current_nodes = _symbol_nodes(target_root)
    collisions = set(names) & current_nodes.keys()
    if collisions and not replace:
        raise LibrarySourceError(f"symbol already exists: {', '.join(sorted(collisions))}")
    if collisions:
        target_root = [
            child
            for child in target_root
            if not (
                isinstance(child, list)
                and len(child) >= 2
                and child[0] == "symbol"
                and child[1] in collisions
            )
        ]
    for name in names:
        target_root.append(source_nodes[name])
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_text(sexpr.serialize(target_root) + "\n", encoding="utf-8")
    return staged_path, names


def _validate_symbol_library(path: Path, work_dir: Path) -> None:
    output = work_dir / "sym-upgrade"
    try:
        result = kicad_cli.run(["sym", "upgrade", str(path), "--output", str(output)])
    except kicad_cli.KicadCliError as exc:
        raise LibrarySourceError(f"kicad-cli symbol validation failed: {exc}") from exc
    if result.returncode:
        raise LibrarySourceError(
            f"kicad-cli symbol validation failed: {result.stderr.strip() or result.stdout.strip()}"
        )


def _validate_footprint_library(path: Path, work_dir: Path) -> None:
    output = work_dir / "fp-upgrade"
    try:
        result = kicad_cli.run(["fp", "upgrade", str(path), "--output", str(output)])
    except kicad_cli.KicadCliError as exc:
        raise LibrarySourceError(f"kicad-cli footprint validation failed: {exc}") from exc
    if result.returncode:
        raise LibrarySourceError(
            "kicad-cli footprint validation failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _validate_model(path: Path) -> None:
    suffix = path.suffix.casefold()
    with path.open("rb") as handle:
        prefix = handle.read(128)
    if suffix in (".stp", ".step") and not prefix.lstrip().startswith(b"ISO-10303-21"):
        raise LibrarySourceError(f"invalid STEP file magic: {path.name}")
    if suffix in (".stpz", ".wrz") and not prefix.startswith(b"\x1f\x8b"):
        raise LibrarySourceError(f"invalid gzip model magic: {path.name}")
    if suffix == ".wrl" and not prefix.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#VRML"):
        raise LibrarySourceError(f"invalid VRML file magic: {path.name}")


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}-", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(0o644)
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def import_library_item(
    source_path: Path,
    library_dir: Path,
    nickname: str,
    *,
    source: SourceInfoInput,
    symbol_names: list[str] | None = None,
    members: list[str] | None = None,
    replace: bool = False,
) -> ImportReport:
    """Safely import a KiCad library item and record its source provenance."""

    destination = _assert_safe_destination(library_dir)
    nickname = _safe_name(nickname, label="library nickname")
    original = source_path.resolve(strict=True)
    _validate_origin_path(original, source.origin)
    _resolved_import_source(source)
    if not original.is_file():
        raise LibrarySourceError(f"source is not a regular file: {original}")
    is_zip = zipfile.is_zipfile(original)
    if members is not None and not is_zip:
        raise LibrarySourceError("members can only be selected from a ZIP source")
    original_sha256 = _sha256(original)
    original_copy = _copy_original(original, destination, original_sha256)
    relative_original = original_copy.relative_to(destination).as_posix()
    source_info = SourceInfo(
        **source.model_dump(),
        original_path=relative_original,
        original_sha256=original_sha256,
    )
    imported: list[ProvenanceEntry] = []
    findings: list[ImportFinding] = []
    with tempfile.TemporaryDirectory(prefix="circuit-library-import-") as temporary_name:
        work_dir = Path(temporary_name)
        if is_zip:
            input_paths = _extract_zip(original, work_dir / "unpacked", members)
        else:
            input_paths = [original]
        input_paths = [
            path for path in input_paths if path.suffix.casefold() in _SUPPORTED_SUFFIXES
        ]
        if not input_paths:
            raise LibrarySourceError("source contains no supported library items")

        candidate_targets: dict[Path, Path] = {}
        symbol_inputs = [path for path in input_paths if path.suffix.casefold() == ".kicad_sym"]
        if len(symbol_inputs) > 1:
            raise LibrarySourceError("ZIP imports may contain only one symbol library")
        if symbol_inputs:
            target = destination / f"{nickname}.kicad_sym"
            if target.exists() and not target.is_file():
                raise LibrarySourceError(f"symbol library target is not a file: {target}")
            staged, names = _merged_symbol_library(
                symbol_inputs[0],
                target,
                work_dir / f"{nickname}.kicad_sym",
                symbol_names,
                replace,
            )
            _validate_symbol_library(staged, work_dir)
            candidate_targets[target] = staged
            for name in names:
                imported.append(
                    ProvenanceEntry(
                        artifact="symbol",
                        name=name,
                        path=target.relative_to(destination).as_posix(),
                        sha256=_sha256(staged),
                        source=source_info,
                    )
                )

        footprint_inputs = [path for path in input_paths if path.suffix.casefold() == ".kicad_mod"]
        if footprint_inputs:
            target_dir = destination / f"{nickname}.pretty"
            staged_dir = work_dir / f"{nickname}.pretty"
            if target_dir.exists():
                if not target_dir.is_dir():
                    raise LibrarySourceError(
                        f"footprint library target is not a directory: {target_dir}"
                    )
                for existing in target_dir.rglob("*"):
                    if existing.is_symlink():
                        raise LibrarySourceError(
                            f"symlinks are not allowed in footprint library: {existing}"
                        )
                shutil.copytree(target_dir, staged_dir)
            else:
                staged_dir.mkdir(parents=True)
            for footprint_path in footprint_inputs:
                try:
                    footprint = parse_footprint(footprint_path)
                except LibItemError as exc:
                    raise LibrarySourceError(str(exc)) from exc
                name = _safe_name(footprint.name, label="footprint name")
                staged = staged_dir / f"{name}.kicad_mod"
                target = target_dir / staged.name
                if target in candidate_targets:
                    raise LibrarySourceError(f"duplicate imported footprint name: {name}")
                if staged.exists() and not replace:
                    raise LibrarySourceError(f"footprint already exists: {name}")
                shutil.copyfile(footprint_path, staged)
                candidate_targets[target] = staged
                imported.append(
                    ProvenanceEntry(
                        artifact="footprint",
                        name=name,
                        path=target.relative_to(destination).as_posix(),
                        sha256=_sha256(staged),
                        source=source_info,
                    )
                )
            _validate_footprint_library(staged_dir, work_dir)

        model_inputs = [
            path
            for path in input_paths
            if path.suffix.casefold() in {".step", ".stp", ".stpz", ".wrl", ".wrz", ".stl"}
        ]
        for model_path in model_inputs:
            name = _safe_name(model_path.name, label="model filename")
            target = destination / f"{nickname}.3dshapes" / name
            if target in candidate_targets:
                raise LibrarySourceError(f"duplicate imported model filename: {name}")
            if target.exists() and not replace:
                raise LibrarySourceError(f"model already exists: {name}")
            if model_path.suffix.casefold() == ".stl":
                findings.append(
                    ImportFinding(
                        code="stl_not_linked",
                        message=f"{name} is retained in sources; STL is not linked to footprints",
                    )
                )
                continue
            _validate_model(model_path)
            staged = work_dir / name
            shutil.copyfile(model_path, staged)
            candidate_targets[target] = staged
            imported.append(
                ProvenanceEntry(
                    artifact="model3d",
                    name=name,
                    path=target.relative_to(destination).as_posix(),
                    sha256=_sha256(staged),
                    source=source_info,
                )
            )

        if not candidate_targets and not findings:
            raise LibrarySourceError("source contains no importable items")
        for target, staged in candidate_targets.items():
            _atomic_copy(staged, target)

    if imported:
        provenance = load_library_provenance(destination)
        for entry in imported:
            provenance.entries = [
                old
                for old in provenance.entries
                if not (old.artifact == entry.artifact and old.name == entry.name)
            ]
            provenance.entries.append(entry)
        _write_provenance(destination, provenance)
    return ImportReport(
        library_dir=destination,
        nickname=nickname,
        source_original_path=relative_original,
        imported=imported,
        findings=findings,
    )


def record_library_item(
    library_dir: Path,
    artifact_path: Path,
    *,
    artifact: LibraryArtifact,
    name: str,
    source: SourceInfo | None,
    transformation: str,
    part_spec_sha256: str | None = None,
    derived_from: list[str] | None = None,
) -> ProvenanceEntry:
    """Register a generated or derived library artifact, or update its history."""

    destination = _assert_safe_destination(library_dir)
    path = artifact_path.resolve(strict=True)
    if not _inside(path, destination) or not path.is_file():
        raise LibrarySourceError("artifact_path must be a file inside library_dir")
    if not transformation.strip():
        raise LibrarySourceError("transformation must not be empty")
    relative = path.relative_to(destination).as_posix()
    provenance = load_library_provenance(destination)
    existing = next((entry for entry in provenance.entries if entry.path == relative), None)
    if existing is not None:
        if existing.artifact != artifact or existing.name != name:
            raise LibrarySourceError("artifact path is already recorded with a different identity")
        if source is not None and source.origin != existing.source.origin:
            raise LibrarySourceError("source origin cannot change for an existing artifact")
        existing.sha256 = _sha256(path)
        existing.transformations.append(transformation)
        if part_spec_sha256 is not None:
            existing.part_spec_sha256 = part_spec_sha256
        provenance_entry = existing
    else:
        if source is None or source.origin not in ("generated", "derived"):
            raise LibrarySourceError("new records require a generated or derived source")
        provenance_entry = ProvenanceEntry(
            artifact=artifact,
            name=name,
            path=relative,
            sha256=_sha256(path),
            source=source,
            derived_from=derived_from or [],
            transformations=[transformation],
            part_spec_sha256=part_spec_sha256,
        )
        provenance.entries.append(provenance_entry)
    _write_provenance(destination, provenance)
    return provenance_entry
