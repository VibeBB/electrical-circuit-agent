"""Canonical owner/ancestry scope checks; synthetic bytes never prove perception."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture()
def image_scope_hook(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    scripts = next(Path(__file__).resolve().parents[1].glob("plugins/*/hooks/scripts"))
    monkeypatch.setattr(sys, "path", [str(scripts), *sys.path])
    spec = importlib.util.spec_from_file_location(
        "image_scope_hook", scripts / "require_records.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _original(root: Path, name: str, owner: str = "ux") -> dict[str, Any]:
    path = root / name
    if not path.exists():
        path.write_bytes(name.encode())
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "owner_plugin": owner,
        "origin": "renderer" if owner == "ux" else "liaison-response",
    }


def _receipt(claim: dict[str, Any]) -> dict[str, Any]:
    record = {
        "sequence": 1,
        "tool_name": "ux_render",
        "session_id": "native",
        "image_path": claim["path"],
        "image_sha256": claim["sha256"],
        "image_identity": claim,
        "identity_status": "bound",
        "delivery_evidence": "inline_bytes",
    }
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    return {**record, "event_id": hashlib.sha256(encoded).hexdigest()}


def _auxiliary(root: Path, parents: list[dict[str, Any]]) -> dict[str, Any]:
    claim = _original(root, "detail.png")
    return _receipt({**claim, "origin": "derived", "operation": "composite", "parents": parents})


def test_multiple_owners_owe_only_their_originals(
    tmp_path: Path, image_scope_hook: ModuleType
) -> None:
    ux = _original(tmp_path, "ux.png")
    bard = _original(tmp_path, "bard.png", "bard")
    auxiliary = _auxiliary(tmp_path, [ux, bard])
    for owner, claim in (("ux", ux), ("bard", bard)):
        keys, _ = image_scope_hook._observation_keys(auxiliary, tmp_path, owner)
        assert keys == [(claim["path"], claim["sha256"])]
    assert image_scope_hook._observation_keys(auxiliary, tmp_path, "wire")[0] == []


@pytest.mark.parametrize("damage", ["receipt", "parent", "child", "copy", "duplicate"])
def test_unverified_auxiliary_keeps_an_explicit_obligation(
    tmp_path: Path, image_scope_hook: ModuleType, damage: str
) -> None:
    source = _original(tmp_path, "source.png")
    auxiliary = _auxiliary(tmp_path, [source])
    if damage == "receipt":
        auxiliary["image_identity"]["parents"][0]["owner_plugin"] = "wire"
    elif damage == "parent":
        (tmp_path / "source.png").write_bytes(b"new original")
    elif damage == "child":
        (tmp_path / "detail.png").write_bytes(b"unreceipted detail revision")
    elif damage == "copy":
        auxiliary["image_path"] = str(tmp_path / "copy.png")
    else:
        claim = auxiliary["image_identity"]
        auxiliary = _receipt({**claim, "parents": [source, source]})
    originals = image_scope_hook._current_originals([_receipt(source)], tmp_path)
    keys, warning = image_scope_hook._observation_keys(auxiliary, tmp_path, "ux", originals)
    assert keys and warning and "unknown" in warning
    assert keys[0][0] == auxiliary["image_path"]


def test_current_original_receipt_retires_old_auxiliary_without_hiding_review(
    tmp_path: Path, image_scope_hook: ModuleType
) -> None:
    source = _original(tmp_path, "source.png")
    auxiliary = _auxiliary(tmp_path, [source])
    (tmp_path / "source.png").write_bytes(b"new original revision")
    replacement = _original(tmp_path, "source.png")
    originals = image_scope_hook._current_originals([_receipt(replacement)], tmp_path)
    keys, warning = image_scope_hook._observation_keys(auxiliary, tmp_path, "ux", originals)
    assert keys == [(replacement["path"], replacement["sha256"])]
    assert keys[0][1] != source["sha256"] and warning and "superseded" in warning


def test_owner_change_cannot_retire_an_old_auxiliary(
    tmp_path: Path, image_scope_hook: ModuleType
) -> None:
    source = _original(tmp_path, "source.png")
    auxiliary = _auxiliary(tmp_path, [source])
    (tmp_path / "source.png").write_bytes(b"new original revision")
    replacement = _original(tmp_path, "source.png", "bard")
    originals = image_scope_hook._current_originals([_receipt(replacement)], tmp_path)
    _, warning = image_scope_hook._observation_keys(auxiliary, tmp_path, "ux", originals)
    assert warning and "unknown" in warning


def test_conflicting_current_owners_never_choose_one(
    tmp_path: Path, image_scope_hook: ModuleType
) -> None:
    candidates = [_original(tmp_path, "source.png", owner) for owner in ("ux", "bard")]
    assert image_scope_hook._current_originals([_receipt(c) for c in candidates], tmp_path) == {}


def test_legacy_observation_retains_the_revision_that_was_viewed(
    tmp_path: Path, image_scope_hook: ModuleType
) -> None:
    source = _original(tmp_path, "source.png")
    observed = {"image_path": source["path"], "image_sha256": source["sha256"]}
    (tmp_path / "source.png").write_bytes(b"replacement never viewed")
    keys, warning = image_scope_hook._observation_keys(observed, tmp_path, "ux", {})
    assert keys == [(source["path"], source["sha256"])]
    assert warning and "unknown" in warning


def test_redelivery_does_not_erase_an_earlier_original_review(
    tmp_path: Path, image_scope_hook: ModuleType
) -> None:
    source = _original(tmp_path, "source.png")
    observed = _receipt(source)
    (tmp_path / "source.png").write_bytes(b"replacement delivered later")
    replacement = _original(tmp_path, "source.png")
    originals = image_scope_hook._current_originals([_receipt(replacement)], tmp_path)
    keys, warning = image_scope_hook._observation_keys(observed, tmp_path, "ux", originals)
    assert keys == [(source["path"], source["sha256"])]
    assert warning and "unknown" in warning
