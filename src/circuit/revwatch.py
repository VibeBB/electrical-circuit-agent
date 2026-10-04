"""Datasheet revision polling and errata evidence binding."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import pdfplumber
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import confidential, humanrequest, partspec

MAX_REVISION_PDF_BYTES = 40 * 1024 * 1024
_REVISION_RE = re.compile(
    r"\b(?:data\s*sheet\s*)?rev(?:ision)?\.?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9._/-]*)",
    re.IGNORECASE,
)


class DatasheetRevisionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str | None = Field(default=None, min_length=1)
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str = Field(min_length=1)
    retrieved_at: datetime

    @field_validator("retrieved_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("revision snapshot time must include a timezone")
        return value.astimezone(UTC)


class RevisionInvalidationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_datasheet_revision_invalidation"] = (
        "circuit_datasheet_revision_invalidation"
    )
    mpn: str = Field(min_length=1)
    bound_revision: str = Field(min_length=1)
    bound_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_revision: str | None = None
    current_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_at: datetime
    request_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("checked_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("revision invalidation time must include a timezone")
        return value.astimezone(UTC)


class DatasheetRevisionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_datasheet_revision_check"] = "circuit_datasheet_revision_check"
    mpn: str
    bound_revision: str
    bound_pdf_sha256: str
    current_revision: str | None = None
    current_pdf_sha256: str | None = None
    changed: bool
    approval_invalidated: bool
    human_request_id: str | None = None
    human_request_json: str | None = None
    findings: list[partspec.SpecFinding]


RevisionFetcher = Callable[[str], DatasheetRevisionSnapshot]


def _validate_source_url(source_url: str) -> None:
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("datasheet source URL must be an HTTPS URL without credentials")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or 443,
                    type=socket.SOCK_STREAM,
                )
            }
        except OSError as exc:
            raise ValueError("datasheet source hostname could not be resolved") from exc
        if not addresses or any(not item.is_global for item in addresses):
            raise ValueError(
                "datasheet source URL must resolve only to public IP addresses"
            ) from None
    else:
        if not address.is_global:
            raise ValueError("datasheet source URL cannot target a private or reserved IP address")


class _PublicHTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _validate_source_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _pdf_revision(data: bytes) -> str | None:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages[:3])
    except Exception as exc:
        raise ValueError(f"current datasheet PDF could not be parsed: {exc}") from exc
    matches = [match.group(1).strip() for match in _REVISION_RE.finditer(text)]
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _fetch_response(source_url: str) -> tuple[bytes, str, str]:
    _validate_source_url(source_url)
    request = Request(source_url, headers={"User-Agent": "circuit-library-revision-watch/1"})
    opener = build_opener(_PublicHTTPSRedirectHandler())
    with opener.open(request, timeout=30) as response:
        data = response.read(MAX_REVISION_PDF_BYTES + 1)
        content_type = response.headers.get_content_type()
        resolved_url = response.geturl()
    if len(data) > MAX_REVISION_PDF_BYTES:
        raise ValueError("datasheet response exceeds the size limit")
    _validate_source_url(resolved_url)
    return data, content_type, resolved_url


def fetch_current_revision(source_url: str) -> DatasheetRevisionSnapshot:
    """Fetch and hash the manufacturer's current PDF over HTTPS."""
    data, content_type, resolved_url = _fetch_response(source_url)
    retrieved_at = datetime.now(UTC)
    if content_type == "application/json":
        raw: Any = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError("revision manifest must be a JSON object")
        payload = cast(dict[str, Any], raw)
        pdf_url = payload.get("pdf_url")
        if not isinstance(pdf_url, str) or not pdf_url:
            raise ValueError("revision manifest must include the current PDF URL")
        pdf_data, _, pdf_resolved_url = _fetch_response(pdf_url)
        if not pdf_data.startswith(b"%PDF-"):
            raise ValueError("revision manifest PDF URL did not return a PDF")
        pdf_sha256 = hashlib.sha256(pdf_data).hexdigest()
        claimed_sha256 = payload.get("pdf_sha256", payload.get("sha256"))
        if claimed_sha256 is not None and claimed_sha256 != pdf_sha256:
            raise ValueError("revision manifest PDF hash does not match the fetched PDF")
        revision = payload.get("revision")
        if revision is not None and not isinstance(revision, str):
            raise ValueError("revision manifest revision must be a string")
        return DatasheetRevisionSnapshot(
            revision=revision or _pdf_revision(pdf_data),
            pdf_sha256=pdf_sha256,
            source_url=pdf_resolved_url,
            retrieved_at=retrieved_at,
        )
    if not data.startswith(b"%PDF-"):
        raise ValueError("datasheet source did not return a PDF or revision JSON manifest")
    return DatasheetRevisionSnapshot(
        revision=_pdf_revision(data),
        pdf_sha256=hashlib.sha256(data).hexdigest(),
        source_url=resolved_url,
        retrieved_at=retrieved_at,
    )


def _normalized_revision(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"^(?:data\s*sheet\s*)?rev(?:ision)?\.?\s*", "", value.strip(), flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", normalized.casefold()) or None


def _mpn_key(mpn: str) -> str:
    return hashlib.sha256(mpn.casefold().encode("utf-8")).hexdigest()[:24]


def _invalidation_dir(library_dir: Path, mpn: str) -> Path:
    return library_dir / "datasheet-revision-watch" / _mpn_key(mpn)


def _invalidation_path(library_dir: Path, record: RevisionInvalidationRecord) -> Path:
    key = hashlib.sha256(
        (
            f"{record.mpn.casefold()}:{record.bound_pdf_sha256}:"
            f"{record.current_revision}:{record.current_pdf_sha256}"
        ).encode()
    ).hexdigest()[:24]
    return _invalidation_dir(library_dir, record.mpn) / f"{key}.json"


def _write_invalidation(library_dir: Path, record: RevisionInvalidationRecord) -> Path:
    path = _invalidation_path(library_dir, record)
    serialized = json.dumps(record.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("datasheet revision invalidation path is a symlink")
    if path.exists():
        existing = RevisionInvalidationRecord.model_validate_json(path.read_bytes())
        if (
            existing.mpn.casefold() != record.mpn.casefold()
            or existing.bound_pdf_sha256 != record.bound_pdf_sha256
            or existing.current_pdf_sha256 != record.current_pdf_sha256
        ):
            raise ValueError("datasheet revision invalidation record conflicts")
        if existing.request_id is not None and record.request_id is None:
            return path
        if (
            existing.request_id == record.request_id
            and existing.request_sha256 == record.request_sha256
        ):
            return path
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def _record_from_path(path: Path) -> RevisionInvalidationRecord:
    if path.is_symlink() or not path.is_file():
        raise ValueError("datasheet revision invalidation record is not a regular file")
    return RevisionInvalidationRecord.model_validate_json(path.read_bytes())


def approval_blocker(library_dir: Path, spec: partspec.PartSpec) -> str | None:
    directory = _invalidation_dir(library_dir, spec.mpn)
    if not directory.exists():
        return None
    try:
        for path in sorted(directory.glob("*.json")):
            record = _record_from_path(path)
            if (
                record.mpn.casefold() == spec.mpn.casefold()
                and record.bound_pdf_sha256 == spec.datasheet.sha256
            ):
                return "datasheet_revision_changed"
    except (OSError, ValueError):
        return "datasheet_revision_watch_state_invalid"
    return None


def _affect_pointer(affect: str) -> str:
    if affect.startswith("/"):
        return affect
    normalized = re.sub(r"\[(\d+)\]", r".\1", affect)
    return "/" + "/".join(
        token.replace("~", "~0").replace("/", "~1") for token in normalized.split(".") if token
    )


def _pointer_covers(pointer: str, covers: set[str]) -> bool:
    return any(pointer == item or pointer.startswith(item.rstrip("/") + "/") for item in covers)


def _errata_field_bound(
    spec: partspec.PartSpec,
    spec_path: Path | None,
    erratum: partspec.DatasheetErratum,
    affect: str,
) -> bool:
    if spec_path is None:
        return False
    errata_path = Path(erratum.path)
    if not errata_path.is_absolute():
        errata_path = spec_path.resolve().parent / errata_path
    try:
        if (
            errata_path.is_symlink()
            or not errata_path.is_file()
            or hashlib.sha256(errata_path.read_bytes()).hexdigest() != erratum.sha256
        ):
            return False
    except OSError:
        return False
    project_root = confidential.project_root_for(spec_path)
    affect_pointer = _affect_pointer(affect)
    readings = partspec._all_readings(spec)  # pyright: ignore[reportPrivateUsage]
    for field, reading, _ in readings:
        field_pointer = partspec._pointer_for_field(field)  # pyright: ignore[reportPrivateUsage]
        if not _pointer_covers(affect_pointer, {field_pointer}) and not _pointer_covers(
            field_pointer, {affect_pointer}
        ):
            continue
        request_id = reading.alternative_evidence
        if request_id is None:
            continue
        try:
            request_path = humanrequest.find_request_path(project_root, request_id)
            request = humanrequest.load_request(request_path)
        except (OSError, ValueError):
            continue
        details = request.details
        if (
            request.kind != "alternative_evidence"
            or request.subject.manufacturer.casefold() != spec.manufacturer.casefold()
            or request.subject.mpn.casefold() != spec.mpn.casefold()
            or not isinstance(details, humanrequest.AlternativeEvidenceDetails)
            or not _pointer_covers(affect_pointer, set(details.covers))
        ):
            continue
        matching_errata = False
        for evidence in details.files:
            evidence_path = Path(evidence.path)
            if not evidence_path.is_absolute():
                evidence_path = project_root / evidence_path
            try:
                same_path = evidence_path.resolve(strict=True) == errata_path.resolve(strict=True)
                valid_hash = (
                    hashlib.sha256(evidence_path.read_bytes()).hexdigest() == erratum.sha256
                )
            except OSError:
                continue
            if same_path and valid_hash and evidence.sha256 == erratum.sha256:
                matching_errata = True
                break
        if not matching_errata:
            continue
        try:
            responses = humanrequest.load_responses(project_root, request)
        except (OSError, ValueError):
            continue
        if any(
            response.valid
            and response.decision == "grant"
            and _pointer_covers(
                affect_pointer,
                {
                    item.strip()
                    for item in response.fields.get("covers", "").split(",")
                    if item.strip()
                },
            )
            for response in responses
        ):
            return True
    return False


def _errata_findings(spec: partspec.PartSpec, spec_path: Path | None) -> list[partspec.SpecFinding]:
    findings: list[partspec.SpecFinding] = []
    for erratum in spec.datasheet.errata:
        for affect in erratum.affects:
            if not _errata_field_bound(spec, spec_path, erratum, affect):
                findings.append(
                    partspec.SpecFinding(
                        code="errata_unbound",
                        severity="error",
                        field=affect,
                        message=(
                            f"erratum {erratum.revision} ({erratum.title}) affects this field "
                            "without a granted reading bound to the errata PDF"
                        ),
                    )
                )
    return findings


def _revision_request(
    spec: partspec.PartSpec,
    snapshot: DatasheetRevisionSnapshot,
) -> humanrequest.HumanRequest:
    evidence = [
        humanrequest.RequestEvidence(
            kind="hash",
            ref="bound_datasheet",
            sha256=spec.datasheet.sha256,
            summary=f"PartSpec currently binds datasheet revision {spec.datasheet.revision}.",
        ),
        humanrequest.RequestEvidence(
            kind="hash",
            ref="manufacturer_current_datasheet",
            sha256=snapshot.pdf_sha256,
            summary="The current manufacturer response returned this PDF SHA-256.",
        ),
        humanrequest.RequestEvidence(
            kind="note",
            ref=snapshot.source_url,
            summary="Manufacturer source used for the current revision check.",
        ),
    ]
    return humanrequest.build_request(
        kind="datasheet_acquisition",
        subject={
            "manufacturer": spec.manufacturer,
            "mpn": spec.mpn,
            "revision": snapshot.revision,
        },
        reason=(
            "The manufacturer datasheet revision or PDF hash changed since this PartSpec "
            "was authored. Provide the current datasheet and revalidate the affected fields "
            "before approving this part."
        ),
        evidence=evidence,
        known=[
            f"The bound datasheet is revision {spec.datasheet.revision} with SHA-256 "
            f"{spec.datasheet.sha256}.",
            f"The current source returned SHA-256 {snapshot.pdf_sha256}.",
        ],
        unknown=[
            "Whether the current revision changes any authored package, pin, land-pattern, "
            "or orderable value.",
            "Whether the current datasheet is complete and suitable as the new evidence source.",
        ],
        agent_assessment=(
            "The manufacturer source now differs from the datasheet hash or revision bound "
            "to this PartSpec. A changed PDF may alter package, pin, land-pattern, or "
            "orderable facts even when the existing library artifacts remain internally "
            "consistent. This automated check does not interpret those changes and cannot "
            "approve the existing authoring. A human must acquire the current document and "
            "revalidate every affected claim before library approval."
        ),
        recommendation="Provide and revalidate the current datasheet",
        recommendation_rationale=(
            "The approval packet is bound to the older datasheet, so its approval must not "
            "be reused for a newer revision or changed PDF."
        ),
        alternatives=[
            humanrequest.RequestAlternative(
                option="Provide and revalidate the current datasheet",
                risks=["The part remains blocked until the new evidence has been checked."],
            ),
            humanrequest.RequestAlternative(
                option="Keep the current datasheet binding",
                risks=[
                    "The manufacturer change remains unresolved and the part cannot be approved."
                ],
            ),
        ],
        recommended=0,
        details={
            "kind": "datasheet_acquisition",
            "failure_reason": "revision_mismatch",
            "requested_revision": snapshot.revision or "current manufacturer revision",
            "required_sections": [
                "package_drawing",
                "pinout",
                "pin_table",
                "land_pattern",
                "orderable_table",
            ],
            "attempted_sources": [snapshot.source_url],
            "current_source_sha256": snapshot.pdf_sha256,
            "optional_cad_requested": False,
        },
    )


def check_revision(
    spec: partspec.PartSpec,
    fetcher: RevisionFetcher,
    *,
    spec_path: Path | None = None,
    project_path: Path | None = None,
) -> DatasheetRevisionCheck:
    """Compare a PartSpec datasheet binding with the manufacturer's current source."""
    bound_path = spec_path or spec.source_file_path
    if bound_path is not None:
        bound_path = bound_path.resolve()
    project_root = (
        project_path.resolve()
        if project_path is not None
        else confidential.project_root_for(bound_path)
        if bound_path is not None
        else None
    )
    findings = _errata_findings(spec, bound_path)
    source_url = spec.datasheet.source_url or spec.datasheet.url
    if not source_url:
        findings.append(
            partspec.SpecFinding(
                code="datasheet_source_url_missing",
                severity="error",
                field="datasheet.source_url",
                message="no manufacturer source URL is bound to this PartSpec",
            )
        )
        return DatasheetRevisionCheck(
            mpn=spec.mpn,
            bound_revision=spec.datasheet.revision,
            bound_pdf_sha256=spec.datasheet.sha256,
            changed=False,
            approval_invalidated=False,
            findings=findings,
        )
    try:
        snapshot = fetcher(source_url)
    except Exception as exc:
        findings.append(
            partspec.SpecFinding(
                code="datasheet_revision_check_failed",
                severity="error",
                field="datasheet.source_url",
                message=f"manufacturer revision fetch failed: {exc}",
            )
        )
        return DatasheetRevisionCheck(
            mpn=spec.mpn,
            bound_revision=spec.datasheet.revision,
            bound_pdf_sha256=spec.datasheet.sha256,
            changed=False,
            approval_invalidated=False,
            findings=findings,
        )
    current_revision = _normalized_revision(snapshot.revision)
    bound_revision = _normalized_revision(spec.datasheet.revision)
    revision_changed = snapshot.revision is not None and (
        current_revision is None or bound_revision is None or current_revision != bound_revision
    )
    changed = snapshot.pdf_sha256 != spec.datasheet.sha256 or revision_changed
    request_id: str | None = None
    request_path: Path | None = None
    approval_invalidated = False
    if changed:
        findings.append(
            partspec.SpecFinding(
                code="datasheet_revision_changed",
                severity="error",
                field="datasheet",
                message=(
                    f"manufacturer returned revision {snapshot.revision or 'unknown'} "
                    f"with SHA-256 {snapshot.pdf_sha256}, while the PartSpec binds "
                    f"{spec.datasheet.revision} and {spec.datasheet.sha256}"
                ),
            )
        )
        if project_root is None:
            findings.append(
                partspec.SpecFinding(
                    code="datasheet_revision_invalidation_failed",
                    severity="error",
                    field="datasheet",
                    message="a project path is required to invalidate approval and write a request",
                )
            )
        else:
            library_dir = project_root / "library"
            invalidation = RevisionInvalidationRecord(
                mpn=spec.mpn,
                bound_revision=spec.datasheet.revision,
                bound_pdf_sha256=spec.datasheet.sha256,
                current_revision=snapshot.revision,
                current_pdf_sha256=snapshot.pdf_sha256,
                checked_at=snapshot.retrieved_at,
            )
            try:
                existing_path = _invalidation_path(library_dir, invalidation)
                existing = _record_from_path(existing_path) if existing_path.exists() else None
                if existing is not None and existing.request_id is not None:
                    candidate_path = humanrequest.find_request_path(
                        project_root, existing.request_id
                    )
                    if candidate_path.is_file():
                        request = humanrequest.load_request(candidate_path)
                        if request.request_sha256 == existing.request_sha256:
                            request_id = existing.request_id
                            request_path = candidate_path
            except (OSError, ValueError) as exc:
                findings.append(
                    partspec.SpecFinding(
                        code="datasheet_revision_request_failed",
                        severity="error",
                        field="datasheet",
                        message=f"could not load the existing revalidation HumanRequest: {exc}",
                    )
                )
            if request_id is None:
                request = _revision_request(spec, snapshot)
                try:
                    request_path, _ = humanrequest.write_request(
                        request,
                        project_root,
                        confidential=spec.datasheet.confidential,
                    )
                    request_id = request.request_id
                    invalidation = invalidation.model_copy(
                        update={
                            "request_id": request_id,
                            "request_sha256": request.request_sha256,
                        }
                    )
                except (OSError, ValueError) as exc:
                    findings.append(
                        partspec.SpecFinding(
                            code="datasheet_revision_request_failed",
                            severity="error",
                            field="datasheet",
                            message=f"could not write revalidation HumanRequest: {exc}",
                        )
                    )
            try:
                _write_invalidation(library_dir, invalidation)
                approval_invalidated = True
            except (OSError, ValueError) as exc:
                findings.append(
                    partspec.SpecFinding(
                        code="datasheet_revision_invalidation_failed",
                        severity="error",
                        field="datasheet",
                        message=f"could not persist approval invalidation: {exc}",
                    )
                )
    return DatasheetRevisionCheck(
        mpn=spec.mpn,
        bound_revision=spec.datasheet.revision,
        bound_pdf_sha256=spec.datasheet.sha256,
        current_revision=snapshot.revision,
        current_pdf_sha256=snapshot.pdf_sha256,
        changed=changed,
        approval_invalidated=approval_invalidated,
        human_request_id=request_id,
        human_request_json=str(request_path) if request_path is not None else None,
        findings=findings,
    )
