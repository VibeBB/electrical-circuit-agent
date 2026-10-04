#!/usr/bin/env python3
"""Score one candidate against corpus truth in a network-isolated Docker container."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from circuit import corpus

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CORPUS = _REPO_ROOT / "library" / "corpus"
_DEFAULT_LOCK = _REPO_ROOT / "docker" / "image-digests.json"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IMAGE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*\Z")
_CONTAINER_SCORE = """\
import json
import sys
from pathlib import Path
from circuit.corpus import score_entry

entry_id, part_spec, footprint, symbol_lib, symbol_name, model = sys.argv[1:]
score = score_entry(
    Path("/corpus"),
    entry_id,
    Path("/candidate") / part_spec,
    Path("/candidate") / footprint,
    Path("/candidate") / symbol_lib,
    symbol_name,
    Path("/candidate") / model,
)
print(score.model_dump_json())
raise SystemExit(1 if score.verdict == "fail" else 0)
"""

Runner = Callable[..., subprocess.CompletedProcess[str]]


class CorpusIsolationError(RuntimeError):
    """Raised when the isolated corpus scorer cannot fail safely."""


def _locked_image(lock_path: Path) -> str:
    if not lock_path.is_file():
        raise CorpusIsolationError(f"image digest lock is missing: {lock_path}")
    try:
        value: Any = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusIsolationError(f"invalid image digest lock: {lock_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CorpusIsolationError("image digest lock must be a JSON object")
    lock_values = cast(dict[str, Any], value)
    item = lock_values.get("circuit_tools")
    if not isinstance(item, dict):
        raise CorpusIsolationError("image digest lock has no circuit_tools entry")
    image_item = cast(dict[str, Any], item)
    image = image_item.get("image")
    digest = image_item.get("digest")
    if (
        not isinstance(image, str)
        or not isinstance(digest, str)
        or not image
        or _DIGEST.fullmatch(digest) is None
        or digest == f"sha256:{'0' * 64}"
    ):
        raise CorpusIsolationError("circuit_tools image lock entry is malformed")
    return f"{image}@{digest}"


def _image_ref(image: str | None, lock_path: Path) -> tuple[str, str | None]:
    if image == "circuit-tools:ci":
        return "circuit-tools:ci", None
    locked_image = _locked_image(lock_path)
    if image is not None and image != locked_image:
        raise CorpusIsolationError(
            "image must be the locked circuit-tools image or circuit-tools:ci"
        )
    selected = locked_image
    if _IMAGE_REF.fullmatch(selected) is None:
        raise CorpusIsolationError("image reference contains unsupported characters")
    if "@" not in selected:
        return selected, None
    digest = selected.rsplit("@", 1)[1]
    if _DIGEST.fullmatch(digest) is None:
        raise CorpusIsolationError("image reference must use a SHA-256 digest")
    return selected, digest


def _relative_candidate_file(root: Path, value: str, *, missing_ok: bool = False) -> str:
    relative = Path(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {".", ".."} for part in relative.parts)
    ):
        raise CorpusIsolationError("candidate artifact paths must stay inside the library")
    try:
        resolved = (root / relative).resolve(strict=not missing_ok)
    except (OSError, RuntimeError) as exc:
        raise CorpusIsolationError(f"candidate artifact is unavailable: {value}: {exc}") from exc
    if not resolved.is_relative_to(root):
        raise CorpusIsolationError(
            f"candidate artifact is outside the library or not a file: {value}"
        )
    if resolved.exists() and not resolved.is_file():
        raise CorpusIsolationError(
            f"candidate artifact is outside the library or not a file: {value}"
        )
    if not resolved.exists() and not missing_ok:
        raise CorpusIsolationError(f"candidate artifact is unavailable: {value}")
    return relative.as_posix()


def _mount(source: Path, target: str, *, readonly: bool) -> str:
    source_value = str(source)
    if any(character in source_value for character in (",", "\n", "\r")):
        raise CorpusIsolationError("Docker bind-mount paths cannot contain commas or newlines")
    value = f"type=bind,source={source_value},target={target}"
    return f"{value},readonly" if readonly else value


def build_docker_argv(
    *,
    image: str,
    corpus_root: Path,
    source_root: Path,
    candidate_root: Path,
    output_dir: Path,
    datasheet_cache: Path | None = None,
    entry_id: str,
    part_spec: str,
    footprint: str,
    symbol_lib: str,
    symbol_name: str,
    model: str,
) -> list[str]:
    """Build the constrained Docker invocation for one corpus entry."""
    arguments = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--mount",
        _mount(source_root, "/circuit-source", readonly=True),
        "--mount",
        _mount(corpus_root, "/corpus", readonly=True),
    ]
    if datasheet_cache is not None:
        arguments.extend(
            [
                "--mount",
                _mount(datasheet_cache, "/datasheets", readonly=True),
                "--env",
                "CIRCUIT_CORPUS_CACHE=/datasheets",
            ]
        )
    arguments.extend(
        [
            "--mount",
            _mount(candidate_root, "/candidate", readonly=True),
            "--mount",
            _mount(output_dir, "/output", readonly=False),
            "--env",
            "PYTHONPATH=/circuit-source",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--workdir",
            "/tmp",
            image,
            "python3",
            "-c",
            _CONTAINER_SCORE,
            entry_id,
            part_spec,
            footprint,
            symbol_lib,
            symbol_name,
            model,
        ]
    )
    return arguments


def _corpus_hashes(corpus_root: Path) -> tuple[str, dict[str, str], dict[str, str]]:
    manifest_path = corpus_root / "corpus.json"
    manifest = corpus.load_manifest(manifest_path)
    manifest_hash = corpus.sha256(manifest_path)
    truth_hashes: dict[str, str] = {}
    datasheet_hashes: dict[str, str] = {}
    for entry in manifest.entries:
        _, truth_hash = corpus.load_truth(corpus_root, entry)
        truth_hashes[entry.id] = truth_hash
        datasheet_hashes[entry.id] = entry.datasheet.sha256
    return manifest_hash, truth_hashes, datasheet_hashes


def _matching_datasheet_sha256(cache_root: Path | None, expected_sha256: str) -> str | None:
    if cache_root is None:
        return None
    try:
        cached_pdfs = sorted(cache_root.glob("*.pdf"))
    except OSError:
        return None
    for path in cached_pdfs:
        try:
            digest = corpus.sha256(path)
        except OSError:
            continue
        if digest == expected_sha256:
            return digest
    return None


def run_isolated_score(
    *,
    entry_id: str,
    candidate_library: Path,
    part_spec: str,
    footprint: str,
    symbol_lib: str,
    symbol_name: str,
    model: str,
    output_dir: Path,
    corpus_root: Path = _DEFAULT_CORPUS,
    source_root: Path = _REPO_ROOT / "src",
    datasheet_cache: Path | None = None,
    image: str | None = None,
    lock_path: Path = _DEFAULT_LOCK,
    runner: Runner | None = None,
) -> dict[str, Any]:
    if "CIRCUIT_AUTHORING_LANE" in os.environ:
        raise CorpusIsolationError("isolated corpus scoring is unavailable in authoring lanes")
    docker_path = shutil.which("docker")
    if docker_path is None:
        raise CorpusIsolationError("Docker is unavailable")
    execute = runner or subprocess.run
    configured_cache = datasheet_cache
    if configured_cache is None:
        cache_env = os.environ.get("CIRCUIT_CORPUS_CACHE")
        if cache_env:
            configured_cache = Path(cache_env)

    try:
        corpus_path = corpus_root.resolve(strict=True)
        source_path = source_root.resolve(strict=True)
        candidate_path = candidate_library.resolve(strict=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir.resolve(strict=True)
        datasheet_cache_path = (
            configured_cache.expanduser().resolve(strict=True)
            if configured_cache is not None
            else None
        )
    except OSError as exc:
        raise CorpusIsolationError(f"scoring input directory is unavailable: {exc}") from exc
    if (
        not corpus_path.is_dir()
        or not source_path.is_dir()
        or not candidate_path.is_dir()
        or (datasheet_cache_path is not None and not datasheet_cache_path.is_dir())
    ):
        raise CorpusIsolationError(
            "corpus, source, candidate, and datasheet-cache inputs must be directories"
        )

    image_ref, locked_digest = _image_ref(image, lock_path)
    inspection = execute(
        [
            docker_path,
            "image",
            "inspect",
            "--format={{.Id}}",
            image_ref,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspection.returncode != 0:
        detail = (inspection.stderr or "").strip() or "image is not present locally"
        raise CorpusIsolationError(f"required Docker image is unavailable: {detail}")
    image_digest = locked_digest or inspection.stdout.strip()
    if _DIGEST.fullmatch(image_digest) is None:
        raise CorpusIsolationError("Docker did not report a valid image digest")

    manifest_hash, truth_hashes, datasheet_hashes = _corpus_hashes(corpus_path)
    if entry_id not in truth_hashes:
        raise CorpusIsolationError(f"unknown corpus entry id: {entry_id}")
    manifest = corpus.load_manifest(corpus_path / "corpus.json")
    entry = next(entry for entry in manifest.entries if entry.id == entry_id)
    truth, _ = corpus.load_truth(corpus_path, entry)
    human_request = truth.expected_outcome == "human_request"
    candidate_files = {
        "part_spec": _relative_candidate_file(candidate_path, part_spec),
        "footprint": _relative_candidate_file(candidate_path, footprint, missing_ok=human_request),
        "symbol_lib": _relative_candidate_file(candidate_path, symbol_lib),
        "model": _relative_candidate_file(candidate_path, model, missing_ok=human_request),
    }
    datasheet_cache_pdf_sha256 = _matching_datasheet_sha256(
        datasheet_cache_path, datasheet_hashes[entry_id]
    )
    argv = build_docker_argv(
        image=image_ref,
        corpus_root=corpus_path,
        source_root=source_path,
        candidate_root=candidate_path,
        output_dir=output_path,
        datasheet_cache=datasheet_cache_path,
        entry_id=entry_id,
        part_spec=candidate_files["part_spec"],
        footprint=candidate_files["footprint"],
        symbol_lib=candidate_files["symbol_lib"],
        symbol_name=symbol_name,
        model=candidate_files["model"],
    )
    completed = execute(argv, capture_output=True, text=True, check=False)
    try:
        score_value: Any = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = (completed.stderr or "").strip() or "container produced no score JSON"
        raise CorpusIsolationError(f"isolated scoring failed: {detail}") from exc
    if not isinstance(score_value, dict):
        raise CorpusIsolationError("isolated scorer returned a malformed score")
    score = cast(dict[str, Any], score_value)
    verdict = score.get("verdict")
    if verdict not in {"pass", "fail", "not_available"}:
        raise CorpusIsolationError("isolated scorer returned an unknown verdict")
    if completed.returncode not in {0, 1} or (completed.returncode == 1 and verdict != "fail"):
        detail = (completed.stderr or "").strip() or f"container exited with {completed.returncode}"
        raise CorpusIsolationError(f"isolated scoring failed: {detail}")

    report = {
        "artifact_kind": "isolated_corpus_score",
        "entry_id": entry_id,
        "image_ref": image_ref,
        "image_digest": image_digest,
        "manifest_sha256": manifest_hash,
        "truth_sha256_by_entry": truth_hashes,
        "datasheet_cache_pdf_sha256": datasheet_cache_pdf_sha256,
        "score": score,
    }
    report_path = output_path / "corpus-score-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry-id", required=True)
    parser.add_argument("--candidate-library", type=Path, required=True)
    parser.add_argument("--part-spec", required=True)
    parser.add_argument("--footprint", required=True)
    parser.add_argument("--symbol-lib", required=True)
    parser.add_argument("--symbol-name", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--corpus-root", type=Path, default=_DEFAULT_CORPUS)
    cache_env = os.environ.get("CIRCUIT_CORPUS_CACHE")
    default_cache = Path(cache_env).expanduser() if cache_env else None
    parser.add_argument(
        "--datasheet-cache",
        type=Path,
        default=default_cache,
        help="read-only datasheet PDF cache (defaults to CIRCUIT_CORPUS_CACHE)",
    )
    parser.add_argument("--image", default=None, help="local circuit-tools image override")
    parser.add_argument("--lock", type=Path, default=_DEFAULT_LOCK)
    args = parser.parse_args(argv)
    try:
        report = run_isolated_score(
            entry_id=args.entry_id,
            candidate_library=args.candidate_library,
            part_spec=args.part_spec,
            footprint=args.footprint,
            symbol_lib=args.symbol_lib,
            symbol_name=args.symbol_name,
            model=args.model,
            output_dir=args.output_dir,
            corpus_root=args.corpus_root,
            datasheet_cache=args.datasheet_cache,
            image=args.image,
            lock_path=args.lock,
        )
    except (CorpusIsolationError, corpus.CorpusError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "report": str(args.output_dir / "corpus-score-report.json"),
                "verdict": report["score"]["verdict"],
            },
            ensure_ascii=False,
        )
    )
    return 1 if report["score"]["verdict"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
