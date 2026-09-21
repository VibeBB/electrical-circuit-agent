import os
import stat
import textwrap
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from circuit import apiserver


def test_rejects_project_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(apiserver, "STATE_FILE", tmp_path / "state.json")
    board = tmp_path / "project.kicad_pro"
    board.write_text("{}", encoding="utf-8")
    with pytest.raises(apiserver.ApiServerError, match="GetOpenDocuments"):
        apiserver.start(board)


def _fake_cli(tmp_path: Path, *, listen: bool) -> Path:
    script = tmp_path / "kicad-cli"
    body = """
import signal
import socket
import sys
import time

socket_path = sys.argv[sys.argv.index("--socket") + 1]
if "--version" in sys.argv:
    print("10.99.0")
    raise SystemExit(0)
if "--help" in sys.argv:
    print("api-server")
    raise SystemExit(0)
"""
    if listen:
        body += """
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(socket_path)
server.listen(1)
def raise_system_exit(*_args):
    server.close()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, lambda *_: raise_system_exit())
while True:
    time.sleep(0.1)
"""
    else:
        body += "time.sleep(60)\n"
    script.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_start_status_stop_with_fake_server(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    cli = _fake_cli(tmp_path, listen=True)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(apiserver, "API_SOCKET_PATH", tmp_path / "kicad.sock")
    monkeypatch.setattr(apiserver, "STATE_FILE", tmp_path / "state.json")
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    assert cli.exists()
    state = apiserver.start(board, timeout_s=2)
    assert apiserver.status() == state
    with pytest.raises(apiserver.ApiServerError, match="already running"):
        apiserver.start(board)
    assert apiserver.stop(timeout_s=2)
    assert apiserver.status() is None


def test_stale_state_is_cleaned(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(apiserver, "API_SOCKET_PATH", tmp_path / "kicad.sock")
    monkeypatch.setattr(apiserver, "STATE_FILE", tmp_path / "state.json")
    apiserver.STATE_FILE.write_text(
        '{"pid": 999999, "socket_path": "'
        + str(tmp_path / "kicad.sock")
        + '", "board_path": "board.kicad_pcb", "started_at": "now"}',
        encoding="utf-8",
    )
    assert apiserver.status() is None
    assert not apiserver.STATE_FILE.exists()


def test_start_times_out_and_cleans_process(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _fake_cli(tmp_path, listen=False)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(apiserver, "API_SOCKET_PATH", tmp_path / "kicad.sock")
    monkeypatch.setattr(apiserver, "STATE_FILE", tmp_path / "state.json")
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    with pytest.raises(apiserver.ApiServerError, match="did not become ready"):
        apiserver.start(board, timeout_s=0.2)
    assert not (tmp_path / "kicad.sock").exists()
