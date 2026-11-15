"""Regression checks for session isolation and bounded Stop status semantics."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _empty(*_args: Any) -> list[Any]:
    return []


def _no_recall(*_args: Any) -> str:
    return ""


def _problems(
    *_args: Any, problems: tuple[str, ...] = (), warnings: tuple[str, ...] = ()
) -> tuple[list[str], list[str]]:
    return list(problems), list(warnings)


@pytest.fixture()
def hook() -> ModuleType:
    scripts = next(Path(__file__).resolve().parents[1].glob("plugins/*/hooks/scripts"))
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "scoped_stop_hook", scripts / "require_records.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def policy() -> dict[str, Any]:
    return {
        "plugin": "owner",
        "records_dir": "observations/owner",
        "artifact_globs": [],
        "dual_review_globs": ["*.png"],
        "max_stop_denials": 2,
    }


def test_explicit_session_identity_overrides_time(hook: ModuleType) -> None:
    now = time.time()
    fresh = datetime.fromtimestamp(now - 1, UTC).isoformat()
    assert not hook._in_session({"session_id": "other", "recorded_at": fresh}, "current", now - 10)
    assert not hook._in_session({"session_id": "other", "recorded_at": fresh}, None, now - 10)
    assert hook._in_session({"session_id": "current"}, "current", now)


def test_legacy_session_fallback_expires(hook: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    record = {"recorded_at": datetime.fromtimestamp(now - 1, UTC).isoformat()}
    assert hook._in_session(record, "current", now - 10)
    assert not hook._in_session(record, "current", now)
    monkeypatch.setattr(hook.time, "time", lambda: now + 86401)
    assert not hook._in_session(record, "current", now - 10)


def _logs(
    hook: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    logs: dict[str, list[dict[str, Any]]],
) -> None:
    # These unit checks exercise attribution after integrity validation.
    # test_records.py separately exercises actual writers and hashed chains.
    def verify(path: Path, kind: str) -> tuple[list[dict[str, Any]], list[str]]:
        return logs.get(kind, []), []

    monkeypatch.setattr(hook, "verify_log", verify)
    monkeypatch.setattr(hook, "song_deliveries", _empty)
    monkeypatch.setattr(hook, "changed_artifacts", _empty)


def _primary(**overrides: Any) -> dict[str, Any]:
    return {
        "plugin": "owner",
        "session_id": "current",
        "event_id": "a" * 64,
        "recorded_at": datetime.now(UTC).isoformat(),
        "reviewer": "primary",
        "image_path": "diagram.png",
        "image_sha256": "b" * 64,
        **overrides,
    }


def test_other_session_primary_creates_no_dual_debt(
    hook: ModuleType,
    policy: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _logs(hook, monkeypatch, {"vision_review": [_primary(session_id="other")]})
    problems, _ = hook.problems_for({"session_id": "current"}, policy, tmp_path, time.time() - 10)
    assert problems == []


@pytest.mark.parametrize("change", [{"session_id": "other"}, {"image_path": "copy.png"}])
def test_other_session_or_image_blind_cannot_discharge_debt(
    hook: ModuleType,
    policy: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, str],
) -> None:
    primary = _primary()
    blind = _primary(reviewer="blind", event_id="c" * 64, **change)
    _logs(hook, monkeypatch, {"vision_review": [primary, blind]})
    problems, _ = hook.problems_for({"session_id": "current"}, policy, tmp_path, time.time() - 10)
    assert any("needs a blind second review" in p for p in problems)


def test_record_owner_must_match_log_owner(
    hook: ModuleType,
    policy: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _logs(hook, monkeypatch, {"vision_review": [_primary(plugin="other")]})
    problems, _ = hook.problems_for({"session_id": "current"}, policy, tmp_path, time.time() - 10)
    assert any("record plugin differs from owner" in p for p in problems)


def test_legacy_record_attribution_is_unknown(
    hook: ModuleType,
    policy: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _primary()
    legacy.pop("session_id")
    _logs(hook, monkeypatch, {"decision": [legacy]})
    _, warnings = hook.problems_for({"session_id": "current"}, policy, tmp_path, time.time() - 10)
    assert any("legacy records lack session_id" in w for w in warnings)


@pytest.mark.parametrize("change", [{"image_sha256": "d" * 64}, {"image_path": "copy.png"}])
def test_source_event_id_cannot_hide_a_different_image(
    hook: ModuleType,
    policy: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, str],
) -> None:
    _logs(hook, monkeypatch, {"vision_review": [_primary(source_event_id="e" * 64, **change)]})
    observed = _primary(event_id="e" * 64)
    log = tmp_path / policy["records_dir"] / "image-observations.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps(observed) + "\n", encoding="utf-8")
    problems, _ = hook.problems_for({"session_id": "current"}, policy, tmp_path, time.time() - 10)
    assert any("viewed image" in p for p in problems)


def test_stop_budget_allows_exit_with_explicit_gaps(
    hook: ModuleType,
    policy: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(hook, "recall", _no_recall)
    monkeypatch.setattr(hook, "problems_for", partial(_problems, problems=("missing review",)))
    payload = {"session_id": "current"}
    hook.session_start(payload, policy, tmp_path)
    results = [hook.stop(payload, policy, tmp_path) for _ in range(7)]
    assert results == [2, 2, 0, 0, 0, 0, 0]
    assert '"decision": "allow"' in capsys.readouterr().out
    status_path = tmp_path / policy["records_dir"] / ".sessions/current.status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["stop_permission"] == "allow"
    assert status["record_compliance"] == status["verdict"] == "fail"
    assert status["acceptance"] == status["run_completion"] == "unknown"
    assert status["stop_denials"] == 2
    assert status["problems"] == ["missing review"]
    assert (tmp_path / status["checkpoint"]).is_file()


def test_missing_marker_and_warnings_never_imply_completion(
    hook: ModuleType,
    policy: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hook, "recall", _no_recall)
    payload = {"session_id": "current"}
    assert hook.stop(payload, policy, tmp_path) == 0
    path = tmp_path / policy["records_dir"] / "records-status.json"
    status = json.loads(path.read_text(encoding="utf-8"))
    assert status["record_compliance"] == "unknown"
    assert status["warnings"]
    assert hook.stop(payload, policy, tmp_path) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["record_compliance"] == "unknown"
    monkeypatch.setattr(hook, "problems_for", partial(_problems, warnings=("unknown attribution",)))
    assert hook.stop(payload, policy, tmp_path) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["record_compliance"] == "unknown"


def test_clean_stop_only_proves_record_compliance(
    hook: ModuleType,
    policy: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hook, "recall", _no_recall)
    monkeypatch.setattr(hook, "problems_for", _problems)
    payload = {"session_id": "current"}
    hook.session_start(payload, policy, tmp_path)
    assert hook.stop(payload, policy, tmp_path) == 0
    path = tmp_path / policy["records_dir"] / "records-status.json"
    status = json.loads(path.read_text(encoding="utf-8"))
    assert status["record_compliance"] == "pass"
    assert status["acceptance"] == status["run_completion"] == "unknown"


@pytest.mark.parametrize("filename", ["vision-tool-events.jsonl", "image-observations.jsonl"])
def test_malformed_observations_are_not_silent(
    hook: ModuleType,
    policy: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    _logs(hook, monkeypatch, {})
    path = tmp_path / policy["records_dir"] / filename
    path.parent.mkdir(parents=True)
    path.write_text("not-json\n", encoding="utf-8")
    problems, _ = hook.problems_for({"session_id": "current"}, policy, tmp_path, time.time() - 10)
    assert any("malformed lines" in problem for problem in problems)


def test_missing_identity_does_not_prove_compliance(
    hook: ModuleType,
    policy: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _logs(hook, monkeypatch, {})
    problems, warnings = hook.problems_for({}, policy, tmp_path, time.time() - 10)
    assert problems == []
    assert any("session_id is missing" in warning for warning in warnings)


def test_missing_marker_recovery_emits_one_hook_response(
    hook: ModuleType,
    policy: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def pending_recall(*_args: Any) -> str:
        return "A sister song still needs its receipt."

    monkeypatch.setattr(hook, "recall", pending_recall)
    monkeypatch.setattr(hook, "problems_for", partial(_problems, problems=("missing receipt",)))
    assert hook.stop({"session_id": "current"}, policy, tmp_path) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["decision"] == "deny"
    assert "missing receipt" in response["reason"]
