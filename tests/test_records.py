"""VibeBB Record Protocol: typed writers, stdlib hook mirror and Stop enforcement."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from circuit import records

SCRIPTS = Path(__file__).parents[1] / "plugins" / "circuit" / "hooks" / "scripts"
HOOK = SCRIPTS / "require_records.py"

IMPRESSION = (
    "The schematic sheet reads as a coherent design document: the power tree and the "
    "signal nets are visibly separated, and every connector carries a unique reference "
    "designator. What worries me is the 3.3 V sense net, which runs alongside the "
    "switcher output for most of its length and could pick up switching noise. A "
    "fabricator would still be able to assemble the board from the drawings alone, "
    "although the mounting note is small. Next I would re-route the sense trace along "
    "the far edge of the board and re-render to confirm the separation. Overall the "
    "sheet communicates intent well, and the remaining risk sits in one routing choice."
)
IMPRESSION_JA = (
    "電源ツリーと信号ネットが明確に分かれており、図面だけで実装まで進められる構成だと感じた。"
    "一方でセンス線がスイッチャ出力と長く並走しており、スイッチングノイズを拾う懸念が残る。"
    "製造現場の作業者はリファレンスを迷わず読めるが、実装注記の文字が小さく見落とされやすい。"
    "次の工程ではセンス線を基板の反対側へ移し、再描画して分離が保たれているかを確認したい。"
    "全体としては設計意図が伝わる図面であり、誤読のリスクは注記の大きさに集中している。"
    "コネクタの嵌合方向は矢印で示されているが、上面視か下面視かの明記が無いので追記したい。"
    "実装面の基準点はボード外形に揃っており、治具への展開もそのまま行えると判断した。"
    "ただしシールドの終端処理は図から読み取れず、仕様書を別途参照させる必要がある。"
    "白黒印刷で色識別が失われた場合でも、ネット名とピン番号の併記によって配線の取り違えは防げると考える。"
    "最後に、検査工程で導通試験の手順を参照できるよう、試験点の一覧を図面の余白へ追加することを提案したい。"
)


def _hook_module() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("_records", SCRIPTS / "_records.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK_RECORDS = _hook_module()


def _decision(evidence_path: str) -> dict[str, Any]:
    return {
        "id": "sense-trace-routing",
        "stage": "layout",
        "question": "Where should the 3.3 V sense trace run on the board?",
        "principles": [
            "Inductive coupling falls with separation and shorter parallel run length",
            "IPC-2221 separation of power and signal conductors",
        ],
        "options": [
            {"name": "same-edge", "pros": ["shortest"], "cons": ["noise coupling"]},
            {"name": "far-edge", "pros": ["separation"], "cons": ["+40 mm length"]},
        ],
        "chosen": "far-edge",
        "rationale": (
            "The sense net feeds an ADC whose LSB is about 0.8 mV, while the switcher "
            "output carries several amperes at 20 kHz; mutual inductance along a 30 mm "
            "parallel run would inject more than an LSB, so physical separation is the "
            "cheapest fix."
        ),
        "evidence": [{"path": evidence_path}, {"reference": "IPC-2221B section 6"}],
        "assumptions": ["switcher frequency is 20 kHz"],
        "unknowns": ["actual switcher current ripple"],
        "risks": ["longer trace adds 40 mm of copper"],
        "revisit_when": "a measured ADC noise floor exceeds 2 LSB",
    }


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "board.kicad_sch").write_text("(kicad_sch (version 1))\n", encoding="utf-8")
    (tmp_path / "out" / "board-top.png").write_bytes(b"\x89PNG fake")
    return tmp_path


def test_impression_rules() -> None:
    assert records.impression_is_prose(IMPRESSION) == IMPRESSION
    assert records.impression_is_prose(IMPRESSION_JA) == IMPRESSION_JA
    with pytest.raises(ValueError, match="characters"):
        records.impression_is_prose("Looks fine. No issues. Done.")
    with pytest.raises(ValueError, match="sentences"):
        records.impression_is_prose("x" * 500 + ". y")
    with pytest.raises(ValueError, match="repeats"):
        records.impression_is_prose("The drawing is fine and readable overall. " * 12)
    assert records.sentence_count("Supply is 3.3 V and 5.0 V") == 0
    assert HOOK_RECORDS.impression_errors(IMPRESSION) == []
    assert HOOK_RECORDS.impression_errors(IMPRESSION_JA) == []
    assert HOOK_RECORDS.impression_errors("Looks fine. No issues. Done.")


def test_record_impression_binds_artifacts(workspace: Path) -> None:
    result = records.record_impression(
        {"stage": "layout", "artifacts": ["out"], "impression": IMPRESSION}
    )
    record = result["record"]
    assert record["artifacts"] == [
        {"path": "out", "sha256": records.tree_sha256(workspace / "out")}
    ]
    assert record["sequence"] == 1
    assert HOOK_RECORDS.record_errors("stage_impression", record) == []
    assert HOOK_RECORDS.tree_sha256(workspace / "out") == records.tree_sha256(workspace / "out")
    with pytest.raises(ValueError, match="outside the workspace"):
        records.record_impression(
            {"stage": "layout", "artifacts": ["/etc/passwd"], "impression": IMPRESSION}
        )
    with pytest.raises(ValueError, match="does not exist"):
        records.record_impression(
            {"stage": "layout", "artifacts": ["out/missing.kicad_sch"], "impression": IMPRESSION}
        )


def test_record_decision_requires_principled_choice(workspace: Path) -> None:
    result = records.record_decision(_decision("out/board.kicad_sch"))
    record = result["record"]
    digest = hashlib.sha256((workspace / "out" / "board.kicad_sch").read_bytes()).hexdigest()
    assert record["evidence"][0] == {"path": "out/board.kicad_sch", "sha256": digest}
    assert record["evidence"][1]["reference"].startswith("IPC")
    assert HOOK_RECORDS.record_errors("decision", record) == []
    for key, value in (
        ("options", _decision("out/board.kicad_sch")["options"][:1]),
        ("chosen", "teleport"),
        ("rationale", "because"),
        ("principles", []),
        ("principles", ["vibes"]),
        ("risks", []),
        ("evidence", []),
    ):
        bad = _decision("out/board.kicad_sch") | {key: value}
        with pytest.raises(ValidationError):
            records.record_decision(bad)
    with pytest.raises(ValidationError):
        records.record_decision(_decision("out/board.kicad_sch") | {"surprise": 1})


def test_record_vision_review(workspace: Path) -> None:
    result = records.record_vision_review(
        {
            "image_path": "out/board-top.png",
            "model": "openhands/kimi-k3",
            "checklist": "board-top",
            "findings": [{"category": "pin1_mark", "severity": "warning", "note": "U4"}],
            "impression": IMPRESSION,
        }
    )
    record = result["record"]
    assert record["image_sha256"] == hashlib.sha256(b"\x89PNG fake").hexdigest()
    assert HOOK_RECORDS.record_errors("vision_review", record) == []
    with pytest.raises(ValidationError, match="image_path or source_event_id"):
        records.record_vision_review({"model": "m", "checklist": "x", "impression": IMPRESSION})
    event = records.record_vision_review(
        {"source_event_id": "a" * 64, "model": "m", "checklist": "x", "impression": IMPRESSION}
    )
    assert event["record"].get("image_sha256") is None


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        ("decision", "principles"),
        ("decision", "options"),
        ("decision", "rationale"),
        ("decision", "evidence"),
        ("decision", "risks"),
        ("decision", "revisit_when"),
        ("decision", "unknowns"),
        ("stage_impression", "impression"),
        ("stage_impression", "artifacts"),
        ("vision_review", "impression"),
        ("vision_review", "model"),
        ("vision_review", "recorded_at"),
        ("vision_review", "event_id"),
    ],
)
def test_hook_mirror_rejects_mutations(workspace: Path, kind: str, field: str) -> None:
    writers = {
        "decision": lambda: records.record_decision(_decision("out/board.kicad_sch")),
        "stage_impression": lambda: records.record_impression(
            {"stage": "layout", "artifacts": ["out"], "impression": IMPRESSION}
        ),
        "vision_review": lambda: records.record_vision_review(
            {
                "image_path": "out/board-top.png",
                "model": "m",
                "checklist": "board-top",
                "impression": IMPRESSION,
            }
        ),
    }
    record = dict(writers[kind]()["record"])
    assert HOOK_RECORDS.record_errors(kind, record) == []
    del record[field]
    assert HOOK_RECORDS.record_errors(kind, record)
    record[field] = ""
    assert HOOK_RECORDS.record_errors(kind, record)


def _hook(mode: str, root: Path, session: str = "s1") -> subprocess.CompletedProcess[str]:
    env = dict(os.environ) | {"OPENHANDS_PROJECT_DIR": str(root)}
    return subprocess.run(
        [sys.executable, str(HOOK), mode],
        input=json.dumps({"session_id": session, "working_dir": str(root)}),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _status(root: Path) -> dict[str, Any]:
    path = root / records.RECORDS_DIR / "records-status.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_stop_without_marker_allows(tmp_path: Path) -> None:
    assert _hook("stop", tmp_path).returncode == 0


def test_stop_clean_session_passes(tmp_path: Path) -> None:
    assert _hook("session-start", tmp_path).returncode == 0
    result = _hook("stop", tmp_path)
    assert result.returncode == 0, result.stdout
    assert _status(tmp_path)["verdict"] == "pass"


def test_stop_enforces_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    assert _hook("session-start", tmp_path).returncode == 0
    out = tmp_path / "out"
    out.mkdir()
    (out / "board.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    (out / "board-top.png").write_bytes(b"png")
    log = tmp_path / records.RECORDS_DIR
    event = {"event_id": "b" * 64, "question": "is it legible?", "session_id": "s1"}
    (log / "vision-tool-events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    denied = _hook("stop", tmp_path)
    assert denied.returncode == 2
    payload = json.loads(denied.stdout)
    assert payload["decision"] == "deny"
    assert "out/board.kicad_sch" in payload["additionalContext"]
    assert "vision tool event" in payload["additionalContext"]
    assert "no decision record" in payload["additionalContext"]
    assert _status(tmp_path)["verdict"] == "fail"

    records.record_decision(_decision("out/board.kicad_sch"))
    records.record_impression({"stage": "layout", "artifacts": ["out"], "impression": IMPRESSION})
    records.record_vision_review(
        {"source_event_id": "b" * 64, "model": "m", "checklist": "q", "impression": IMPRESSION}
    )
    passed = _hook("stop", tmp_path)
    assert passed.returncode == 0, passed.stdout
    assert _status(tmp_path)["verdict"] == "pass"

    (out / "board.kicad_sch").write_text("(kicad_sch v2)\n", encoding="utf-8")
    stale = _hook("stop", tmp_path)
    assert stale.returncode == 2
    assert "no fresh stage_impression" in json.loads(stale.stdout)["reason"]

    released = _hook("stop", tmp_path)
    assert released.returncode == 0
    assert json.loads(released.stdout)["decision"] == "allow"
    assert "still unmet" in json.loads(released.stdout)["additionalContext"]


def test_stop_flags_tampered_and_malformed_lines(tmp_path: Path) -> None:
    assert _hook("session-start", tmp_path).returncode == 0
    log = tmp_path / records.RECORDS_DIR
    forged = {
        "schema_version": 1,
        "kind": "stage_impression",
        "plugin": "circuit",
        "sequence": 1,
        "event_id": "c" * 64,
        "recorded_at": "2999-01-01T00:00:00+00:00",
        "stage": "layout",
        "artifacts": [{"path": "out", "sha256": "d" * 64}],
        "impression": "ok.",
    }
    (log / "impressions.jsonl").write_text(json.dumps(forged) + "\n{not json\n", encoding="utf-8")
    result = _hook("stop", tmp_path)
    assert result.returncode == 2
    reason = json.loads(result.stdout)["reason"]
    assert "malformed" in reason
    assert "invalid stage_impression" in reason


def test_viewed_image_needs_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    assert _hook("session-start", tmp_path).returncode == 0
    log = tmp_path / records.RECORDS_DIR
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpg")
    digest = hashlib.sha256(b"jpg").hexdigest()
    observation = {
        "event_id": "e" * 64,
        "image_path": str(image),
        "image_sha256": digest,
        "session_id": "s1",
    }
    (log / "image-observations.jsonl").write_text(json.dumps(observation) + "\n", encoding="utf-8")
    assert _hook("stop", tmp_path).returncode == 2
    records.record_vision_review(
        {
            "image_path": "photo.jpg",
            "model": "m",
            "checklist": "intake-image",
            "impression": IMPRESSION,
        }
    )
    assert _hook("stop", tmp_path).returncode == 0


def test_records_policy_matches_core() -> None:
    policy = HOOK_RECORDS.load_policy(SCRIPTS.parents[1])
    assert policy["plugin"] == records.PLUGIN
    assert policy["records_dir"] == records.RECORDS_DIR.as_posix()
    assert HOOK_RECORDS.IMPRESSION_MIN_CHARS == records.IMPRESSION_MIN_CHARS
    assert HOOK_RECORDS.IMPRESSION_MIN_SENTENCES == records.IMPRESSION_MIN_SENTENCES
    assert HOOK_RECORDS.RATIONALE_MIN_CHARS == records.RATIONALE_MIN_CHARS
    assert HOOK_RECORDS.LOG_FILES == records.LOG_FILES
    assert set(records.Severity.__args__) == HOOK_RECORDS.SEVERITIES
    hooks = json.loads((SCRIPTS.parent / "hooks.json").read_text(encoding="utf-8"))
    for event, mode in (("session_start", "session-start"), ("stop", "stop")):
        commands = [h["command"] for g in hooks[event] for h in g["hooks"]]
        assert any(f'require_records.py" {mode}' in c for c in commands)
