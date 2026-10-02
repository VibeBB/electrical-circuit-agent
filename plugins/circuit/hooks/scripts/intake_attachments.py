"""Materialize user-attached images and PDFs from conversation events.

Scans the agent-canvas event store
(~/.openhands/agent-canvas/dev_conversations/<session_id>/events/event-*.json)
for `source=user` messages carrying image or PDF content. Public attachments
are written to `<workspace>/intake/attachments/`; confidential attachments go
to `<workspace>/.confidential/intake/attachments/`. Runs on
session_start, user_prompt_submit, and stop; idempotent via the manifest and
a `.processed` marker of already-scanned event files. Always exits 0 — when
the events directory is unreachable (remote/docker runtimes) the fallback is
to drop files into `intake/` manually.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _provenance import project_dir

EVENTS_DIR_ENV = "CIRCUIT_AGENT_EVENTS_DIR"
ATTACHMENTS_ENV = "CIRCUIT_INTAKE_ATTACHMENTS_DIR"
DEFAULT_EVENTS_ROOT = Path(".openhands/agent-canvas/dev_conversations")
ATTACHMENTS_RELATIVE = Path("intake/attachments")
PROCESSED_MARKER = ".processed"

_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_DATA_URL = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.DOTALL)
_REQUEST_LINE = re.compile(r"^CIRCUIT-HUMAN-RESPONSE ([0-9a-f]{16})$")
_GENERIC_DATA_URL = re.compile(r"^data:[^,]+;base64,(.+)$", re.DOTALL | re.IGNORECASE)
_MAGIC_EXT = (
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF8", ".gif", "image/gif"),
    (b"RIFF", ".webp", "image/webp"),
)


def _events_dir(payload: dict[str, Any]) -> Path | None:
    override = os.environ.get(EVENTS_DIR_ENV)
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_dir() else None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    candidate = Path.home() / DEFAULT_EVENTS_ROOT / session_id.strip() / "events"
    return candidate if candidate.is_dir() else None


def _attachments_dir(payload: dict[str, Any]) -> Path:
    override = os.environ.get(ATTACHMENTS_ENV)
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else project_dir(payload) / path
    return project_dir(payload) / ATTACHMENTS_RELATIVE


def _image_blocks(value: Any) -> list[dict[str, Any]]:
    """Collect image content blocks from a serialized event (any nesting)."""
    blocks: list[dict[str, Any]] = []
    if isinstance(value, dict):
        record = cast(dict[str, Any], value)
        if record.get("type") == "image" and isinstance(record.get("image_urls"), list):
            blocks.append(record)
        else:
            for child in list(record.values()):
                blocks.extend(_image_blocks(child))
    elif isinstance(value, list):
        for child in cast(list[Any], value):
            blocks.extend(_image_blocks(child))
    return blocks


def _decode_image(url: str) -> tuple[bytes, str, str] | None:
    """Decode a data: URL or bare base64 string to (bytes, ext, mime)."""
    match = _DATA_URL.match(url.strip())
    if match:
        mime, encoded = match.group(1).lower(), match.group(2)
    else:
        mime, encoded = "", url.strip()
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    for magic, ext, detected in _MAGIC_EXT:
        if data.startswith(magic):
            return data, ext, detected
    if mime in _MIME_EXT:
        return data, _MIME_EXT[mime], mime
    return None


def _decode_pdf(value: str) -> bytes | None:
    text = value.strip()
    match = _GENERIC_DATA_URL.fullmatch(text)
    encoded = match.group(1) if match is not None else text
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if data.startswith(b"%PDF-") else None


def _pdf_attachment_values(value: Any, *, attachment_context: bool = False) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text.casefold().startswith("data:") or attachment_context else []
    if isinstance(value, list):
        return [
            url
            for child in cast(list[Any], value)
            for url in _pdf_attachment_values(child, attachment_context=attachment_context)
        ]
    if isinstance(value, dict):
        record = cast(dict[str, Any], value)
        context = attachment_context or record.get("type") in {
            "file",
            "document",
            "pdf",
            "attachment",
        }
        return [
            url
            for child in record.values()
            for url in _pdf_attachment_values(child, attachment_context=context)
        ]
    return []


def _message_text(value: Any, key: str = "") -> list[str]:
    if isinstance(value, str):
        return [value] if key in {"content", "message", "text"} else []
    if isinstance(value, list):
        return [text for child in cast(list[Any], value) for text in _message_text(child)]
    if isinstance(value, dict):
        return [
            text
            for child_key, child in cast(dict[str, Any], value).items()
            for text in _message_text(child, str(child_key))
        ]
    return []


def _response_metadata(record: dict[str, Any]) -> tuple[str | None, bool]:
    lines = [line for text in _message_text(record) for line in text.splitlines()]
    if not lines:
        return None, False
    match = _REQUEST_LINE.fullmatch(lines[0].strip())
    confidential = any(
        key.strip().casefold() == "confidential" and value.strip().casefold() == "yes"
        for line in lines[1:]
        for key, separator, value in [line.partition(":")]
        if separator
    )
    return (match.group(1) if match is not None else None), confidential


def _scan_event_file(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()
        value: Any = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(value, dict):
        return []
    record = cast(dict[str, Any], value)
    if record.get("source") != "user":
        return []
    event_sha256 = hashlib.sha256(raw).hexdigest()
    urls: list[str] = []
    for block in _image_blocks(record):
        for url in block["image_urls"]:
            if isinstance(url, str):
                urls.append(url)
    pdf_urls = [url for url in _pdf_attachment_values(record) if _decode_pdf(url) is not None]
    request_id, confidential = _response_metadata(record)
    if not urls and not pdf_urls:
        return []
    return [
        {
            "event_file": path.name,
            "event_sha256": event_sha256,
            "image_urls": urls,
            "pdf_urls": pdf_urls,
            "request_id": request_id,
            "confidential": confidential,
        }
    ]


def _load_manifest(manifest: Path) -> set[tuple[str, bool]]:
    seen: set[tuple[str, bool]] = set()
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                record = cast(dict[str, Any], record)
                if isinstance(record.get("sha256"), str):
                    seen.add((record["sha256"], record.get("confidential") is True))
    except OSError:
        pass
    return seen


def _confidential_attachments_dir(project: Path) -> Path:
    root = project / ".confidential"
    root.mkdir(parents=True, exist_ok=True)
    gitignore = root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        lines = content.splitlines()
        if "*" not in lines or "!.gitignore" not in lines:
            gitignore.write_text(
                f"{content.rstrip()}\n*\n!.gitignore\n" if content.strip() else "*\n!.gitignore\n",
                encoding="utf-8",
            )
    else:
        gitignore.write_text("*\n!.gitignore\n", encoding="utf-8")
    return root / "intake" / "attachments"


def main() -> int:
    try:
        payload: Any = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload = cast(dict[str, Any], payload)
    events_dir = _events_dir(payload)
    if events_dir is None:
        return 0
    out_dir = _attachments_dir(payload)
    marker = out_dir / PROCESSED_MARKER
    processed: set[str] = set()
    with contextlib.suppress(OSError):
        processed = set(marker.read_text(encoding="utf-8").split())
    manifest = out_dir / "manifest.jsonl"
    seen_sha = _load_manifest(manifest)
    pending = [
        entry
        for path in sorted(events_dir.glob("event-*.json"))
        if path.name not in processed
        for entry in _scan_event_file(path)
    ]
    scanned = [
        path.name for path in sorted(events_dir.glob("event-*.json")) if path.name not in processed
    ]
    if not pending and not scanned:
        return 0
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        for entry in pending:
            attachments = [
                ("image", index, url) for index, url in enumerate(entry["image_urls"])
            ] + [("pdf", index, url) for index, url in enumerate(entry["pdf_urls"])]
            for kind, index, url in attachments:
                decoded_image = _decode_image(url) if kind == "image" else None
                pdf_data = _decode_pdf(url) if kind == "pdf" else None
                record: dict[str, Any] = {
                    "event_file": entry["event_file"],
                    "image_index": index,
                    "attachment_kind": kind,
                    "origin": "user_provided",
                    "event_sha256": entry["event_sha256"],
                    "request_id": entry["request_id"],
                    "confidential": entry["confidential"],
                    "recorded_at": datetime.now(UTC).isoformat(),
                }
                if decoded_image is None and pdf_data is None:
                    record["materialized"] = False
                    record["reason"] = (
                        "non-data-url" if not _DATA_URL.match(url.strip()) else "undecodable"
                    )
                    record["url_prefix"] = url[:80]
                else:
                    if decoded_image is not None:
                        data, ext, mime = decoded_image
                    else:
                        data = cast(bytes, pdf_data)
                        ext, mime = ".pdf", "application/pdf"
                    sha256 = hashlib.sha256(data).hexdigest()
                    record.update(
                        {
                            "materialized": True,
                            "sha256": sha256,
                            "mime": mime,
                            "bytes": len(data),
                        }
                    )
                    seen_key = (sha256, entry["confidential"])
                    target_dir = (
                        _confidential_attachments_dir(project_dir(payload))
                        if entry["confidential"]
                        else out_dir
                    )
                    target_path = target_dir / f"{sha256[:12]}{ext}"
                    if seen_key in seen_sha and target_path.is_file():
                        record["duplicate"] = True
                        record["attachment_path"] = str(target_path)
                    else:
                        target_dir.mkdir(parents=True, exist_ok=True)
                        target_path.write_bytes(data)
                        record["attachment_path"] = str(target_path)
                        if kind == "image":
                            record["image_path"] = str(target_path)
                        seen_sha.add(seen_key)
                records.append(record)
        with manifest.open("a", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                stream.write("\n")
        with marker.open("a", encoding="utf-8") as stream:
            for name in scanned:
                stream.write(name + "\n")
    except OSError as exc:
        print(f"intake attachments unavailable: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
