"""Thermal handoff to simulation-agent and its hash-bound answer."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .brief import DesignBrief
from .workspace import workspace_path

CheckStatus = Literal["pass", "fail", "unknown"]
Finding = tuple[str, CheckStatus, float | None, str]

_REQUEST_SUFFIX = ".sim-request.json"
_RESPONSE_SUFFIX = ".sim-response.json"
_PREFIX = "thermal."
_PATH_KEYS = ("theta_ja_c_per_w", "theta_jc", "theta_cs", "theta_sa")


class SimResponse(BaseModel):
    """Mirror of simulation-agent's ``SimulationResponse`` (schema v2)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[2]
    request_id: str = Field(min_length=1)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    brief_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: Literal["accepted", "rejected", "deferred", "needs_info"]
    verdict: Literal["pass", "fail", "unknown"] | None = None
    report_path: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_refs: list[str] = Field(default_factory=list[str])
    reasons: list[str] = Field(default_factory=list[str])

    @model_validator(mode="after")
    def validate_verdict(self) -> SimResponse:
        if self.status == "accepted" and self.verdict != "pass":
            raise ValueError("accepted responses require a pass verdict")
        if self.status == "rejected" and self.verdict != "fail":
            raise ValueError("rejected responses require a fail verdict")
        if (self.report_path is None) != (self.sha256 is None):
            raise ValueError("report_path and sha256 must be provided together")
        if self.status in ("accepted", "rejected") and self.report_path is None:
            raise ValueError("accepted and rejected responses require a hashed report")
        return self


def thermal_brief(brief: DesignBrief) -> dict[str, Any]:
    """simulation-agent ``*.sim.json`` payload with a ``thermal`` section."""
    if brief.thermal is None:
        raise ValueError("design brief has no thermal section")
    components: list[dict[str, Any]] = []
    for part in brief.thermal.parts:
        component: dict[str, Any] = {
            "ref": part.reference,
            "power_w": part.power_w,
            "tj_max_c": part.tj_max_c,
            "derating_margin_c": part.derating_margin_c,
        }
        path = {key: getattr(part, key) for key in _PATH_KEYS if getattr(part, key) is not None}
        if path:
            component["path"] = path
        components.append(component)
    sources = "; ".join(f"{part.reference}: {part.source}" for part in brief.thermal.parts)
    return {
        "schema_version": 1,
        "name": brief.name,
        "description": f"circuit thermal handoff ({sources})",
        "thermal": {"ambient_c": brief.thermal.ambient_c, "components": components},
    }


def _brief_text(brief: DesignBrief) -> str:
    return json.dumps(thermal_brief(brief), indent=2, sort_keys=True) + "\n"


def expected_request(brief: DesignBrief) -> tuple[str, str]:
    """``(request_id, sim brief sha256)`` the current brief would request."""
    digest = hashlib.sha256(_brief_text(brief).encode("utf-8")).hexdigest()
    return f"{brief.name}-thermal-{digest[:12]}", digest


def write_sim_request(
    brief: DesignBrief, out_dir: Path, *, root: Path | None = None
) -> dict[str, Any]:
    """Write the sim brief and its v1 ``*.sim-request.json``; ``root`` relativises paths."""
    text = _brief_text(brief)
    out_dir.mkdir(parents=True, exist_ok=True)
    sim_brief = out_dir / f"{brief.name}.thermal.sim.json"
    sim_brief.write_text(text, encoding="utf-8")
    request_id, digest = expected_request(brief)
    brief_ref = (sim_brief.relative_to(root) if root is not None else sim_brief).as_posix()
    refs = [part.reference for part in brief.thermal.parts] if brief.thermal else []
    request = {
        "schema_version": 1,
        "from_system": "circuit",
        "request_id": request_id,
        "kind": "thermal",
        "brief_path": brief_ref,
        "question": (
            f"Check junction temperatures of {', '.join(refs)} on {brief.name} "
            "and answer with a sim-response."
        ),
        "requested_by": "circuit",
    }
    request_path = out_dir / f"{brief.name}.thermal{_REQUEST_SUFFIX}"
    request_path.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "verdict": "pass",
        "design": brief.name,
        "parts": refs,
        "sim_brief": brief_ref,
        "sim_brief_sha256": digest,
        "request": str(request_path),
        "request_id": request_id,
    }


def resolve_response(brief: DesignBrief, base_dir: Path) -> Path | None:
    """Path of ``thermal.response_path`` relative to ``base_dir`` (or ``None``)."""
    if brief.thermal is None or brief.thermal.response_path is None:
        return None
    candidate = Path(brief.thermal.response_path)
    return candidate if candidate.is_absolute() else base_dir / candidate


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Any:
    if path.is_symlink():
        raise ValueError(f"{path.name} is a symlink")
    return json.loads(path.read_text(encoding="utf-8"))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _report_findings(report: Any, response: SimResponse) -> list[Finding]:
    if not isinstance(report, dict):
        return [("report", "unknown", None, "sim report is not a JSON object")]
    data: dict[str, Any] = report  # pyright: ignore[reportUnknownVariableType]
    raw_checks: Any = data.get("checks")
    if not isinstance(raw_checks, list):
        return [("report", "unknown", None, "sim report has no checks list")]
    findings: list[Finding] = []
    for raw in raw_checks:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(raw, dict):
            return [("report", "unknown", None, "sim report check is not an object")]
        item: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
        check_id, verdict = item.get("id"), item.get("verdict")
        if not isinstance(check_id, str) or not check_id.startswith(_PREFIX):
            continue
        if verdict not in ("pass", "fail", "unknown"):
            return [("report", "unknown", None, f"{check_id} has no pass/fail/unknown verdict")]
        detail = str(item.get("detail") or "")
        limit = item.get("limit")
        if limit is not None:
            detail = f"{detail}; limit {limit}" if detail else f"limit {limit}"
        margin = _number(item.get("margin"))
        if margin is not None:
            detail = f"{detail}; margin {margin:.6g}"
        guidance: object = item.get("guidance")
        if isinstance(guidance, list):
            fixes = [str(line) for line in guidance if isinstance(line, str)]  # pyright: ignore[reportUnknownVariableType]
            if fixes:
                detail = f"{detail}; fix: {' | '.join(fixes)}"
        measured = _number(item.get("measured"))
        findings.append((check_id.removeprefix(_PREFIX), verdict, measured, detail))
    if not findings:
        return [("report", "unknown", None, "sim report has no thermal checks")]
    if data.get("verdict") != response.verdict:
        return [
            (
                "report",
                "fail",
                None,
                f"report verdict {data.get('verdict')!r} != response verdict {response.verdict!r}",
            )
        ]
    return findings


def thermal_findings(brief: DesignBrief, response_path: Path | None, root: Path) -> list[Finding]:
    """``(subject, status, measured, detail)`` per thermal result; fail-closed.

    The response must answer the request the *current* brief would emit
    (request id and sim brief sha256), the sibling request file must match
    ``request_sha256``, and the hashed report must be unchanged. Only
    simulation's own check verdicts are reported; circuit adds no judgement.
    """
    if brief.thermal is None:
        return []
    if response_path is None:
        return [("response", "unknown", None, "thermal.response_path is not set")]
    if not response_path.name.endswith(_RESPONSE_SUFFIX):
        return [("response", "unknown", None, f"not a {_RESPONSE_SUFFIX}: {response_path.name}")]
    if not response_path.is_file():
        return [("response", "unknown", None, f"simulation response missing: {response_path}")]
    try:
        response = SimResponse.model_validate(_load_json(response_path))
    except (OSError, ValueError, ValidationError) as exc:
        return [("response", "unknown", None, f"invalid simulation response: {exc}")]
    request_path = response_path.with_name(
        response_path.name.removesuffix(_RESPONSE_SUFFIX) + _REQUEST_SUFFIX
    )
    if not request_path.is_file() or request_path.is_symlink():
        return [("response", "unknown", None, f"request missing beside response: {request_path}")]
    if _sha256(request_path) != response.request_sha256:
        return [("response", "fail", None, "stale: request file changed after the response")]
    try:
        request = _load_json(request_path)
    except (OSError, ValueError) as exc:
        return [("response", "unknown", None, f"invalid request: {exc}")]
    expected_id, expected_sha = expected_request(brief)
    if not isinstance(request, dict) or (
        request.get("from_system"),  # pyright: ignore[reportUnknownMemberType]
        request.get("kind"),  # pyright: ignore[reportUnknownMemberType]
        request.get("request_id"),  # pyright: ignore[reportUnknownMemberType]
    ) != ("circuit", "thermal", response.request_id):
        return [("response", "fail", None, "response does not answer a circuit thermal request")]
    if response.request_id != expected_id or response.brief_sha256 != expected_sha:
        return [
            (
                "response",
                "fail",
                None,
                f"stale: answers {response.request_id}, current brief requests {expected_id}",
            )
        ]
    if response.status in ("needs_info", "deferred"):
        reasons = "; ".join(response.reasons) or "no reason given"
        return [("response", "unknown", None, f"simulation {response.status}: {reasons}")]
    assert response.report_path is not None and response.sha256 is not None
    try:
        report_path = workspace_path(response.report_path, root=root)
    except ValueError as exc:
        return [("report", "unknown", None, str(exc))]
    if not report_path.is_file():
        return [("report", "unknown", None, f"hashed sim report missing: {response.report_path}")]
    if _sha256(report_path) != response.sha256:
        return [("report", "fail", None, "stale: sim report changed after the response")]
    try:
        report = _load_json(report_path)
    except (OSError, ValueError) as exc:
        return [("report", "unknown", None, f"invalid sim report: {exc}")]
    findings = _report_findings(report, response)
    if findings[0][0] == "report":
        return findings
    return [("response", "pass", None, f"{response.request_id} {response.status}"), *findings]


def thermal_check(brief: DesignBrief, response_path: Path | None, root: Path) -> dict[str, Any]:
    """JSON verdict over :func:`thermal_findings`; no thermal section is ``unknown``."""
    findings = thermal_findings(brief, response_path, root)
    if not findings:
        findings = [("thermal", "unknown", None, "design brief has no thermal section")]
    statuses = {status for _, status, _, _ in findings}
    verdict: CheckStatus = (
        "fail" if "fail" in statuses else "unknown" if "unknown" in statuses else "pass"
    )
    return {
        "verdict": verdict,
        "design": brief.name,
        "checks": [
            {"subject": subject, "status": status, "measured": measured, "detail": detail}
            for subject, status, measured, detail in findings
        ],
    }


__all__ = [
    "SimResponse",
    "expected_request",
    "resolve_response",
    "thermal_brief",
    "thermal_check",
    "thermal_findings",
    "write_sim_request",
]
