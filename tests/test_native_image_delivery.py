"""Producer/observer/writer integration; delivered bytes do not prove perception."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import json
import os
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = next(REPO.glob("plugins/*/hooks/scripts"))
PLUGIN = SCRIPTS.parents[1].name


@pytest.fixture()
def native() -> tuple[ModuleType, Callable[..., dict[str, object]], ModuleType]:
    fixtures = runpy.run_path(
        str(REPO / "tests" / ("test_doc_records.py" if PLUGIN == "doc" else "test_records.py"))
    )
    return (
        cast(ModuleType, fixtures["records"]),
        cast(Callable[..., dict[str, object]], fixtures["_vision"]),
        cast(ModuleType, fixtures.get("_vrp", fixtures["HOOK_RECORDS"])),
    )


def _claim(path: Path, owner: str) -> dict[str, object]:
    path.write_bytes(b"synthetic W1 image bytes; no product acceptance")
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "owner_plugin": owner,
        "origin": "renderer",
    }


def _response(path: Path, claim: dict[str, object] | None) -> dict[str, object]:
    # SDK image conversion keeps the data URL but can discard MCP _meta.
    metadata = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "attached": True,
        "identity_status": "bound",
        "image_identity": claim,
    }
    return {
        "content": [
            {"type": "text", "text": json.dumps({"inline_images": [metadata]})},
            {
                "type": "image",
                "image_urls": [
                    "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
                ],
            },
        ]
    }


def _observe(root: Path, path: Path, response: dict[str, object]) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "record_image_observation.py")],
        input=json.dumps(
            {
                "tool_name": "ux_render" if PLUGIN == "ux" else "file_editor",
                "session_id": "reader",
                "tool_input": {"command": "view", "path": str(path)},
                "tool_response": response,
            }
        ),
        text=True,
        capture_output=True,
        env={**os.environ, "OPENHANDS_PROJECT_DIR": str(root)},
        check=True,
    )
    assert not result.stderr, result.stderr
    log = root / "observations" / PLUGIN / "image-observations.jsonl"
    return cast(dict[str, object], json.loads(log.read_text(encoding="utf-8").splitlines()[-1]))


def _stop_hook(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setattr(sys, "path", [str(SCRIPTS), *sys.path])
    spec = importlib.util.spec_from_file_location(
        "native_delivery_stop", SCRIPTS / "require_records.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("derived", [False, True])
@pytest.mark.parametrize("repeat", [False, True])
def test_native_observer_and_writer_bind_original_or_auxiliary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native: tuple[ModuleType, Callable[..., dict[str, object]], ModuleType],
    derived: bool,
    repeat: bool,
) -> None:
    writer, vision, vrp = native
    monkeypatch.delenv("OPENHANDS_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("OPENHANDS_SESSION_ID", raising=False)
    image = tmp_path / "detail.png"
    claim = _claim(image, PLUGIN)
    parent = _claim(tmp_path / "source.png", PLUGIN)
    if derived:
        claim = {
            **claim,
            "owner_plugin": "ux",
            "origin": "derived",
            "operation": "crop",
            "parents": [parent],
        }
    observation = _observe(tmp_path, image, _response(image, claim))
    if repeat:
        again = _observe(tmp_path, image, _response(image, claim))
        assert again["event_id"] != observation["event_id"]
    assert observation["image_identity"] == claim
    assert observation["delivery_evidence"] == "inline_bytes"
    payload = vision(image_path="detail.png", session_id="reader")
    for entry in cast(list[dict[str, object]], payload["claims"]):
        entry["artifact"] = "detail.png"
    review = writer.record_vision_review(payload, tmp_path)["record"]
    delivery = review["image_delivery"]
    assert delivery["status"] == "bound"
    assert delivery["image_identity"] == claim
    assert delivery["observation_event_id"] == observation["event_id"]
    hook = _stop_hook(monkeypatch)
    if repeat:
        _observe(tmp_path, image, _response(image, claim))
    assert hook._auxiliary_review(review, tmp_path) is derived
    problems, _ = hook._dual_review_problems(
        {"dual_review_globs": ["*.png"]},
        {"vision_review": [review], "review_reconcile": []},
        "reader",
        tmp_path,
    )
    assert bool(problems) is not derived
    keys, _ = hook._observation_keys(observation, tmp_path, PLUGIN)
    subject = parent if derived else claim
    assert keys == [(subject["path"], subject["sha256"])]
    assert (
        vrp.verify_log(
            tmp_path / "observations" / PLUGIN / "vision-reviews.jsonl", "vision_review"
        )[1]
        == []
    )


@pytest.mark.parametrize("extra_path", [False, True])
def test_identical_inline_bytes_keep_each_unambiguous_metadata_path(
    tmp_path: Path,
    native: tuple[ModuleType, Callable[..., dict[str, object]], ModuleType],
    extra_path: bool,
) -> None:
    _, _, vrp = native
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    response_a = _response(first, _claim(first, PLUGIN))
    response_b = _response(second, _claim(second, PLUGIN))
    content = cast(list[object], response_a["content"]) + cast(list[object], response_b["content"])
    if extra_path:
        third = tmp_path / "third.png"
        content += cast(list[object], _response(third, _claim(third, PLUGIN))["content"])[:1]
    log = tmp_path / "delivery.jsonl"
    assert vrp.capture_image_delivery(
        {"tool_name": "render", "session_id": "reader", "tool_response": {"content": content}},
        tmp_path,
        log,
    )
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    if extra_path:
        assert all(
            row["image_path"] is None and row["identity_status"] == "unknown" for row in rows
        )
    else:
        assert {row["image_path"] for row in rows} == {str(first), str(second)}
        assert all(row["identity_status"] == "bound" for row in rows)


@pytest.mark.parametrize("use_action_id", [False, True])
def test_inline_delivery_retains_actor_and_parent_call(
    tmp_path: Path,
    native: tuple[ModuleType, Callable[..., dict[str, object]], ModuleType],
    use_action_id: bool,
) -> None:
    _, _, vrp = native
    image = tmp_path / "image.png"
    claim = _claim(image, PLUGIN)
    actor = {
        "agent_name": "reviewer",
        "parent_id": "parent",
        "call_id": "parent-call",
        "action_id" if use_action_id else "tool_call_id": "native-call",
    }
    log = tmp_path / "delivery.jsonl"
    assert vrp.capture_image_delivery(
        {
            **actor,
            "tool_name": "render",
            "session_id": "reader",
            "tool_response": _response(image, claim),
        },
        tmp_path,
        log,
    )
    row = json.loads(log.read_text(encoding="utf-8"))
    assert row["actor"] == actor
    assert row["tool_call_id"] == "native-call"


def test_home_relative_observation_override_binds_the_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native: tuple[ModuleType, Callable[..., dict[str, object]], ModuleType],
) -> None:
    writer, vision, vrp = native
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(f"{PLUGIN.upper()}_IMAGE_OBSERVATIONS", "~/deliveries.jsonl")
    monkeypatch.delenv("OPENHANDS_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("OPENHANDS_SESSION_ID", raising=False)
    image = tmp_path / "image.png"
    claim = _claim(image, PLUGIN)
    assert vrp.capture_image_delivery(
        {"tool_name": "render", "session_id": "reader", "tool_response": _response(image, claim)},
        tmp_path,
        tmp_path / "deliveries.jsonl",
    )
    payload = vision(image_path="image.png", session_id="reader")
    for entry in cast(list[dict[str, object]], payload["claims"]):
        entry["artifact"] = "image.png"
    row = writer.record_vision_review(payload, tmp_path)["record"]
    assert row["image_delivery"]["status"] == "bound"
    assert row["image_delivery"]["image_identity"] == claim


@pytest.mark.parametrize("damage", ["none", "session", "integrity", "source"])
def test_writer_can_review_the_delivered_revision_after_regeneration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native: tuple[ModuleType, Callable[..., dict[str, object]], ModuleType],
    damage: str,
) -> None:
    writer, vision, _ = native
    monkeypatch.delenv("OPENHANDS_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("OPENHANDS_SESSION_ID", raising=False)
    image = tmp_path / "detail.png"
    claim = _claim(image, PLUGIN)
    observation = _observe(tmp_path, image, _response(image, claim))
    if damage == "integrity":
        log = tmp_path / "observations" / PLUGIN / "image-observations.jsonl"
        log.write_text(
            json.dumps({**observation, "image_sha256": "f" * 64}) + "\n", encoding="utf-8"
        )
    image.write_bytes(b"new W1 render, old delivery remains a separate obligation")
    payload = vision(
        image_path="detail.png",
        session_id="foreign" if damage == "session" else "reader",
        source_event_id="f" * 64 if damage == "source" else observation["event_id"],
    )
    for entry in cast(list[dict[str, object]], payload["claims"]):
        entry["artifact"] = "detail.png"
    row = writer.record_vision_review(payload, tmp_path)["record"]
    expected = (
        claim["sha256"] if damage == "none" else hashlib.sha256(image.read_bytes()).hexdigest()
    )
    assert row["image_sha256"] == expected
    assert row["image_delivery"]["status"] == ("bound" if damage == "none" else "unknown")


@pytest.mark.parametrize("damage", ["utf8", "depth", "io"])
def test_unreadable_delivery_log_preserves_a_review_with_unknown_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native: tuple[ModuleType, Callable[..., dict[str, object]], ModuleType],
    damage: str,
) -> None:
    writer, vision, _ = native
    monkeypatch.delenv("OPENHANDS_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("OPENHANDS_SESSION_ID", raising=False)
    image = tmp_path / "detail.png"
    claim = _claim(image, PLUGIN)
    observation = _observe(tmp_path, image, _response(image, claim))
    log = tmp_path / "observations" / PLUGIN / "image-observations.jsonl"
    if damage == "utf8":
        log.write_bytes(b"\xff\xfe")
    elif damage == "depth":
        log.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
    else:
        read_text = Path.read_text

        def unreadable(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == log:
                raise PermissionError("delivery log cannot be read")
            return read_text(path, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", unreadable)
    image.write_bytes(b"new W1 render; unreadable evidence cannot bind the old revision")
    payload = vision(
        image_path="detail.png", session_id="reader", source_event_id=observation["event_id"]
    )
    for entry in cast(list[dict[str, object]], payload["claims"]):
        entry["artifact"] = "detail.png"
    row = writer.record_vision_review(payload, tmp_path)["record"]
    assert row["image_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert row["image_delivery"]["status"] == "unknown"


def test_file_editor_inherits_only_exact_ux_producer_receipt(
    tmp_path: Path,
    native: tuple[ModuleType, Callable[..., dict[str, object]], ModuleType],
) -> None:
    _, _, vrp = native
    source = tmp_path / "source.png"
    parent = _claim(source, PLUGIN)
    image = tmp_path / "detail.png"
    claim = {**_claim(image, "ux"), "origin": "derived", "operation": "crop", "parents": [parent]}
    producer_log = tmp_path / "observations/ux/image-observations.jsonl"
    assert vrp.capture_image_delivery(
        {
            "tool_name": "ux_image_detail",
            "session_id": "producer",
            "tool_response": _response(image, claim),
        },
        tmp_path,
        producer_log,
    )
    response = _response(image, None)
    response["content"] = cast(list[object], response["content"])[1:]
    observation = _observe(tmp_path, image, response)
    if PLUGIN == "ux":
        # UX has its existing typed observer; cross-plugin inheritance is the new path.
        assert observation["image_sha256"] == claim["sha256"]
        return
    assert observation["image_identity"] == claim
    image.write_bytes(b"replacement W1 that has no producer receipt")
    response = _response(image, None)
    response["content"] = cast(list[object], response["content"])[1:]
    assert _observe(tmp_path, image, response)["identity_status"] == "unknown"


@pytest.mark.parametrize("damage", ["owner", "parent", "copy", "metadata"])
def test_invalid_native_ancestry_stays_unknown(
    tmp_path: Path,
    damage: str,
    native: tuple[ModuleType, Callable[..., dict[str, object]], ModuleType],
) -> None:
    _, _, vrp = native
    image = tmp_path / "detail.png"
    parent = _claim(tmp_path / "source.png", PLUGIN)
    claim = {**_claim(image, "ux"), "origin": "derived", "operation": "crop", "parents": [parent]}
    if damage == "owner":
        parent["owner_plugin"] = ["forged"]
    elif damage == "parent":
        (tmp_path / "source.png").write_bytes(b"parent changed")
    elif damage == "copy":
        claim["path"] = str(tmp_path / "unrelated.png")
    response = _response(image, claim)
    if damage == "metadata":
        response["content"] = cast(list[object], response["content"])[1:]
    log = tmp_path / "observations" / PLUGIN / "image-observations.jsonl"
    assert vrp.capture_image_delivery(
        {
            "tool_name": "file_editor",
            "session_id": "reader",
            "tool_input": {"command": "view", "path": str(image)},
            "tool_response": response,
        },
        tmp_path,
        log,
    )
    row = json.loads(log.read_text(encoding="utf-8"))
    assert row["identity_status"] == "unknown"
    assert row["image_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
    review = {"image_path": str(image), "image_sha256": row["image_sha256"], "session_id": "other"}
    assert vrp.review_image_delivery(review, tmp_path, PLUGIN)["status"] == "unknown"


def test_local_mcp_attachment_preserves_producer_metadata(
    tmp_path: Path,
) -> None:
    if PLUGIN in {"ux", "bard", "doc"}:
        pytest.skip("This plugin consumes UX receipts; its producer has separate coverage")
    server = importlib.import_module(f"{PLUGIN}.mcp_server")
    path = tmp_path / "W1.png"
    _claim(path, PLUGIN)
    if PLUGIN == "dashboard":
        blocks = server.render_content({"images": [str(path)]})
    elif PLUGIN == "firmware":
        blocks = server._inline_images({"written": [str(path)]})
    elif PLUGIN == "fpga":
        blocks = server._image_contents({"images": [str(path)]})
    elif PLUGIN == "sim":
        blocks = server._payload_images(
            {
                "plots": [
                    {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                ]
            },
            tmp_path,
        )[0]
    elif PLUGIN == "circuit":
        blocks = [importlib.import_module("circuit.mcp_collect")._image_content(path)]
    elif PLUGIN == "wire":
        blocks = [server.image_content(path, renderer=True)]
    else:
        blocks = [getattr(server, "_image_content" if PLUGIN == "mech" else "image_content")(path)]
    delivered = server._with_image_metadata(blocks)
    entries = [
        json.loads(block.text)["inline_images"]
        for block in delivered
        if getattr(block, "type", None) == "text" and '"inline_images"' in block.text
    ]
    assert entries
    assert entries[-1][0]["image_identity"]["owner_plugin"] == PLUGIN
    assert entries[-1][0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    sdk_content: list[dict[str, object]] = []
    for block in delivered:
        if block.type == "image":
            sdk_content.append(
                {"type": "image", "image_urls": [f"data:{block.mime_type};base64,{block.data}"]}
            )
        else:
            sdk_content.append({"type": "text", "text": block.text})
    observation = _observe(tmp_path, path, {"content": sdk_content})
    assert observation["identity_status"] == "bound"
    assert cast(dict[str, object], observation["image_identity"])["owner_plugin"] == PLUGIN
