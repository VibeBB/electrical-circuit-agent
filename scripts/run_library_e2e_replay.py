#!/usr/bin/env python3
"""Replay synthetic library transcripts through the circuit MCP dispatcher."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import random
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch as mock_patch


class ReplayError(RuntimeError):
    pass


class _ReplayClock(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _ReplayClock:
        return cls(2025, 1, 1, tzinfo=tz or UTC)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project_file(project: Path, value: str) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else project / path).resolve()
    if not resolved.is_relative_to(project.resolve()):
        raise ReplayError(f"transcript path escapes its temporary project: {value}")
    return resolved


def _json_pointer(value: object, pointer: str) -> object:
    if not pointer:
        return value
    if not pointer.startswith("/"):
        raise ReplayError(f"invalid JSON pointer: {pointer}")
    current = value
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            mapping = cast(dict[str, object], current)
            if token not in mapping:
                raise ReplayError(f"JSON pointer does not exist: {pointer}")
            current = mapping[token]
        elif isinstance(current, list):
            try:
                current = cast(list[object], current)[int(token)]
            except (ValueError, IndexError) as exc:
                raise ReplayError(f"JSON pointer does not exist: {pointer}") from exc
        else:
            raise ReplayError(f"JSON pointer does not exist: {pointer}")
    return current


def _reference(value: object, results: Mapping[str, object]) -> object:
    if isinstance(value, list):
        return [_reference(item, results) for item in cast(list[object], value)]
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        if set(mapping) == {"$ref"}:
            reference = mapping["$ref"]
            if not isinstance(reference, str) or "#" not in reference:
                raise ReplayError("$ref must use step-id#/json/pointer syntax")
            step_id, pointer = reference.split("#", 1)
            if step_id not in results:
                raise ReplayError(f"transcript references unknown step: {step_id}")
            return _json_pointer(results[step_id], pointer)
        if set(mapping) == {"$sha256"}:
            path_value = mapping["$sha256"]
            if not isinstance(path_value, str):
                raise ReplayError("$sha256 must contain a project-relative path")
            return _sha256(_project_file(_ACTIVE_PROJECT.get(), path_value))
        if set(mapping) == {"$vision_replay"}:
            parameters = mapping["$vision_replay"]
            if not isinstance(parameters, dict):
                raise ReplayError("$vision_replay must contain batch_path and items references")
            parameters = cast(dict[str, object], parameters)
            batch_path = _reference(parameters.get("batch_path"), results)
            items = _reference(parameters.get("items"), results)
            if not isinstance(batch_path, str) or not isinstance(items, list):
                raise ReplayError("vision replay references did not resolve")
            control_answer = _VISION_CONTROL_ANSWERS.get(batch_path)
            if control_answer is None:
                raise ReplayError("vision replay has no private control answer for this batch")
            impression = (
                "This synthetic vision replay inspects the generated datasheet crop and its "
                "numbered mechanical tokens. The image is used only to exercise the recording "
                "gate, and the transcript makes no assertion about a real component. No reading "
                "is claimed as manufacturer evidence, and no agent was started for this replay."
            )
            answers: dict[str, dict[str, str]] = {}
            batch_document_value = json.loads(
                _project_file(_ACTIVE_PROJECT.get(), batch_path).read_text(encoding="utf-8")
            )
            if not isinstance(batch_document_value, dict):
                raise ReplayError("vision batch artifact is malformed")
            batch_document = cast(dict[str, Any], batch_document_value)
            batch_items = batch_document.get("items")
            if not isinstance(batch_items, list):
                raise ReplayError("vision batch artifact is malformed")
            persisted_items: dict[str, dict[str, Any]] = {}
            for item_value in cast(list[object], batch_items):
                if not isinstance(item_value, dict):
                    continue
                item = cast(dict[str, Any], item_value)
                read_id = item.get("read_id")
                if isinstance(read_id, str):
                    persisted_items[read_id] = item
            for item_value in cast(list[object], items):
                if not isinstance(item_value, dict):
                    raise ReplayError("vision replay item is malformed")
                item = cast(dict[str, Any], item_value)
                read_id = item.get("read_id")
                kind = item.get("kind")
                if not isinstance(read_id, str) or not isinstance(kind, str):
                    raise ReplayError("vision replay item has no read_id or kind")
                if kind == "transcribe":
                    answer = control_answer
                elif kind == "som_tokens":
                    persisted = persisted_items.get(read_id)
                    tokens = persisted.get("tokens") if isinstance(persisted, dict) else None
                    if not isinstance(tokens, list) or not tokens:
                        raise ReplayError("Set-of-Mark replay item has no numbered tokens")
                    observations: dict[str, dict[str, str]] = {}
                    for index, token_value in enumerate(cast(list[object], tokens), start=1):
                        if not isinstance(token_value, dict):
                            raise ReplayError("Set-of-Mark replay token is malformed")
                        token = cast(dict[str, Any], token_value)
                        if not isinstance(token.get("token_id"), str):
                            raise ReplayError("Set-of-Mark replay token is malformed")
                        observations[f"token_{index}"] = {"token_id": cast(str, token["token_id"])}
                    answer = json.dumps(observations, sort_keys=True)
                else:
                    raise ReplayError(f"unsupported synthetic vision kind: {kind}")
                answers[read_id] = {"answer": answer, "impression": impression}
            return answers
        return {key: _reference(item, results) for key, item in mapping.items()}
    if isinstance(value, str):
        return value.replace("{{project}}", str(_ACTIVE_PROJECT.get()))
    return value


class _ActiveProject:
    def __init__(self) -> None:
        self.path: Path | None = None

    def set(self, path: Path) -> None:
        self.path = path

    def get(self) -> Path:
        if self.path is None:
            raise ReplayError("temporary project has not been initialized")
        return self.path


_ACTIVE_PROJECT = _ActiveProject()
_VISION_CONTROL_ANSWERS: dict[str, str] = {}


def _read_transcript(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayError(f"cannot read transcript {path}: {exc}") from exc
    if not records or not isinstance(records[0], dict):
        raise ReplayError(f"transcript has no scenario header: {path}")
    header = cast(dict[str, Any], records[0])
    if header.get("type") != "scenario" or header.get("label") != "synthetic":
        raise ReplayError(f"transcript must start with a synthetic scenario header: {path}")
    if header.get("version") != 1:
        raise ReplayError(f"unsupported transcript version in {path}")
    steps = records[1:]
    if not all(isinstance(step, dict) for step in steps):
        raise ReplayError(f"transcript steps must be JSON objects: {path}")
    return header, cast(list[dict[str, Any]], steps)


def _write_synthetic_pdf(path: Path, lines: Sequence[str]) -> None:
    def pdf_string(value: str) -> str:
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    commands = ["BT", "/F1 12 Tf"]
    for index, line in enumerate(lines):
        commands.extend((f"1 0 0 1 72 {740 - index * 24} Tm", f"({pdf_string(line)}) Tj"))
    commands.append("ET")
    content = ("\n".join(commands) + "\n").encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"endstream",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output)


def _setup(project: Path, repository: Path, header: Mapping[str, Any]) -> None:
    setup = header.get("setup", [])
    if not isinstance(setup, list):
        raise ReplayError("scenario setup must be a list")
    for raw_value in cast(list[object], setup):
        if not isinstance(raw_value, dict):
            raise ReplayError("scenario setup records must be objects")
        raw = cast(dict[str, Any], raw_value)
        operation = raw.get("op")
        if operation == "copy_fixture":
            source_value = raw.get("source")
            target_value = raw.get("target")
            if not isinstance(source_value, str) or not isinstance(target_value, str):
                raise ReplayError("copy_fixture requires source and target")
            source = (repository / source_value).resolve()
            if not source.is_relative_to(repository.resolve()) or not source.is_file():
                raise ReplayError(
                    f"fixture file is missing or outside the repository: {source_value}"
                )
            target = _project_file(project, target_value)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        elif operation == "write_text":
            target_value = raw.get("path")
            text = raw.get("text")
            if not isinstance(target_value, str) or not isinstance(text, str):
                raise ReplayError("write_text requires path and text")
            target = _project_file(project, target_value)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        elif operation == "write_pdf":
            target_value = raw.get("path")
            lines = raw.get("lines")
            if (
                not isinstance(target_value, str)
                or not isinstance(lines, list)
                or not all(isinstance(line, str) for line in cast(list[object], lines))
            ):
                raise ReplayError("write_pdf requires a path and string lines")
            _write_synthetic_pdf(_project_file(project, target_value), cast(list[str], lines))
        elif operation == "copy_file":
            source_value = raw.get("source")
            target_value = raw.get("target")
            if not isinstance(source_value, str) or not isinstance(target_value, str):
                raise ReplayError("copy_file requires source and target")
            source = _project_file(project, source_value)
            target = _project_file(project, target_value)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        elif operation == "patch_json":
            _patch_json(project, raw, {})
        else:
            raise ReplayError(f"unsupported scenario setup operation: {operation}")


def _patch_json(
    project: Path, raw: Mapping[str, Any], results: Mapping[str, object]
) -> dict[str, Any]:
    path_value = raw.get("path")
    patches = raw.get("patches")
    if not isinstance(path_value, str) or not isinstance(patches, list):
        raise ReplayError("patch_json requires path and patches")
    path = _project_file(project, path_value)
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayError(f"cannot load JSON for patching: {path}: {exc}") from exc
    for patch_value in cast(list[object], patches):
        if not isinstance(patch_value, dict):
            raise ReplayError("JSON patches require a pointer")
        patch_record = cast(dict[str, Any], patch_value)
        if not isinstance(patch_record.get("pointer"), str):
            raise ReplayError("JSON patches require a pointer")
        pointer = cast(str, patch_record["pointer"])
        tokens = pointer.strip("/").split("/")
        if not pointer.startswith("/") or not tokens:
            raise ReplayError(f"invalid patch pointer: {pointer}")
        current = document
        for token in tokens[:-1]:
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(current, list):
                current = cast(list[Any], current)[int(token)]
            else:
                current = cast(dict[str, Any], current)[token]
        final = tokens[-1].replace("~1", "/").replace("~0", "~")
        value = _reference(patch_record.get("value"), results)
        if isinstance(current, list):
            cast(list[Any], current)[int(final)] = value
        else:
            cast(dict[str, Any], current)[final] = value
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": str(path), "sha256": _sha256(path)}


def _check_predicate(result: object, predicate: Mapping[str, Any]) -> None:
    pointer = predicate.get("pointer")
    operation = predicate.get("op")
    if not isinstance(pointer, str) or not isinstance(operation, str):
        raise ReplayError("predicates require pointer and op")
    try:
        actual = _json_pointer(result, pointer)
    except ReplayError:
        if operation == "exists" and predicate.get("value") is False:
            return
        raise
    expected = predicate.get("value")
    if operation == "equals" and actual != expected:
        raise ReplayError(f"predicate {pointer} expected {expected!r}, got {actual!r}")
    if operation == "not_equals" and actual == expected:
        raise ReplayError(f"predicate {pointer} unexpectedly equals {expected!r}")
    if operation == "contains":
        if isinstance(actual, str):
            contains = isinstance(expected, str) and expected in actual
        elif isinstance(actual, list):
            contains = expected in cast(list[Any], actual)
        elif isinstance(actual, dict):
            contains = expected in cast(dict[Any, Any], actual)
        else:
            contains = False
        if not contains:
            raise ReplayError(f"predicate {pointer} does not contain {expected!r}")
    if operation == "contains_object":
        contains_object = False
        if isinstance(actual, list) and isinstance(expected, dict):
            expected_mapping = cast(dict[str, Any], expected)
            contains_object = any(
                isinstance(item, dict)
                and all(
                    cast(dict[str, Any], item).get(key) == value
                    for key, value in expected_mapping.items()
                )
                for item in cast(list[object], actual)
            )
        if not contains_object:
            raise ReplayError(f"predicate {pointer} has no object matching {expected!r}")
    if operation == "exists" and (actual is None) != (expected is False):
        raise ReplayError(f"predicate {pointer} existence did not match {expected!r}")
    if operation == "truthy" and bool(cast(Any, actual)) is not bool(cast(Any, expected)):
        raise ReplayError(f"predicate {pointer} truthiness did not match {expected!r}")
    if operation == "length" and (
        not isinstance(actual, (str, list, dict)) or len(cast(Any, actual)) != expected
    ):
        raise ReplayError(f"predicate {pointer} length did not match {expected!r}")
    if operation not in {
        "equals",
        "not_equals",
        "contains",
        "contains_object",
        "exists",
        "truthy",
        "length",
    }:
        raise ReplayError(f"unsupported predicate operation: {operation}")


def _artifact_checks(
    project: Path,
    checks: object,
    results: Mapping[str, object],
) -> list[dict[str, str]]:
    if checks is None:
        return []
    if not isinstance(checks, list):
        raise ReplayError("artifact checks must be a list")
    records: list[dict[str, str]] = []
    for check_value in cast(list[object], checks):
        if not isinstance(check_value, dict):
            raise ReplayError("artifact checks must be objects")
        resolved_value = _reference(cast(dict[str, object], check_value), results)
        if not isinstance(resolved_value, dict):
            raise ReplayError("artifact check must resolve to an object")
        resolved = cast(dict[str, Any], resolved_value)
        path_value = resolved.get("path")
        expected = resolved.get("sha256")
        if not isinstance(path_value, str):
            raise ReplayError("artifact check path must resolve to a string")
        path = _project_file(project, path_value)
        if not path.is_file():
            raise ReplayError(f"expected artifact is missing: {path}")
        actual = _sha256(path)
        if expected != "auto" and expected != actual:
            raise ReplayError(
                f"artifact hash mismatch for {path}: expected {expected!r}, got {actual}"
            )
        records.append({"path": path.relative_to(project).as_posix(), "sha256": actual})
    return records


def _event_step(
    project: Path,
    events_root: Path,
    step: Mapping[str, Any],
    results: Mapping[str, object],
) -> dict[str, Any]:
    event_kind = step.get("kind")
    event_id = step.get("id")
    if not isinstance(event_kind, str) or not isinstance(event_id, str):
        raise ReplayError("human_event requires kind and id")
    resolved = cast(dict[str, Any], _reference(dict(step), results))
    recorded_at_value = resolved.get("recorded_at", "2025-01-01T00:00:00+00:00")
    if not isinstance(recorded_at_value, str):
        raise ReplayError("recorded_at must be an ISO timestamp")
    recorded_at = datetime.fromisoformat(recorded_at_value)
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ReplayError("recorded_at must include a UTC offset")
    if event_kind == "human_request":
        request_id = resolved.get("request_id")
        decision = resolved.get("decision")
        fields = resolved.get("fields", {})
        if (
            not isinstance(request_id, str)
            or not isinstance(decision, str)
            or not isinstance(fields, dict)
        ):
            raise ReplayError("human request response requires request_id, decision, and fields")
        from circuit import humanrequest

        request_path = humanrequest.find_request_path(project, request_id)
        request = humanrequest.load_request(request_path)
        lines = [f"CIRCUIT-HUMAN-RESPONSE {request_id}", f"decision: {decision}"]
        lines.extend(
            f"{key}: {value}" for key, value in sorted(cast(dict[str, Any], fields).items())
        )
        if "reviewer" not in fields:
            lines.append("reviewer: Synthetic reviewer")
        event_path = events_root / f"{event_id}.json"
        event = _user_event("\n".join(lines), resolved.get("attachment"), project)
        event_sha = _write_event(event_path, event)
        pointer_path = request_path.parent / "responses" / f"{request_id}.{event_sha[:12]}.json"
        pointer = {
            "artifact_kind": "circuit_human_response_pointer",
            "request_id": request_id,
            "request_sha256": request.request_sha256,
            "event_path": str(event_path),
            "event_sha256": event_sha,
            "recorded_at": recorded_at.isoformat(),
        }
        _write_json(pointer_path, pointer)
        return {
            "event_path": str(event_path),
            "event_sha256": event_sha,
            "pointer_path": str(pointer_path),
            "request_sha256": request.request_sha256,
        }
    if event_kind == "library_review":
        packet_id = resolved.get("packet_id")
        packet_dir_value = resolved.get("packet_dir")
        library_dir_value = resolved.get("library_dir")
        if not all(
            isinstance(value, str) for value in (packet_id, packet_dir_value, library_dir_value)
        ):
            raise ReplayError(
                "library review event requires packet_id, packet_dir, and library_dir"
            )
        packet_dir = _project_file(project, cast(str, packet_dir_value))
        library_dir = _project_file(project, cast(str, library_dir_value))
        review_path = packet_dir / "review.json"
        review_value = json.loads(review_path.read_text(encoding="utf-8"))
        if not isinstance(review_value, dict):
            raise ReplayError("review packet must be an object")
        review = cast(dict[str, Any], review_value)
        questions = review.get("blind_questions", [])
        if not isinstance(questions, list):
            raise ReplayError("review packet blind_questions must be a list")
        questions = cast(list[object], questions)
        inputs = review.get("inputs")
        if not isinstance(inputs, dict):
            raise ReplayError("review packet has no PartSpec input path")
        input_values = cast(dict[str, Any], inputs)
        if not isinstance(input_values.get("spec_path"), str):
            raise ReplayError("review packet has no PartSpec input path")
        from circuit import authoring, libreview, partspec

        spec = partspec.load_part_spec(_project_file(project, input_values["spec_path"]))
        comparison_value = review.get("authoring_comparison")
        comparison = (
            authoring.AuthoringComparison.model_validate(comparison_value)
            if isinstance(comparison_value, dict)
            else None
        )
        expected_answers = {
            question.question_id: question.expected
            for question in libreview.blind_questions(spec, cast(str, packet_id), comparison)
        }
        lines = [
            f"CIRCUIT-LIBRARY-REVIEW {packet_id}",
            f"decision: {resolved.get('decision', 'approve')}",
            f"reviewer: {resolved.get('reviewer', 'Synthetic reviewer')}",
        ]
        public_question_ids: set[str] = set()
        for question_value in questions:
            if not isinstance(question_value, dict):
                raise ReplayError("review packet contains an invalid blind question")
            question = cast(dict[str, Any], question_value)
            if not isinstance(question.get("question_id"), str) or not isinstance(
                question.get("prompt"), str
            ):
                raise ReplayError("review packet contains an invalid blind question")
            question_id = cast(str, question["question_id"])
            public_question_ids.add(question_id)
            answer = expected_answers.get(question_id)
            if (
                answer is None
                and "Does the cited datasheet support the displayed" in question["prompt"]
            ):
                answer = "no"
                lines.append(
                    f"finding: {question_id} | synthetic reviewer flagged the unsupported value"
                )
            if answer is None:
                raise ReplayError(f"no scripted answer for review question: {question_id}")
            lines.append(f"answer: {question_id} = {answer}")
        if not set(expected_answers).issubset(public_question_ids):
            raise ReplayError("scripted review answers do not cover the packet questions")
        event_path = events_root / "replay" / "events" / f"event-{event_id}.json"
        event_sha = _write_event(event_path, _user_event("\n".join(lines), None, project))
        pointer_path = library_dir / "reviews" / "decisions" / f"{packet_id}.{event_sha[:12]}.json"
        _write_json(
            pointer_path,
            {
                "artifact_kind": "circuit_library_review_pointer",
                "packet_id": packet_id,
                "event_path": str(event_path),
                "event_sha256": event_sha,
            },
        )
        return {"event_path": str(event_path), "event_sha256": event_sha}
    raise ReplayError(f"unsupported human event kind: {event_kind}")


def _user_event(text: str, attachment: object, project: Path) -> dict[str, Any]:
    message: dict[str, Any] = {"content": text}
    if attachment is not None:
        if not isinstance(attachment, str):
            raise ReplayError("human event attachment must be a project-relative PDF path")
        path = _project_file(project, attachment)
        if not path.is_file() or path.suffix.casefold() != ".pdf":
            raise ReplayError("human event attachment must be an existing PDF")
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        message["attachments"] = [
            {
                "name": path.name,
                "url": "data:application/pdf;base64," + data,
            }
        ]
    return {"source": "user", "message": message}


def _write_event(path: Path, event: Mapping[str, Any]) -> str:
    _write_json(path, event)
    return _sha256(path)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _redact_private(value: object, secrets: Sequence[str]) -> object:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[private-control-redacted]")
        return value
    if isinstance(value, list):
        return [_redact_private(item, secrets) for item in cast(list[object], value)]
    if isinstance(value, dict):
        return {
            key: _redact_private(item, secrets)
            for key, item in cast(dict[str, object], value).items()
        }
    return value


def _tool_result(raw: object) -> tuple[bool, object]:
    is_error = bool(getattr(raw, "isError", False))
    content = getattr(raw, "content", [])
    text = next(
        (
            item.text
            for item in content
            if getattr(item, "type", None) == "text"
            and isinstance(getattr(item, "text", None), str)
        ),
        None,
    )
    if text is None:
        return is_error, {"content": str(content)}
    try:
        return is_error, json.loads(text)
    except json.JSONDecodeError:
        return is_error, {"message": text}


async def _call_tool(
    tool_name: str, arguments: dict[str, Any], seed: str
) -> tuple[bool, object, str | None]:
    from circuit import authoring, mcp_server, visionread

    control_answers: list[str] = []
    control_image_attribute = "_control_image"
    original_control_image = cast(
        Callable[[Path, tuple[int, int]], str],
        getattr(visionread, control_image_attribute),
    )
    entropy = random.Random(seed)

    def capture_control_image(path: Path, size: tuple[int, int]) -> str:
        answer = original_control_image(path, size)
        control_answers.append(answer)
        return answer

    def deterministic_token_hex(nbytes: int = 32) -> str:
        return entropy.randbytes(nbytes).hex()

    with ExitStack() as patches:
        if tool_name in {"circuit_vision_read", "circuit_vision_answer"}:
            patches.enter_context(mock_patch.object(visionread, "datetime", _ReplayClock))
            patches.enter_context(
                mock_patch.object(visionread.secrets, "token_hex", deterministic_token_hex)
            )
            patches.enter_context(mock_patch.object(visionread.secrets, "choice", entropy.choice))
            patches.enter_context(
                mock_patch.object(visionread.secrets, "randbelow", entropy.randrange)
            )
            patches.enter_context(
                mock_patch.object(visionread.secrets, "SystemRandom", lambda: entropy)
            )
        if tool_name == "circuit_part_author_commit":
            patches.enter_context(mock_patch.object(authoring, "datetime", _ReplayClock))
        if tool_name == "circuit_vision_read":
            patches.enter_context(
                mock_patch.object(
                    visionread,
                    control_image_attribute,
                    capture_control_image,
                )
            )
        raw = await mcp_server.call_tool(tool_name, arguments)
    is_error, result = _tool_result(raw)
    return is_error, result, control_answers[0] if control_answers else None


def _run_step(
    project: Path,
    events_root: Path,
    step: Mapping[str, Any],
    results: dict[str, object],
) -> tuple[str, object, list[dict[str, str]]]:
    step_id = step.get("id")
    step_type = step.get("type")
    if not isinstance(step_id, str) or not isinstance(step_type, str):
        raise ReplayError("each transcript step requires id and type")
    if step_id in results:
        raise ReplayError(f"duplicate transcript step id: {step_id}")
    if step_type == "human_event":
        result = _event_step(project, events_root, step, results)
        return step_id, result, []
    if step_type == "patch_json":
        result = _patch_json(project, step, results)
        return step_id, result, []
    if step_type == "fetch_response":
        resolved = cast(dict[str, Any], _reference(dict(step), results))
        path_value = resolved.get("path")
        content_type = resolved.get("content_type")
        body = resolved.get("body")
        if not all(isinstance(value, str) for value in (path_value, content_type, body)):
            raise ReplayError("fetch_response requires path, content_type, and body strings")
        target = _project_file(project, cast(str, path_value))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(cast(str, body).encode("utf-8"))
        return (
            step_id,
            {
                "path": str(target),
                "url": resolved.get("url"),
                "content_type": content_type,
                "body_prefix": cast(str, body)[:32],
                "sha256": _sha256(target),
            },
            [],
        )
    if step_type == "copy_file":
        resolved = cast(dict[str, Any], _reference(dict(step), results))
        source_value = resolved.get("source")
        target_value = resolved.get("target")
        if not isinstance(source_value, str) or not isinstance(target_value, str):
            raise ReplayError("copy_file requires source and target")
        source = _project_file(project, source_value)
        target = _project_file(project, target_value)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return step_id, {"path": str(target), "sha256": _sha256(target)}, []
    if step_type != "tool":
        raise ReplayError(f"unsupported transcript step type: {step_type}")
    tool_name = step.get("tool")
    arguments_value = step.get("arguments", {})
    if not isinstance(tool_name, str) or not isinstance(arguments_value, dict):
        raise ReplayError("tool steps require tool and arguments")
    arguments = cast(
        dict[str, Any],
        _reference(cast(dict[str, object], arguments_value), results),
    )
    environment = step.get("environment", {})
    if not isinstance(environment, dict):
        raise ReplayError("tool environment must be an object")
    saved: dict[str, str | None] = {}
    keys = {"CIRCUIT_AUTHORING_LANE", "CIRCUIT_LLM_PROFILE", "CIRCUIT_LLM_MODEL"}
    for key in keys:
        saved[key] = os.environ.get(key)
    try:
        for key in keys:
            os.environ.pop(key, None)
        for key, value in cast(dict[str, Any], environment).items():
            if key not in keys or not isinstance(value, str):
                raise ReplayError(f"unsupported tool environment variable: {key}")
            os.environ[key] = value
        is_error, result, private_control_answer = asyncio.run(
            _call_tool(tool_name, arguments, seed=step_id)
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if private_control_answer is not None:
        if not isinstance(result, dict):
            raise ReplayError("vision batch returned no batch path for private control state")
        result_mapping = cast(dict[str, object], result)
        if not isinstance(result_mapping.get("batch_path"), str):
            raise ReplayError("vision batch returned no batch path for private control state")
        _VISION_CONTROL_ANSWERS[cast(str, result_mapping["batch_path"])] = private_control_answer
    expected = step.get("expect", {})
    if not isinstance(expected, dict):
        raise ReplayError("tool expectation must be an object")
    expected = cast(dict[str, Any], expected)
    expected_error = expected.get("is_error", False)
    if not isinstance(expected_error, bool) or is_error != expected_error:
        raise ReplayError(
            f"step {step_id} expected is_error={expected_error}, got {is_error}: {result}"
        )
    predicates = expected.get("predicates", [])
    if not isinstance(predicates, list):
        raise ReplayError("predicates must be a list")
    for predicate_value in cast(list[object], predicates):
        if not isinstance(predicate_value, dict):
            raise ReplayError("predicates must be objects")
        predicate = cast(dict[str, Any], predicate_value)
        _check_predicate(cast(object, result), predicate)
    artifacts = _artifact_checks(
        project,
        expected.get("artifacts"),
        results | {step_id: result},
    )
    return step_id, cast(object, result), artifacts


def run_scenario(
    transcript_path: Path,
    repository: Path,
) -> dict[str, Any]:
    header, steps = _read_transcript(transcript_path)
    _VISION_CONTROL_ANSWERS.clear()
    with tempfile.TemporaryDirectory(prefix="circuit-library-replay-") as work:
        project = Path(work) / "project"
        project.mkdir()
        _ACTIVE_PROJECT.set(project)
        _setup(project, repository, header)
        events_root = project / ".replay-events"
        events_root.mkdir()
        saved_environment = {
            key: os.environ.get(key)
            for key in ("OPENHANDS_PROJECT_DIR", "CIRCUIT_AGENT_EVENTS_DIR", "CIRCUIT_E2E_REPLAY")
        }
        os.environ["OPENHANDS_PROJECT_DIR"] = str(project)
        os.environ["CIRCUIT_AGENT_EVENTS_DIR"] = str(events_root)
        os.environ["CIRCUIT_E2E_REPLAY"] = "1"
        results: dict[str, object] = {}
        artifact_records: list[dict[str, str]] = []
        try:
            for step in steps:
                step_id, result, artifacts = _run_step(project, events_root, step, results)
                results[step_id] = result
                artifact_records.extend(artifacts)
            artifact_manifest: list[dict[str, str]] = []
            for path in sorted(project.rglob("*")):
                if path.is_symlink():
                    raise ReplayError(f"replay produced a symlink artifact: {path}")
                if path.is_file():
                    artifact_manifest.append(
                        {
                            "path": path.relative_to(project).as_posix(),
                            "sha256": _sha256(path),
                        }
                    )
        finally:
            for key, value in saved_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        private_answers = list(_VISION_CONTROL_ANSWERS.values())
        safe_results = cast(
            dict[str, object],
            _redact_private(results, private_answers),
        )
        _VISION_CONTROL_ANSWERS.clear()
        return {
            "scenario": header.get("scenario"),
            "label": header["label"],
            "steps": len(steps),
            "artifacts": artifact_records,
            "artifact_manifest": artifact_manifest,
            "results": safe_results,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transcripts",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests" / "data" / "e2e_replay",
        help="directory containing synthetic JSONL scenarios",
    )
    parser.add_argument("--scenario", action="append", help="scenario filename (repeatable)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="enable live-mode policy; this harness still replays files and never launches agents",
    )
    parser.add_argument("--report", type=Path, help="write the replay summary as JSON")
    args = parser.parse_args()
    if args.live and os.environ.get("CIRCUIT_E2E_LIVE") != "1":
        parser.error("--live requires CIRCUIT_E2E_LIVE=1")
    repository = Path(__file__).resolve().parents[1]
    transcript_root = args.transcripts.resolve()
    if args.scenario:
        transcripts = [transcript_root / name for name in args.scenario]
    else:
        transcripts = sorted(transcript_root.glob("*.jsonl"))
    if not transcripts:
        parser.error(f"no JSONL transcripts found under {transcript_root}")
    summaries: list[dict[str, Any]] = []
    try:
        for path in transcripts:
            summaries.append(run_scenario(path, repository))
        report = {"status": "passed", "scenarios": summaries}
        rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
        if args.report:
            args.report.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
