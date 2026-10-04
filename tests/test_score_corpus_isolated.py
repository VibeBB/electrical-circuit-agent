from __future__ import annotations

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
        corpus_root=CORPUS_ROOT,
        source_root=REPO_ROOT / "src",
        image="circuit-tools:ci",
        runner=runner,
    )


def test_isolated_score_uses_read_only_truth_and_network_free_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(score_corpus_isolated.shutil, "which", _docker_path)
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
    writable_mounts = [mount for mount in mounts if ",readonly" not in mount]
    assert len(writable_mounts) == 1
    assert "target=/output" in writable_mounts[0]


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
