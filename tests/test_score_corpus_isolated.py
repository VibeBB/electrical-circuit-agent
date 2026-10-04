from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import score_corpus_isolated

from circuit import corpus

REPO_ROOT = Path(__file__).parents[1]
CORPUS_ROOT = REPO_ROOT / "library" / "corpus"


def _candidate_library(path: Path) -> None:
    path.mkdir()
    for name in (
        "part.spec.json",
        "footprint.kicad_mod",
        "symbols.kicad_sym",
        "model.step",
    ):
        (path / name).write_text("fixture\n", encoding="utf-8")


def _docker_path(_name: str) -> str:
    return "/usr/bin/docker"


def _no_docker(_name: str) -> None:
    return None


def _run_score(
    candidate: Path,
    output: Path,
    runner: score_corpus_isolated.Runner,
    *,
    datasheet_cache: Path | None = None,
    corpus_root: Path = CORPUS_ROOT,
) -> dict[str, Any]:
    return score_corpus_isolated.run_isolated_score(
        entry_id="lm317-to220",
        candidate_library=candidate,
        part_spec="part.spec.json",
        footprint="footprint.kicad_mod",
        symbol_lib="symbols.kicad_sym",
        symbol_name="LM317",
        model="model.step",
        output_dir=output,
        corpus_root=corpus_root,
        source_root=REPO_ROOT / "src",
        datasheet_cache=datasheet_cache,
        image="circuit-tools:ci",
        runner=runner,
    )


def test_isolated_score_uses_read_only_truth_and_network_free_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(score_corpus_isolated.shutil, "which", _docker_path)
    monkeypatch.delenv("CIRCUIT_CORPUS_CACHE", raising=False)
    candidate = tmp_path / "candidate"
    _candidate_library(candidate)
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=f"sha256:{'a' * 64}\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"verdict":"not_available"}',
            stderr="",
        )

    output = tmp_path / "output"
    report = _run_score(candidate, output, runner)
    docker_argv = calls[-1]

    assert len(calls) == 2
    assert report["image_digest"] == f"sha256:{'a' * 64}"
    assert report["datasheet_cache_pdf_sha256"] is None
    assert report["manifest_sha256"] == corpus.sha256(CORPUS_ROOT / "corpus.json")
    assert len(report["truth_sha256_by_entry"]) == 40
    assert set(report["truth_sha256_by_entry"]) == {
        entry.id for entry in corpus.load_manifest(CORPUS_ROOT / "corpus.json").entries
    }
    assert (output / "corpus-score-report.json").is_file()
    assert "--network" in docker_argv
    assert docker_argv[docker_argv.index("--network") + 1] == "none"
    assert "--read-only" in docker_argv
    assert docker_argv[docker_argv.index("--tmpfs") + 1] == "/tmp"
    assert docker_argv[docker_argv.index("circuit-tools:ci") + 1] == "python3"
    mounts = [
        docker_argv[index + 1]
        for index, argument in enumerate(docker_argv[:-1])
        if argument == "--mount"
    ]
    assert len(mounts) == 4
    assert any("target=/corpus,readonly" in mount for mount in mounts)
    assert any("target=/candidate,readonly" in mount for mount in mounts)
    assert any("target=/circuit-source,readonly" in mount for mount in mounts)
    assert not any("target=/datasheets" in mount for mount in mounts)
    assert "CIRCUIT_CORPUS_CACHE=/datasheets" not in docker_argv
    writable_mounts = [mount for mount in mounts if ",readonly" not in mount]
    assert len(writable_mounts) == 1
    assert "target=/output" in writable_mounts[0]


def test_isolated_score_mounts_datasheet_cache_and_records_matching_pdf_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(score_corpus_isolated.shutil, "which", _docker_path)
    candidate = tmp_path / "candidate"
    _candidate_library(candidate)
    corpus_root = tmp_path / "corpus"
    shutil.copytree(CORPUS_ROOT, corpus_root)
    cache = tmp_path / "datasheets"
    cache.mkdir()
    pdf_contents = b"verified corpus datasheet"
    pdf_sha256 = hashlib.sha256(pdf_contents).hexdigest()
    (cache / "lm317-to220.pdf").write_bytes(pdf_contents)

    manifest_path = corpus_root / "corpus.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["entries"] if item["id"] == "lm317-to220")
    entry["datasheet"]["sha256"] = pdf_sha256
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=f"sha256:{'a' * 64}\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"verdict":"not_available"}',
            stderr="",
        )

    report = _run_score(
        candidate,
        tmp_path / "output",
        runner,
        datasheet_cache=cache,
        corpus_root=corpus_root,
    )
    docker_argv = calls[-1]
    mounts = [
        docker_argv[index + 1]
        for index, argument in enumerate(docker_argv[:-1])
        if argument == "--mount"
    ]

    assert report["datasheet_cache_pdf_sha256"] == pdf_sha256
    assert len(mounts) == 5
    assert any(f"source={cache.resolve()},target=/datasheets,readonly" in mount for mount in mounts)
    assert "--env" in docker_argv
    assert docker_argv[docker_argv.index("CIRCUIT_CORPUS_CACHE=/datasheets") - 1] == "--env"


def test_isolated_score_allows_missing_footprint_and_model_for_human_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(score_corpus_isolated.shutil, "which", _docker_path)
    candidate = tmp_path / "candidate"
    _candidate_library(candidate)
    (candidate / "footprint.kicad_mod").unlink()
    (candidate / "model.step").unlink()
    corpus_root = tmp_path / "corpus"
    shutil.copytree(CORPUS_ROOT, corpus_root)
    manifest = json.loads((corpus_root / "corpus.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["entries"] if item["id"] == "lm317-to220")
    truth_path = corpus_root / entry["truth_path"]
    truth = corpus.CorpusTruth.model_validate_json(truth_path.read_bytes())
    truth = truth.model_copy(
        update={
            "expected_outcome": "human_request",
            "expected_pads": [],
            "dimensions": corpus.CorpusDimensions(),
        }
    )
    truth_path.write_text(truth.model_dump_json(by_alias=True), encoding="utf-8")

    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=f"sha256:{'a' * 64}\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"verdict":"not_available"}',
            stderr="",
        )

    report = _run_score(
        candidate,
        tmp_path / "output",
        runner,
        corpus_root=corpus_root,
    )
    docker_argv = calls[-1]

    assert report["score"]["verdict"] == "not_available"
    assert docker_argv[-6:] == [
        "lm317-to220",
        "part.spec.json",
        "footprint.kicad_mod",
        "symbols.kicad_sym",
        "LM317",
        "model.step",
    ]
    assert "footprint.kicad_mod" in docker_argv
    assert "model.step" in docker_argv
    with pytest.raises(
        score_corpus_isolated.CorpusIsolationError,
        match="candidate artifact is unavailable",
    ):
        score_corpus_isolated._relative_candidate_file(  # pyright: ignore[reportPrivateUsage]
            candidate.resolve(),
            "footprint.kicad_mod",
        )


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_datasheet_cache_defaults_from_environment_or_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    environment_cache = tmp_path / "environment-cache"
    explicit_cache = tmp_path / "explicit-cache"
    monkeypatch.setenv("CIRCUIT_CORPUS_CACHE", str(environment_cache))
    captured: dict[str, Any] = {}

    def run_score(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"score": {"verdict": "not_available"}}

    monkeypatch.setattr(score_corpus_isolated, "run_isolated_score", run_score)
    arguments = [
        "--entry-id",
        "lm317-to220",
        "--candidate-library",
        str(tmp_path / "candidate"),
        "--part-spec",
        "part.spec.json",
        "--footprint",
        "footprint.kicad_mod",
        "--symbol-lib",
        "symbols.kicad_sym",
        "--symbol-name",
        "LM317",
        "--model",
        "model.step",
        "--output-dir",
        str(tmp_path / "output"),
    ]
    if explicit:
        arguments.extend(["--datasheet-cache", str(explicit_cache)])

    assert score_corpus_isolated.main(arguments) == 0
    assert captured["datasheet_cache"] == (explicit_cache if explicit else environment_cache)


def test_isolated_scorer_refuses_any_authoring_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CIRCUIT_AUTHORING_LANE", "")
    with pytest.raises(score_corpus_isolated.CorpusIsolationError, match="authoring lanes"):
        score_corpus_isolated.run_isolated_score(
            entry_id="lm317-to220",
            candidate_library=tmp_path,
            part_spec="part.spec.json",
            footprint="footprint.kicad_mod",
            symbol_lib="symbols.kicad_sym",
            symbol_name="LM317",
            model="model.step",
            output_dir=tmp_path / "output",
        )


def test_isolated_scorer_fails_closed_when_docker_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(score_corpus_isolated.shutil, "which", _no_docker)
    with pytest.raises(score_corpus_isolated.CorpusIsolationError, match="Docker is unavailable"):
        score_corpus_isolated.run_isolated_score(
            entry_id="lm317-to220",
            candidate_library=tmp_path,
            part_spec="part.spec.json",
            footprint="footprint.kicad_mod",
            symbol_lib="symbols.kicad_sym",
            symbol_name="LM317",
            model="model.step",
            output_dir=tmp_path / "output",
        )


def test_isolated_scorer_fails_closed_when_image_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(score_corpus_isolated.shutil, "which", _docker_path)
    monkeypatch.delenv("CIRCUIT_CORPUS_CACHE", raising=False)
    candidate = tmp_path / "candidate"
    _candidate_library(candidate)
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(
            arguments,
            1,
            stdout="",
            stderr="No such image",
        )

    with pytest.raises(score_corpus_isolated.CorpusIsolationError, match="image is unavailable"):
        _run_score(candidate, tmp_path / "output", runner)
    assert len(calls) == 1
    assert calls[0][1:3] == ["image", "inspect"]
