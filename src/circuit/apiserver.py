"""Lifecycle management for a headless KiCad API server."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from .kicad_cli import command_prefix
from .paths import API_SOCKET_PATH, STATE_FILE


class ApiServerError(RuntimeError):
    """Raised when the KiCad API server cannot be managed."""


class ApiServerState(BaseModel):
    pid: int
    socket_path: str
    board_path: str
    started_at: str


def _state_dir() -> Path:
    return STATE_FILE.parent


def _read_state() -> ApiServerState | None:
    if not STATE_FILE.exists():
        return None
    try:
        with STATE_FILE.open(encoding="utf-8") as handle:
            return ApiServerState.model_validate_json(handle.read())
    except (OSError, ValueError):
        STATE_FILE.unlink(missing_ok=True)
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _socket_ready(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(str(path))
    except OSError:
        return False
    return True


def start(board_path: Path, *, timeout_s: float = 30.0) -> ApiServerState:
    board_path = board_path.resolve()
    if board_path.suffix != ".kicad_pcb":
        raise ApiServerError(
            "api-server requires a .kicad_pcb; .kicad_pro is unsupported because "
            "KiCad lacks the GetOpenDocuments handler"
        )
    if not board_path.is_file():
        raise ApiServerError(f"board does not exist: {board_path}")

    current = _read_state()
    if current is not None:
        if _alive(current.pid):
            raise ApiServerError(f"already running for {current.board_path}; stop first")
        STATE_FILE.unlink(missing_ok=True)

    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    API_SOCKET_PATH.unlink(missing_ok=True)
    log_path = state_dir / "api-server.log"
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [
                *command_prefix(),
                "api-server",
                "--socket",
                str(API_SOCKET_PATH),
                str(board_path),
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    except OSError as exc:
        log_handle.close()
        raise ApiServerError(f"could not start kicad-cli: {exc}") from exc
    log_handle.close()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _socket_ready(API_SOCKET_PATH):
            state = ApiServerState(
                pid=process.pid,
                socket_path=str(API_SOCKET_PATH),
                board_path=str(board_path),
                started_at=datetime.now(UTC).isoformat(),
            )
            STATE_FILE.write_text(state.model_dump_json(indent=2), encoding="utf-8")
            return state
        if process.poll() is not None:
            API_SOCKET_PATH.unlink(missing_ok=True)
            raise ApiServerError(f"kicad-cli exited with status {process.returncode}")
        time.sleep(0.1)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()
    process.wait()
    API_SOCKET_PATH.unlink(missing_ok=True)
    raise ApiServerError(f"api-server did not become ready within {timeout_s:g}s")


def status() -> ApiServerState | None:
    state = _read_state()
    if state is None:
        return None
    if _alive(state.pid):
        return state
    STATE_FILE.unlink(missing_ok=True)
    Path(state.socket_path).unlink(missing_ok=True)
    return None


def stop(*, timeout_s: float = 10.0) -> bool:
    state = _read_state()
    if state is None:
        API_SOCKET_PATH.unlink(missing_ok=True)
        return False
    if _alive(state.pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(state.pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout_s
        while _alive(state.pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _alive(state.pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(state.pid, signal.SIGKILL)
    Path(state.socket_path).unlink(missing_ok=True)
    STATE_FILE.unlink(missing_ok=True)
    return True
