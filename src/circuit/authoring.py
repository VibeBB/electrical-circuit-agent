"""Blind, hash-sealed dual authoring for datasheet-derived PartSpecs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from . import advisory, visionread
from . import pinout as pinout_oracle
from .partspec import PartSpec, Reading, load_part_spec


class AuthoringError(ValueError):
    """Raised when authoring inputs or sealed records are not trustworthy."""


class AuthoringIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["error", "warning"]
    message: str


class AuthoringDisagreement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pointer: str
    a: Any
    b: Any


class AuthoringComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_part_authoring_comparison"]
    run_dir: str
    sealed: dict[str, str]
    models: dict[str, str]
    profiles: dict[str, str]
    impressions: dict[str, str]
    rasterizers: dict[str, list[str]]
    agreed: list[str]
    disagreements: list[AuthoringDisagreement]
    model_diversity: Literal["distinct", "same", "unknown"]
    issues: list[AuthoringIssue]


_LANES = ("a", "b")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuthoringError(f"authoring commits are unreadable: {exc}") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuthoringError(f"invalid authoring commit line {line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise AuthoringError(f"invalid authoring commit line {line_number}")
        records.append(cast(dict[str, Any], value))
    return records


def _read_refs(spec: PartSpec) -> list[str]:
    refs = [
        reading.vision_read
        for _, reading, _ in _part_spec_readings(spec)
        if reading.vision_read is not None
    ]
    refs.extend(
        reference
        for reference in (
            spec.pin_table.vision_read,
            spec.orderable_vision_read,
            spec.pinout.labels_vision_read if spec.pinout is not None else None,
        )
        if reference is not None
    )
    return sorted(set(refs))


def _part_spec_readings(spec: PartSpec) -> list[tuple[str, Reading, Any]]:
    values: list[tuple[str, Reading, Any]] = []
    package = spec.package
    for name in (
        "pitch",
        "body_length",
        "body_width",
        "height",
        "standoff",
        "lead_span",
        "lead_length",
        "lead_width",
    ):
        dimension = getattr(package, name)
        if dimension is not None:
            values.append((f"package.{name}", dimension.reading, dimension))
    if package.exposed_pad is not None:
        for name in ("length", "width"):
            dimension = getattr(package.exposed_pad, name)
            values.append((f"package.exposed_pad.{name}", dimension.reading, dimension))
    if spec.land_pattern is not None:
        values.extend(
            (f"land_pattern.dimensions.{name}", dimension.reading, dimension)
            for name, dimension in spec.land_pattern.dimensions.items()
        )
    values.append(("package.pin1_reading", package.pin1_reading, None))
    values.extend((f"pins[{index}]", pin.reading, None) for index, pin in enumerate(spec.pins))
    values.extend(
        (f"orderable[{index}]", variant.reading, None)
        for index, variant in enumerate(spec.orderable)
    )
    if spec.pinout is not None:
        values.append(("pinout.view_reading", spec.pinout.view_reading, None))
    return values


def _validate_read_refs(spec: PartSpec, spec_dir: Path, lane: str) -> list[str]:
    batches: set[str] = set()
    for reference in _read_refs(spec):
        try:
            batch, item, answers = visionread.load_vision_read(spec_dir, reference)
        except ValueError as exc:
            raise AuthoringError(f"invalid vision_read {reference!r}: {exc}") from exc
        if batch.lane != lane:
            raise AuthoringError(
                f"cross-lane vision read {reference!r}: batch lane {batch.lane!r} != {lane!r}"
            )
        if not answers.control_passed:
            raise AuthoringError(f"vision batch {batch.batch_id} control read did not pass")
        if answers.status.get(item.read_id) != "ok":
            raise AuthoringError(f"vision read {item.read_id} is not parseable")
        impression = answers.impressions.get(item.read_id)
        try:
            if not isinstance(impression, str):
                raise ValueError("impression is missing")
            advisory.impression_is_prose(impression)
        except ValueError as exc:
            raise AuthoringError(
                f"vision impression for read {item.read_id} is missing or invalid: {exc}"
            ) from exc
        batches.add(batch.batch_id)
    return sorted(batches)


def commit_lane(
    run_dir: Path,
    part_spec_path: Path,
    lane: str | None = None,
    profile: str | None = None,
    model: str | None = None,
    *,
    impression: str,
) -> dict[str, Any]:
    env_lane = os.environ.get("CIRCUIT_AUTHORING_LANE", "")
    selected_lane = env_lane if lane is None else lane
    if selected_lane not in _LANES or selected_lane != env_lane:
        raise AuthoringError(
            "CIRCUIT_AUTHORING_LANE must be 'a' or 'b' and match the requested lane"
        )
    try:
        advisory.impression_is_prose(impression)
    except ValueError as exc:
        raise AuthoringError(f"author impression is missing or invalid: {exc}") from exc

    run_root = run_dir.resolve()
    spec_path = part_spec_path.resolve()
    lane_dir = (run_root / selected_lane).resolve()
    if spec_path.parent != lane_dir:
        raise AuthoringError(f"PartSpec must be directly inside lane {selected_lane!r}")
    if not spec_path.is_file():
        raise AuthoringError(f"PartSpec is missing: {spec_path}")
    try:
        spec = load_part_spec(spec_path)
        spec_bytes = spec_path.read_bytes()
    except (OSError, ValueError) as exc:
        raise AuthoringError(f"PartSpec is unreadable or invalid: {exc}") from exc

    commit_path = run_root / "commits.jsonl"
    records = _records(commit_path)
    if any(record.get("lane") == selected_lane for record in records):
        raise AuthoringError(f"lane {selected_lane!r} is already committed")
    sealed_path = run_root / "sealed" / f"{selected_lane}.json"
    if sealed_path.exists():
        raise AuthoringError(f"sealed PartSpec already exists for lane {selected_lane!r}")
    vision_batches = _validate_read_refs(spec, spec_path.parent, selected_lane)
    selected_profile = profile if profile is not None else os.environ.get("CIRCUIT_LLM_PROFILE", "")
    selected_model = model if model is not None else os.environ.get("CIRCUIT_LLM_MODEL", "unknown")
    if not selected_model:
        selected_model = "unknown"
    sealed_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sealed_path.open("xb") as stream:
            stream.write(spec_bytes)
    except OSError as exc:
        raise AuthoringError(f"could not seal lane {selected_lane!r}: {exc}") from exc
    record: dict[str, Any] = {
        "lane": selected_lane,
        "sha256": _sha256(spec_bytes),
        "committed_at": datetime.now(UTC).isoformat(),
        "profile": selected_profile,
        "model": selected_model,
        "vision_batches": vision_batches,
        "impression": impression,
    }
    try:
        with commit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        raise AuthoringError(f"could not append authoring commit: {exc}") from exc
    return record


def _pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _normalization_excluded(path: tuple[str, ...]) -> bool:
    if path in {
        ("datasheet", "path"),
        ("datasheet", "extraction_path"),
        ("authoring",),
        ("pin_table",),
        ("orderable_vision_read",),
        ("pinout", "page"),
        ("pinout", "bbox"),
        ("pinout", "vision_record"),
        ("pinout", "labels_vision_read"),
    }:
        return True
    return any(key in {"reading", "pin1_reading", "view_reading"} for key in path)


def normalize(spec: PartSpec) -> dict[str, Any]:
    """Flatten authored PartSpec values while excluding evidence references."""

    raw = spec.model_dump(mode="json")
    result: dict[str, Any] = {}

    def walk(value: object, path: tuple[str, ...]) -> None:
        if _normalization_excluded(path):
            return
        if isinstance(value, dict):
            for key, child in cast(dict[str, object], value).items():
                if key == "vision_read":
                    continue
                walk(child, (*path, str(key)))
            return
        if isinstance(value, list):
            values = cast(list[object], value)
            if path == ("pins",):
                for pin in values:
                    if not isinstance(pin, dict):
                        continue
                    pin_data = cast(dict[str, object], pin)
                    number = str(pin_data.get("number", ""))
                    for key, child in pin_data.items():
                        if key == "reading" or key == "vision_read":
                            continue
                        walk(child, (*path, _pointer_escape(number), str(key)))
                return
            if path == ("orderable",):
                for orderable in values:
                    if not isinstance(orderable, dict):
                        continue
                    orderable_data = cast(dict[str, object], orderable)
                    mpn = str(orderable_data.get("mpn", ""))
                    for key, child in orderable_data.items():
                        if key == "reading" or key == "vision_read":
                            continue
                        walk(child, (*path, _pointer_escape(mpn), str(key)))
                return
            for index, child in enumerate(values):
                walk(child, (*path, str(index)))
            return
        pointer = "/" + "/".join(_pointer_escape(part) for part in path)
        result[pointer] = value

    walk(raw, ())
    return dict(sorted(result.items()))


def _name_value_equal(pointer: str, left: Any, right: Any) -> bool:
    if pointer.endswith("/name") or "/labels_vision/" in pointer:
        return (
            isinstance(left, str)
            and isinstance(right, str)
            and pinout_oracle.names_equal(left, right)
        )
    return left == right


def _lane_commits(run_root: Path) -> dict[str, dict[str, Any]]:
    records = _records(run_root / "commits.jsonl")
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        lane = record.get("lane")
        if lane in result:
            raise AuthoringError(f"lane {lane!r} has more than one commit")
        if lane in _LANES:
            result[cast(str, lane)] = record
    if set(result) != set(_LANES):
        raise AuthoringError("both authoring lanes must commit before comparison")
    return result


def _observation_log(run_root: Path) -> Path | None:
    for parent in (run_root, *run_root.parents):
        candidate = parent / "observations" / "circuit" / "authoring-events.jsonl"
        if candidate.is_file():
            return candidate
    return None


def _observed_commit_hashes(path: Path) -> set[str]:
    hashes: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return hashes
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            record = cast(dict[str, object], value)
            digest = record.get("sha256")
            if isinstance(digest, str):
                hashes.add(digest)
    return hashes


def _sealed_spec(
    run_root: Path,
    lane: str,
    commit: dict[str, Any],
) -> tuple[PartSpec, str]:
    sealed_path = run_root / "sealed" / f"{lane}.json"
    try:
        content = sealed_path.read_bytes()
    except OSError as exc:
        raise AuthoringError(f"sealed lane {lane!r} is unreadable: {exc}") from exc
    digest = _sha256(content)
    if not _SHA256.fullmatch(str(commit.get("sha256", ""))) or digest != commit.get("sha256"):
        raise AuthoringError(f"sealed lane {lane!r} SHA-256 differs from commits.jsonl")
    try:
        spec = PartSpec.model_validate_json(content)
    except ValueError as exc:
        raise AuthoringError(f"sealed lane {lane!r} PartSpec is invalid: {exc}") from exc
    if lane not in _LANES:
        raise AuthoringError(f"invalid authoring lane {lane!r}")
    if not isinstance(commit.get("impression"), str):
        raise AuthoringError(f"lane {lane!r} author impression is missing")
    try:
        advisory.impression_is_prose(cast(str, commit["impression"]))
    except ValueError as exc:
        raise AuthoringError(f"lane {lane!r} author impression is invalid: {exc}") from exc
    return spec, digest


def compare_runs(run_dir: Path) -> AuthoringComparison:
    run_root = run_dir.resolve()
    commits = _lane_commits(run_root)
    lane_specs: dict[str, PartSpec] = {}
    sealed: dict[str, str] = {}
    profiles: dict[str, str] = {}
    models: dict[str, str] = {}
    impressions: dict[str, str] = {}
    rasterizers: dict[str, list[str]] = {}
    issues: list[AuthoringIssue] = []
    normalized: dict[str, dict[str, Any]] = {}

    for lane in _LANES:
        commit = commits[lane]
        spec, digest = _sealed_spec(run_root, lane, commit)
        lane_specs[lane] = spec
        sealed[lane] = digest
        profiles[lane] = str(commit.get("profile", ""))
        models[lane] = str(commit.get("model", "unknown"))
        impressions[lane] = cast(str, commit["impression"])
        normalized[lane] = normalize(spec)
        batches: list[visionread.VisionBatch] = []
        batch_ids: set[str] = set()
        for reference in _read_refs(spec):
            try:
                batch, item, answers = visionread.load_vision_read(run_root / lane, reference)
            except ValueError as exc:
                raise AuthoringError(
                    f"lane {lane!r} has invalid vision read {reference!r}: {exc}"
                ) from exc
            if batch.lane != lane:
                raise AuthoringError(f"lane {lane!r} references a batch from lane {batch.lane!r}")
            if not answers.control_passed or answers.status.get(item.read_id) != "ok":
                raise AuthoringError(f"lane {lane!r} has an unsuccessful vision read")
            impression = answers.impressions.get(item.read_id)
            try:
                if not isinstance(impression, str):
                    raise ValueError("impression is missing")
                advisory.impression_is_prose(impression)
            except ValueError as exc:
                raise AuthoringError(f"lane {lane!r} vision impression is invalid: {exc}") from exc
            batches.append(batch)
            batch_ids.add(batch.batch_id)
        recorded_batch_ids = commit.get("vision_batches")
        if not isinstance(recorded_batch_ids, list):
            raise AuthoringError(f"lane {lane!r} vision batch provenance differs from its commit")
        recorded_ids = cast(list[object], recorded_batch_ids)
        if not all(isinstance(batch_id, str) for batch_id in recorded_ids):
            raise AuthoringError(f"lane {lane!r} vision batch provenance differs from its commit")
        if sorted(cast(list[str], recorded_ids)) != sorted(batch_ids):
            raise AuthoringError(f"lane {lane!r} vision batch provenance differs from its commit")
        rasterizers[lane] = sorted({item.rasterizer for batch in batches for item in batch.items})
        expected_rasterizer = "pdftoppm" if lane == "a" else "pdfium"
        if rasterizers[lane] != [expected_rasterizer]:
            issues.append(
                AuthoringIssue(
                    code="authoring_lane_input_mismatch",
                    severity="error",
                    message=(
                        f"lane {lane} must use only {expected_rasterizer}; "
                        f"found {rasterizers[lane]}"
                    ),
                )
            )

    log_path = _observation_log(run_root)
    if log_path is not None:
        observed = _observed_commit_hashes(log_path)
        for lane, digest in sealed.items():
            if digest not in observed:
                issues.append(
                    AuthoringIssue(
                        code="authoring_commit_unobserved",
                        severity="error",
                        message=(
                            f"sealed lane {lane} commit {digest} is absent from authoring events"
                        ),
                    )
                )

    fields_a = normalized["a"]
    fields_b = normalized["b"]
    agreed: list[str] = []
    disagreements: list[AuthoringDisagreement] = []
    for pointer in sorted(fields_a.keys() | fields_b.keys()):
        a_value = fields_a.get(pointer)
        b_value = fields_b.get(pointer)
        if (
            pointer in fields_a
            and pointer in fields_b
            and _name_value_equal(pointer, a_value, b_value)
        ):
            agreed.append(pointer)
        else:
            disagreements.append(AuthoringDisagreement(pointer=pointer, a=a_value, b=b_value))
    if models["a"] == "unknown" or models["b"] == "unknown":
        model_diversity: Literal["distinct", "same", "unknown"] = "unknown"
    else:
        model_diversity = "distinct" if models["a"] != models["b"] else "same"
    return AuthoringComparison(
        artifact_kind="circuit_part_authoring_comparison",
        run_dir=str(run_root),
        sealed=sealed,
        models=models,
        profiles=profiles,
        impressions=impressions,
        rasterizers=rasterizers,
        agreed=agreed,
        disagreements=disagreements,
        model_diversity=model_diversity,
        issues=issues,
    )


def write_comparison(run_dir: Path, comparison: AuthoringComparison) -> Path:
    target = run_dir / "comparison.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = comparison.model_dump_json(indent=2)
    descriptor, temporary = tempfile.mkstemp(prefix=".comparison-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
        os.replace(temporary, target)
    except OSError as exc:
        with suppress(OSError):
            os.unlink(temporary)
        raise AuthoringError(f"could not write authoring comparison: {exc}") from exc
    return target
