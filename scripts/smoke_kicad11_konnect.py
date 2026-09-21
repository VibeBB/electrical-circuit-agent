#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///

"""Run a minimal KiCad 11 headless IPC smoke test through Konnect."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from konnect_client import call_tool, notify, request


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    fixture = Path(os.environ.get("CIRCUIT_SMOKE_FIXTURE", "/work"))
    source_board = fixture / "board.kicad_pcb"
    source_project = fixture / "board.kicad_pro"
    if not source_board.is_file() or not source_project.is_file():
        raise FileNotFoundError("fixture must contain board.kicad_pcb and board.kicad_pro")

    runtime_environment = os.environ.copy()
    runtime_environment.setdefault("HOME", "/home/circuit")
    doctor = subprocess.run(
        ["python3", "-m", "circuit.doctor", "--warn"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=runtime_environment,
    )
    if doctor.stdout:
        print(doctor.stdout.strip())

    with tempfile.TemporaryDirectory(prefix="circuit-smoke-") as temporary:
        work = Path(temporary)
        board = work / "board.kicad_pcb"
        project = work / "board.kicad_pro"
        shutil.copy2(source_board, board)
        shutil.copy2(source_project, project)
        before_hash = sha256(board)
        socket_path = Path("/tmp/circuit-smoke.sock")
        socket_path.unlink(missing_ok=True)
        server_log = work / "kicad-server.log"
        with server_log.open("w", encoding="utf-8") as log:
            server = subprocess.Popen(
                [
                    "kicad-cli",
                    "api-server",
                    "--socket",
                    str(socket_path),
                    str(board),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                env=runtime_environment,
            )
        konnect: subprocess.Popen[str] | None = None
        next_id = 1
        try:
            deadline = time.monotonic() + 30
            while not socket_path.exists():
                if server.poll() is not None:
                    raise RuntimeError(f"KiCad server exited with {server.returncode}")
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for KiCad API socket")
                time.sleep(0.05)

            environment = runtime_environment.copy()
            environment["KICAD_API_SOCKET"] = f"ipc://{socket_path}"
            environment["HOME"] = str(work / "home")
            (work / "home").mkdir()
            konnect = subprocess.Popen(
                ["konnect"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=environment,
            )
            initialize, _ = request(
                konnect,
                next_id,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "circuit-smoke", "version": "0.1"},
                },
            )
            next_id += 1
            notify(konnect, "notifications/initialized")
            _, _ = request(konnect, next_id, "tools/list", {})
            next_id += 1
            boxes, next_id = call_tool(konnect, next_id, "list_toolboxes", {})
            toolsets = [
                item["name"]
                for item in boxes["toolsets"]
                if "pcb" in item["name"] or item["name"] == "verification"
            ]
            for toolset in toolsets:
                _, next_id = call_tool(konnect, next_id, "load_toolset", {"name": toolset})
            _, next_id = call_tool(konnect, next_id, "check_kicad_ui", {"timeout_seconds": 2})
            board_info, next_id = call_tool(
                konnect, next_id, "get_board_info", {"board": str(board)}
            )
            if board_info.get("source") != "ipc":
                raise RuntimeError(f"board did not use IPC: {board_info}")
            moved, next_id = call_tool(
                konnect,
                next_id,
                "move_component",
                {"board": str(board), "reference": "R1", "x": 102.0, "y": 100.0},
            )
            if moved.get("source") != "ipc":
                raise RuntimeError(f"move did not use IPC: {moved}")
            routed, next_id = call_tool(
                konnect,
                next_id,
                "route_trace",
                {
                    "board": str(board),
                    "net_name": "VCC",
                    "layer": "F.Cu",
                    "x1": 100.8,
                    "y1": 100.0,
                    "x2": 108.0,
                    "y2": 100.0,
                    "width": 0.25,
                },
            )
            via, next_id = call_tool(
                konnect,
                next_id,
                "add_via",
                {"board": str(board), "net_name": "GND", "x": 105.0, "y": 102.0},
            )
            saved, next_id = call_tool(konnect, next_id, "save_project", {})
            drc_path = work / "drc.json"
            subprocess.run(
                [
                    "kicad-cli",
                    "pcb",
                    "drc",
                    "--format",
                    "json",
                    "--output",
                    str(drc_path),
                    str(board),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            drc = json.loads(drc_path.read_text(encoding="utf-8"))
            after_hash = sha256(board)
            if before_hash == after_hash:
                raise RuntimeError("board hash did not change")
            summary = {
                "initialize": initialize.get("result", {}),
                "board_info": board_info,
                "move_component": moved,
                "route_trace": routed,
                "add_via": via,
                "save_project": saved,
                "drc": drc,
                "board_sha256_before": before_hash,
                "board_sha256_after": after_hash,
            }
            print(json.dumps(summary, ensure_ascii=False))
            return 0
        finally:
            if konnect is not None:
                if konnect.stdin is not None:
                    konnect.stdin.close()
                try:
                    konnect.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    konnect.kill()
                    konnect.wait()
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"smoke failed: {error}", file=sys.stderr)
        raise
