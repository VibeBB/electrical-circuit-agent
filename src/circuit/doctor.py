"""Installation diagnostics for the circuit plugin."""

from __future__ import annotations

import argparse
import json
import os
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
        import circuit

        result.append(_check("circuit-import", True, str(Path(circuit.__file__).parent)))
    except ImportError as exc:
        result.append(_check("circuit-import", False, str(exc)))
    else:
        missing: list[str] = []
        for module in ("circuit.sch_lint",):
            try:
                __import__(module)
            except ImportError:
                missing.append(module)
        from . import kicad_cli

        if not hasattr(kicad_cli, "_cache_sidecar"):
            missing.append("kicad_cli.src_sha256")
        result.append(
            _check(
                "package-features",
                not missing,
                "all expected" if not missing else f"stale package, missing {missing}",
            )
        )
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
    candidates = _cern_library_candidates()
    found = next((p for p in candidates if _has_cern_libraries(p)), None)
    detail = (
        str(found)
        if found is not None
        else "not found (searched: " + ", ".join(str(p) for p in candidates) + ")"
    )
    result.append(_check("cern-libraries", found is not None, detail))
    result.append(_vision_probe())
    return result


def _has_cern_libraries(path: Path) -> bool:
    return (path / "SchLib").is_dir() and (path / "PcbLib").is_dir()


def _cern_library_candidates() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get("CIRCUIT_CERN_LIBS")
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/opt/circuit/libraries/cern-kicad-libs"))
    home = Path.home()
    candidates.append(home / "opt/circuit/libraries/cern-kicad-libs")
    candidates.extend(
        sorted(
            home.glob(
                ".openhands/cache/extensions/electrical-circuit-agent-*/libraries/cern-kicad-libs"
            )
        )
    )
    return candidates


_VISION_MODEL_HINTS = (
    "vision",
    "kimi-k3",
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "claude",
    "gemini",
    "qwen-vl",
)


def _vision_probe() -> dict[str, object]:
    """Warn-level vision-lane probe; never fail-closed.

    Reports the best available lane as `vision=<lane>` in the detail:
    `model` (a vision-capable conversation model is configured), `profile`
    (a dedicated vision agent profile is configured), `materialize-only`
    (intake images can be materialized but no vision model is set up), or
    `none`. Model names are matched heuristically, not authoritatively.
    """
    profile = os.environ.get("OPENHANDS_AGENT_PROFILE") or os.environ.get("CIRCUIT_VISION_PROFILE")
    model = os.environ.get("OPENHANDS_LLM_MODEL") or os.environ.get("LLM_MODEL")
    events = os.environ.get("CIRCUIT_AGENT_EVENTS_DIR")
    events_dir = (
        Path(events)
        if events
        else Path.home() / ".openhands" / "agent-canvas" / "dev_conversations"
    )
    tools = [
        name
        for name in ("pdftoppm", "rsvg-convert")
        if shutil.which(os.environ.get(f"CIRCUIT_{name.upper().replace('-', '_')}", name))
    ]
    tools_detail = f"; rasterizers={','.join(tools)}" if tools else ""
    if model and any(hint in model.lower() for hint in _VISION_MODEL_HINTS):
        return {
            "name": "vision",
            "status": "ok",
            "detail": f"vision=model ({model}){tools_detail}",
        }
    if profile and "vision" in profile.lower():
        return {
            "name": "vision",
            "status": "ok",
            "detail": f"vision=profile ({profile}){tools_detail}",
        }
    if events_dir.is_dir():
        return {
            "name": "vision",
            "status": "ok",
            "detail": f"vision=materialize-only ({events_dir}){tools_detail}",
        }
    return {
        "name": "vision",
        "status": "warn",
        "detail": "vision=none (no vision model, profile, or events dir)" + tools_detail,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warn", action="store_true")
    args = parser.parse_args()
    results = checks()
    ok = all(item["status"] != "fail" for item in results)
    payload = {"status": "ok" if ok else "fail", "checks": results}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok or args.warn else 1


if __name__ == "__main__":
    raise SystemExit(main())
