"""VibeBB Record Protocol (VRP) v1 — stdlib validator shared by every plugin.

Three append-only JSONL logs live under ``<records_dir>`` (from the
plugin's ``hooks/records-policy.json``, normally ``observations/<plugin>``):

* ``decisions.jsonl`` — design decisions with first principles, at least
  two options, evidence bound by sha256, unknowns, risks and a revisit
  trigger.
* ``impressions.jsonl`` — one long-form impression per finished stage,
  bound to the artifacts it read (file or directory tree sha256).
* ``vision-reviews.jsonl`` — what the model thought after looking at an
  image, bound to the image bytes or to the vision tool event.

The plugin core writes these records through its typed models; this
module re-validates them from hooks with nothing but the standard
library, so a hand-written or tampered line is caught at Stop time.
Records are advisory evidence: they never change a gate verdict.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = 1
IMPRESSION_MIN_CHARS = 400
IMPRESSION_MIN_SENTENCES = 3
RATIONALE_MIN_CHARS = 200
PRINCIPLE_MIN_CHARS = 12
QUESTION_MIN_CHARS = 10
DEFAULT_MAX_STOP_DENIALS = 2
LOG_FILES = {
    "decision": "decisions.jsonl",
    "stage_impression": "impressions.jsonl",
    "vision_review": "vision-reviews.jsonl",
}
VISION_EVENTS_FILE = "vision-tool-events.jsonl"
IMAGE_OBSERVATIONS_FILE = "image-observations.jsonl"
SKIP_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
SEVERITIES = {"info", "warning", "error"}
DECIDERS = {"agent", "user"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CJK_END = re.compile(r"[。\uff01\uff1f]")
_LATIN_END = re.compile(r"[.!?](?=\s|$)")


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。\uff01\uff1f])|(?<=[.!?])(?=\s|$)", text)
    return [part.strip() for part in parts if part.strip()]


def sentence_count(text: str) -> int:
    return len(_CJK_END.findall(text)) + len(_LATIN_END.findall(text))


def impression_errors(value: Any, field: str = "impression") -> list[str]:
    if not isinstance(value, str):
        return [f"{field} must be a string"]
    text = value.strip()
    errors: list[str] = []
    if len(text) < IMPRESSION_MIN_CHARS:
        errors.append(
            f"{field} has {len(text)} characters; write at least"
            f" {IMPRESSION_MIN_CHARS} (what you noticed, what works, what worries"
            " you, what a maker or user would feel, what to do next)"
        )
    if sentence_count(text) < IMPRESSION_MIN_SENTENCES:
        errors.append(f"{field} must contain at least {IMPRESSION_MIN_SENTENCES} sentences")
    distinct = set(sentences(text))
    enough = sentence_count(text) >= IMPRESSION_MIN_SENTENCES
    if enough and len(distinct) < IMPRESSION_MIN_SENTENCES:
        errors.append(f"{field} repeats the same sentence")
    return errors


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(path: Path) -> str:
    """sha256 of a file, or of a directory's sorted (relative path, sha256) list."""
    if path.is_file():
        return sha256_file(path)
    digest = hashlib.sha256()
    for child in sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink()):
        relative = child.relative_to(path).as_posix()
        if any(part in SKIP_PARTS for part in child.relative_to(path).parts):
            continue
        digest.update(f"{relative}\0{sha256_file(child)}\n".encode())
    return digest.hexdigest()


def _str(value: Any, min_chars: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= min_chars


def _str_list(value: Any, min_items: int = 0, min_chars: int = 1) -> bool:
    if not isinstance(value, list):
        return False
    items = cast(list[Any], value)
    return len(items) >= min_items and all(_str(item, min_chars) for item in items)


def _ref_errors(value: Any, field: str, *, allow_reference: bool) -> list[str]:
    if not isinstance(value, dict):
        return [f"{field} must be an object"]
    entry = cast(dict[str, Any], value)
    if allow_reference and _str(entry.get("reference")) and entry.get("path") is None:
        return []
    errors: list[str] = []
    if not _str(entry.get("path")):
        errors.append(f"{field}.path is required")
    sha = entry.get("sha256")
    if not isinstance(sha, str) or not _SHA256.match(sha):
        errors.append(f"{field}.sha256 must be a lowercase hex sha256")
    return errors


def _envelope_errors(record: dict[str, Any], kind: str) -> list[str]:
    errors: list[str] = []
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if record.get("kind") != kind:
        errors.append(f"kind must be {kind!r}")
    if not _str(record.get("plugin")):
        errors.append("plugin is required")
    if parse_time(record.get("recorded_at")) is None:
        errors.append("recorded_at must be an ISO-8601 timestamp with a timezone")
    if not isinstance(record.get("sequence"), int):
        errors.append("sequence must be an integer")
    if not isinstance(record.get("event_id"), str) or not _SHA256.match(record["event_id"]):
        errors.append("event_id must be a sha256")
    return errors


def decision_errors(record: dict[str, Any]) -> list[str]:
    errors = _envelope_errors(record, "decision")
    for key in ("id", "stage"):
        value = record.get(key)
        if not isinstance(value, str) or not _SLUG.match(value):
            errors.append(f"{key} must be a lowercase slug")
    if not _str(record.get("question"), QUESTION_MIN_CHARS):
        errors.append(f"question must state the decision in at least {QUESTION_MIN_CHARS} chars")
    if not _str_list(record.get("principles"), 1, PRINCIPLE_MIN_CHARS):
        errors.append(
            "principles must cite at least one first principle, law or standard"
            f" (each at least {PRINCIPLE_MIN_CHARS} chars)"
        )
    options = record.get("options")
    names: list[str] = []
    if not isinstance(options, list) or len(cast(list[Any], options)) < 2:
        errors.append("options must list at least two considered alternatives")
    else:
        for index, option in enumerate(cast(list[Any], options)):
            if not isinstance(option, dict):
                errors.append(f"options[{index}] must be an object")
                continue
            entry = cast(dict[str, Any], option)
            if not _str(entry.get("name")):
                errors.append(f"options[{index}].name is required")
            else:
                names.append(str(entry["name"]))
            for side in ("pros", "cons"):
                if not _str_list(entry.get(side), 1):
                    errors.append(f"options[{index}].{side} needs at least one entry")
        if len(set(names)) != len(names):
            errors.append("option names must be unique")
    if record.get("chosen") not in names:
        errors.append("chosen must name one of the options")
    if not _str(record.get("rationale"), RATIONALE_MIN_CHARS):
        errors.append(f"rationale must explain the choice in at least {RATIONALE_MIN_CHARS} chars")
    evidence = record.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("evidence must cite at least one artifact (path+sha256) or reference")
    else:
        for index, entry in enumerate(cast(list[Any], evidence)):
            errors.extend(_ref_errors(entry, f"evidence[{index}]", allow_reference=True))
    for key in ("assumptions", "unknowns"):
        if not _str_list(record.get(key)):
            errors.append(f"{key} must be a list of strings (empty only when truly none)")
    if not _str_list(record.get("risks"), 1):
        errors.append("risks must name at least one residual risk of the chosen option")
    if not _str(record.get("revisit_when")):
        errors.append("revisit_when must name the observation that would reopen the decision")
    if record.get("decided_by") not in DECIDERS:
        errors.append("decided_by must be 'agent' or 'user'")
    return errors


def stage_impression_errors(record: dict[str, Any]) -> list[str]:
    errors = _envelope_errors(record, "stage_impression")
    stage = record.get("stage")
    if not isinstance(stage, str) or not _SLUG.match(stage):
        errors.append("stage must be a lowercase slug")
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("artifacts must list what the stage produced or read (path+sha256)")
    else:
        for index, entry in enumerate(cast(list[Any], artifacts)):
            errors.extend(_ref_errors(entry, f"artifacts[{index}]", allow_reference=False))
    errors.extend(impression_errors(record.get("impression")))
    return errors


def vision_review_errors(record: dict[str, Any]) -> list[str]:
    errors = _envelope_errors(record, "vision_review")
    has_image = _str(record.get("image_path"))
    if has_image:
        sha = record.get("image_sha256")
        if not isinstance(sha, str) or not _SHA256.match(sha):
            errors.append("image_sha256 must bind the review to the image bytes")
    if not has_image and not _str(record.get("source_event_id")):
        errors.append("a vision review needs image_path or source_event_id")
    if not _str(record.get("model")):
        errors.append("model is required (the vision-capable model that read the image)")
    checklist = record.get("checklist")
    if not isinstance(checklist, str) or not _SLUG.match(checklist):
        errors.append("checklist must be a lowercase slug")
    findings = record.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be a list")
    else:
        for index, finding in enumerate(cast(list[Any], findings)):
            if not isinstance(finding, dict):
                errors.append(f"findings[{index}] must be an object")
                continue
            entry = cast(dict[str, Any], finding)
            if not _str(entry.get("category")):
                errors.append(f"findings[{index}].category is required")
            if entry.get("severity") not in SEVERITIES:
                errors.append(f"findings[{index}].severity must be one of {sorted(SEVERITIES)}")
            if not _str(entry.get("note")):
                errors.append(f"findings[{index}].note is required")
    errors.extend(impression_errors(record.get("impression")))
    return errors


VALIDATORS = {
    "decision": decision_errors,
    "stage_impression": stage_impression_errors,
    "vision_review": vision_review_errors,
}


def record_errors(kind: str, record: Any) -> list[str]:
    if not isinstance(record, dict):
        return ["record must be a JSON object"]
    return VALIDATORS[kind](cast(dict[str, Any], record))


def parse_time(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.timestamp()


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Return (object lines, count of malformed lines)."""
    if not path.is_file():
        return [], 0
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value: Any = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(value, dict):
            records.append(cast(dict[str, Any], value))
        else:
            malformed += 1
    return records, malformed


def load_policy(plugin_root: Path) -> dict[str, Any]:
    path = plugin_root / "hooks" / "records-policy.json"
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a JSON object")
    policy = cast(dict[str, Any], value)
    if policy.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version must be {SCHEMA_VERSION}")
    if not _str(policy.get("plugin")) or not _str(policy.get("records_dir")):
        raise ValueError(f"{path}: plugin and records_dir are required")
    if not _str_list(policy.get("artifact_globs"), 1):
        raise ValueError(f"{path}: artifact_globs must list at least one glob")
    return policy


def matches(relative: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)


def changed_artifacts(root: Path, policy: dict[str, Any], since: float) -> list[str]:
    """Workspace-relative files matching artifact_globs modified at or after `since`."""
    records_dir = str(policy["records_dir"]).strip("/")
    ignore = cast(list[str], policy.get("ignore_globs") or [])
    found: set[str] = set()
    for pattern in cast(list[str], policy["artifact_globs"]):
        for path in root.glob(pattern):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            parts = path.relative_to(root).parts
            if any(part in SKIP_PARTS for part in parts):
                continue
            if relative.startswith(records_dir + "/") or matches(relative, ignore):
                continue
            try:
                if path.stat().st_mtime >= since:
                    found.add(relative)
            except OSError:
                continue
    return sorted(found)


def covers(entry_path: str, relative: str) -> bool:
    entry = entry_path.strip("/").removeprefix("./")
    return relative == entry or relative.startswith(entry + "/") or entry in ("", ".")
