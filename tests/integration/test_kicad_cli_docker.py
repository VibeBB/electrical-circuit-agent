import os
from pathlib import Path

import pytest

from circuit.kicad_cli import drc, erc

pytestmark = pytest.mark.docker


def _configure_cli(repo: Path, tmp_path: Path, image: str) -> str | None:
    tmp_path.chmod(0o777)
    command = (
        f"docker run --rm --user circuit -v {repo}:{repo} -v {tmp_path}:{tmp_path} "
        f"-w {repo} {image} kicad-cli"
    )
    old = os.environ.get("CIRCUIT_KICAD_CLI")
    os.environ["CIRCUIT_KICAD_CLI"] = command
    return old


def _restore_cli(old: str | None) -> None:
    if old is None:
        os.environ.pop("CIRCUIT_KICAD_CLI", None)
    else:
        os.environ["CIRCUIT_KICAD_CLI"] = old


def test_drc_in_tools_image(tmp_path: Path) -> None:
    image = os.environ.get("CIRCUIT_TOOLS_IMAGE")
    if not image:
        pytest.skip("CIRCUIT_TOOLS_IMAGE is not set")
    repo = Path(__file__).parents[2]
    source = repo / "fixtures" / "smoke-board" / "board.kicad_pcb"
    output = tmp_path / "drc.json"
    old = _configure_cli(repo, tmp_path, image)
    try:
        report = drc(source, output)
    finally:
        _restore_cli(old)
    assert report.kicad_version.startswith("10.99")


def test_erc_in_tools_image(tmp_path: Path) -> None:
    image = os.environ.get("CIRCUIT_TOOLS_IMAGE")
    if not image:
        pytest.skip("CIRCUIT_TOOLS_IMAGE is not set")
    repo = Path(__file__).parents[2]
    source = repo / "fixtures" / "smoke-board" / "board.kicad_sch"
    output = tmp_path / "erc.json"
    old = _configure_cli(repo, tmp_path, image)
    try:
        report = erc(source, output)
    finally:
        _restore_cli(old)
    assert report.kicad_version.startswith("10.99")
