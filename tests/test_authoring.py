from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest

from circuit.authoring import AuthoringError, commit_lane, compare_runs, write_comparison
from test_partspec import _fixture as _part_spec_fixture  # pyright: ignore[reportPrivateUsage]
from vision_fixtures import FIXTURE_IMPRESSION, attach_vision_reads


def _lane_fixture(
    run_dir: Path,
    lane: Literal["a", "b"],
    *,
    manufacturer: str = "Example",
) -> Path:
    lane_dir = run_dir / lane
    lane_dir.mkdir(parents=True)
    spec, _extraction, spec_path, extraction_path = _part_spec_fixture(lane_dir)
    spec.manufacturer = manufacturer
    attach_vision_reads(spec, spec_path, extraction_path, lane=lane)
    return spec_path


def _commit(
    run_dir: Path,
    lane: Literal["a", "b"],
    spec_path: Path,
    *,
    model: str,
    impression: str = FIXTURE_IMPRESSION,
) -> dict[str, Any]:
    return commit_lane(
        run_dir,
        spec_path,
        profile=f"profile-{lane}",
        model=model,
        impression=impression,
    )


def _complete_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_a: str = "model-a",
    model_b: str = "model-b",
    manufacturer_b: str = "Example",
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    run_dir = tmp_path / "authoring" / "example-1" / "run-1"
    run_dir.mkdir(parents=True)
    spec_a = _lane_fixture(run_dir, "a")
    spec_b = _lane_fixture(run_dir, "b", manufacturer=manufacturer_b)
    monkeypatch.setenv("CIRCUIT_AUTHORING_LANE", "a")
    commit_a = _commit(run_dir, "a", spec_a, model=model_a)
    monkeypatch.setenv("CIRCUIT_AUTHORING_LANE", "b")
    commit_b = _commit(run_dir, "b", spec_b, model=model_b)
    return run_dir, commit_a, commit_b


def test_lane_commit_seals_hash_provenance_and_impression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    spec_path = _lane_fixture(run_dir, "a")
    monkeypatch.setenv("CIRCUIT_AUTHORING_LANE", "a")

    with pytest.raises(AuthoringError, match="impression"):
        commit_lane(run_dir, spec_path, impression="Too short.")

    record = _commit(run_dir, "a", spec_path, model="provider/model-a")

    assert record["lane"] == "a"
    assert record["sha256"]
    assert record["committed_at"]
    assert record["profile"] == "profile-a"
    assert record["model"] == "provider/model-a"
    assert record["vision_batches"]
    assert record["impression"] == FIXTURE_IMPRESSION
    sealed = run_dir / "sealed" / "a.json"
    assert sealed.is_file()
    assert (
        json.loads((run_dir / "commits.jsonl").read_text(encoding="utf-8"))["sha256"]
        == (record["sha256"])
    )
    with pytest.raises(AuthoringError, match="already committed"):
        _commit(run_dir, "a", spec_path, model="provider/model-a")


def test_lane_commit_rejects_cross_lane_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    spec_a = _lane_fixture(run_dir, "a")
    spec_b = _lane_fixture(run_dir, "b")
    document = json.loads(spec_a.read_text(encoding="utf-8"))
    other = json.loads(spec_b.read_text(encoding="utf-8"))
    reference = other["package"]["body_length"]["reading"]["vision_read"]
    batch_ref = reference.split("#", maxsplit=1)[0]
    batch_dir = Path(batch_ref).parent
    shutil.copytree(spec_b.parent / batch_dir, spec_a.parent / batch_dir)
    document["package"]["body_length"]["reading"]["vision_read"] = reference
    spec_a.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("CIRCUIT_AUTHORING_LANE", "a")

    with pytest.raises(AuthoringError, match="cross-lane"):
        commit_lane(run_dir, spec_a, impression=FIXTURE_IMPRESSION)


def test_compare_runs_seals_consensus_models_impressions_and_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, commit_a, commit_b = _complete_run(tmp_path, monkeypatch)
    observations = run_dir / "observations" / "circuit" / "authoring-events.jsonl"
    observations.parent.mkdir(parents=True)
    observations.write_text("", encoding="utf-8")

    comparison = compare_runs(run_dir)

    assert comparison.model_diversity == "distinct"
    assert comparison.sealed == {"a": commit_a["sha256"], "b": commit_b["sha256"]}
    assert comparison.impressions == {"a": FIXTURE_IMPRESSION, "b": FIXTURE_IMPRESSION}
    assert comparison.rasterizers == {"a": ["pdftoppm"], "b": ["pdfium"]}
    assert not comparison.disagreements
    assert any(issue.code == "authoring_commit_unobserved" for issue in comparison.issues)

    observations.write_text(
        "".join(json.dumps({"sha256": commit["sha256"]}) + "\n" for commit in (commit_a, commit_b)),
        encoding="utf-8",
    )
    comparison = compare_runs(run_dir)
    assert not comparison.issues
    assert not comparison.disagreements
    path = write_comparison(run_dir, comparison)
    assert json.loads(path.read_text(encoding="utf-8"))["impressions"] == comparison.impressions


def test_compare_runs_loads_control_sidecars_in_a_fresh_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _commit_a, _commit_b = _complete_run(tmp_path, monkeypatch)
    for lane in ("a", "b"):
        spec = json.loads((run_dir / lane / "part.spec.json").read_text(encoding="utf-8"))
        reference = spec["package"]["body_length"]["reading"]["vision_read"]
        batch_reference, _read_id = reference.split("#", maxsplit=1)
        batch_path = run_dir / lane / batch_reference
        payload = json.loads(batch_path.read_text(encoding="utf-8"))
        sidecar = (batch_path.parent / payload["control_state_path"]).resolve()
        assert sidecar.parent == run_dir / ".vision-control"

    code = (
        "from pathlib import Path; import sys; "
        "from circuit.authoring import compare_runs; "
        "result=compare_runs(Path(sys.argv[1])); "
        "assert result.model_diversity == 'distinct' and not result.issues"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(run_dir)],
        text=True,
        capture_output=True,
        cwd=Path(__file__).parents[1],
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("model_a", "model_b", "expected"),
    [
        ("model-a", "model-a", "same"),
        ("unknown", "model-b", "unknown"),
    ],
)
def test_compare_reports_same_and_unknown_model_diversity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_a: str,
    model_b: str,
    expected: Literal["same", "unknown"],
) -> None:
    run_dir, _commit_a, _commit_b = _complete_run(
        tmp_path,
        monkeypatch,
        model_a=model_a,
        model_b=model_b,
    )

    assert compare_runs(run_dir).model_diversity == expected


def test_compare_reports_authored_value_disagreements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _commit_a, _commit_b = _complete_run(
        tmp_path,
        monkeypatch,
        manufacturer_b="Other",
    )

    comparison = compare_runs(run_dir)

    assert any(item.pointer == "/manufacturer" for item in comparison.disagreements)


def test_authoring_commit_requires_lane_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    spec_path = _lane_fixture(run_dir, "a")
    monkeypatch.delenv("CIRCUIT_AUTHORING_LANE", raising=False)

    with pytest.raises(AuthoringError, match="CIRCUIT_AUTHORING_LANE"):
        commit_lane(run_dir, spec_path, lane="a", impression=FIXTURE_IMPRESSION)
