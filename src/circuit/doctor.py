"""Installation diagnostics for the circuit plugin."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
from pathlib import Path

from .kicad_cli import version
from .paths import API_SOCKET_PATH


def _check(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"name": name, "status": "ok" if ok else "fail", "detail": detail}


def checks() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    cli = shutil.which("kicad-cli")
    result.append(_check("kicad-cli", cli is not None, cli or "not found"))
    if cli is not None:
        try:
            cli_version = version()
            result.append(_check("kicad-version", cli_version.startswith("10.99"), cli_version))
        except Exception as exc:
            result.append(_check("kicad-version", False, str(exc)))
        try:
            probe = subprocess.run(
                ["kicad-cli", "api-server", "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            result.append(_check("api-server", probe.returncode == 0, probe.stderr or probe.stdout))
        except OSError as exc:
            result.append(_check("api-server", False, str(exc)))
    konnect = shutil.which("konnect")
    if konnect is None:
        result.append(_check("konnect", False, "not found"))
    else:
        try:
            probe = subprocess.run(
                ["konnect", "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            detail = (probe.stdout or probe.stderr).strip()
            result.append(_check("konnect", probe.returncode == 0, detail))
        except OSError as exc:
            result.append(_check("konnect", False, str(exc)))
    try:
        __import__("circuit")
        result.append(_check("circuit-import", True, "importable"))
    except ImportError as exc:
        result.append(_check("circuit-import", False, str(exc)))
    try:
        API_SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        probe_path = API_SOCKET_PATH.parent / ".circuit-write-test"
        probe_path.write_text("", encoding="utf-8")
        probe_path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM):
            pass
        result.append(_check("socket-directory", True, str(API_SOCKET_PATH.parent)))
    except OSError as exc:
        result.append(_check("socket-directory", False, str(exc)))
    libraries = Path("/opt/circuit/libraries/cern-kicad-libs")
    result.append(_check("cern-libraries", libraries.is_dir(), str(libraries)))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warn", action="store_true")
    args = parser.parse_args()
    results = checks()
    ok = all(item["status"] == "ok" for item in results)
    payload = {"status": "ok" if ok else "fail", "checks": results}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok or args.warn else 1


if __name__ == "__main__":
    raise SystemExit(main())
