"""circuit_launcher.py behavior checks (no docker required — argv/lock only)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, TypedDict

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "circuit"
LAUNCHER = PLUGIN_ROOT / "scripts" / "circuit_launcher.py"


class ImagePin(TypedDict):
    ref: str
    image: str | None
    digest: str | None
    attestation: str | None


def _load_launcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location("circuit_launcher_test", LAUNCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_image_pin_ships_with_plugin() -> None:
    pin = PLUGIN_ROOT / "skills" / "circuit-workflow" / "tools-image.json"
    assert pin.is_file()
    data = json.loads(pin.read_text(encoding="utf-8"))
    assert data["image"].endswith("/circuit-tools")
    assert data["digest"].startswith("sha256:")
    assert data["tag"]
    assert data["published_at"]
    assert data["tools"]["python"]


def test_plugin_image_pin_matches_docker_lock() -> None:
    pin = PLUGIN_ROOT / "skills" / "circuit-workflow" / "tools-image.json"
    lock = Path(__file__).resolve().parents[1] / "docker" / "image-digests.json"
    if not lock.is_file():
        pytest.skip("docker/image-digests.json not in checkout")
    pinned = json.loads(pin.read_text(encoding="utf-8"))
    locked = json.loads(lock.read_text(encoding="utf-8"))["circuit_tools"]
    assert pinned["image"] == locked["image"]
    assert pinned["digest"] == locked["digest"]


def test_docker_argv_transient_state_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HOME/XDG point at /tmp inside the container (host HOME is unwritable)."""
    monkeypatch.delenv("OPENHANDS_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    module = _load_launcher()
    argv = module._docker_argv(image="img", source=None, inner_argv=["doctor"])
    env = {
        argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1]
        for i, arg in enumerate(argv)
        if arg == "-e"
    }
    assert env["HOME"] == "/tmp"
    assert env["XDG_CACHE_HOME"].startswith("/tmp/")
    assert env["XDG_CONFIG_HOME"].startswith("/tmp/")
    assert env["XDG_DATA_HOME"].startswith("/tmp/")
    assert env["OPENHANDS_PROJECT_DIR"] == str(tmp_path)


def test_docker_argv_does_not_forward_host_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/home/somebody")
    module = _load_launcher()
    argv = module._docker_argv(image="img", source=None, inner_argv=["doctor"])
    env_pairs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
    assert not any(pair == "HOME=/home/somebody" for pair in env_pairs)


def test_image_from_lock_reads_skill_pin(tmp_path: Path) -> None:
    module = _load_launcher()
    skill_dir = tmp_path / "skills" / "circuit-workflow"
    skill_dir.mkdir(parents=True)
    entry = {
        "image": "ghcr.io/x/circuit-tools",
        "digest": "sha256:abc",
        "tag": "t1",
        "attestation": "https://example.invalid/attestation/1",
    }
    (skill_dir / "tools-image.json").write_text(json.dumps(entry), encoding="utf-8")
    assert module._image_from_lock(tmp_path) == {
        "ref": "ghcr.io/x/circuit-tools@sha256:abc",
        "image": "ghcr.io/x/circuit-tools",
        "digest": "sha256:abc",
        "attestation": "https://example.invalid/attestation/1",
    }


def test_socket_mounts_existing_kicad_api_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_launcher()
    socket_path = tmp_path / "kicad.sock"
    socket_path.touch()
    monkeypatch.setenv("KICAD_API_SOCKET", f"ipc://{socket_path}")

    assert module._socket_mounts() == ["-v", f"{socket_path}:{socket_path}"]


def test_resolve_llm_model_from_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile_path = tmp_path / ".openhands" / "profiles" / "author.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(json.dumps({"model": "provider/model"}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CIRCUIT_LLM_PROFILE", "author")
    monkeypatch.delenv("CIRCUIT_LLM_MODEL", raising=False)

    _load_launcher()._resolve_llm_model()

    assert os.environ["CIRCUIT_LLM_MODEL"] == "provider/model"


@pytest.mark.parametrize("profile_contents", ["missing", "{invalid}", '{"other":"value"}'])
def test_resolve_llm_model_falls_back_for_unreadable_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_contents: str,
) -> None:
    if profile_contents != "missing":
        profile_path = tmp_path / ".openhands" / "profiles" / "author.json"
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(profile_contents, encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CIRCUIT_LLM_PROFILE", "author")
    monkeypatch.delenv("CIRCUIT_LLM_MODEL", raising=False)

    _load_launcher()._resolve_llm_model()

    assert os.environ["CIRCUIT_LLM_MODEL"] == "unknown"


def test_resolve_llm_model_preserves_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIRCUIT_LLM_PROFILE", "author")
    monkeypatch.setenv("CIRCUIT_LLM_MODEL", "explicit/model")

    _load_launcher()._resolve_llm_model()

    assert os.environ["CIRCUIT_LLM_MODEL"] == "explicit/model"


def test_ensure_image_warn_mode_never_pulls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pull=False must report failure without running docker pull."""
    monkeypatch.delenv("CIRCUIT_TOOLS_IMAGE", raising=False)
    module = _load_launcher()
    # Point resolution at a pin whose image cannot exist locally.
    skill_dir = tmp_path / "skills" / "circuit-workflow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "tools-image.json").write_text(
        json.dumps({"image": "ghcr.io/x/definitely-not-pulled", "digest": "sha256:abc"}),
        encoding="utf-8",
    )
    if module._docker() is None:
        pytest.skip("docker not on PATH")
    with pytest.raises(RuntimeError, match="not pulled locally"):
        module._ensure_image(tmp_path, pull=False)


def test_ensure_image_inspect_timeout_does_not_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_launcher()
    monkeypatch.delenv("CIRCUIT_TOOLS_IMAGE", raising=False)
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "_docker", lambda: "docker")

    def fake_image_from_lock(_root: Path) -> ImagePin:
        return {
            "ref": "image",
            "image": None,
            "digest": None,
            "attestation": None,
        }

    monkeypatch.setattr(module, "_image_from_lock", fake_image_from_lock)

    def timeout(command: list[str], **kwargs: Any) -> Any:
        calls.append(command)
        raise module.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="docker image inspect timed out after 30 seconds"):
        module._ensure_image(tmp_path)
    assert calls == [["docker", "image", "inspect", "image"]]


def test_ensure_image_pull_timeout_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_launcher()
    monkeypatch.delenv("CIRCUIT_TOOLS_IMAGE", raising=False)
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "_docker", lambda: "docker")

    def fake_image_from_lock(_root: Path) -> ImagePin:
        return {
            "ref": "image",
            "image": None,
            "digest": None,
            "attestation": None,
        }

    monkeypatch.setattr(module, "_image_from_lock", fake_image_from_lock)

    def timeout_pull(command: list[str], **kwargs: Any) -> Any:
        calls.append(command)
        if command[1:3] == ["image", "inspect"]:
            return module.subprocess.CompletedProcess(command, 1)
        raise module.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", timeout_pull)
    with pytest.raises(RuntimeError, match="docker pull timed out after 900 seconds"):
        module._ensure_image(tmp_path)
    assert calls == [
        ["docker", "image", "inspect", "image"],
        ["docker", "pull", "image"],
    ]


def test_homes_includes_real_pw_dir() -> None:
    module = _load_launcher()
    import pwd as _pwd

    homes = module._homes()
    assert Path.home() in homes
    assert Path(_pwd.getpwuid(os.getuid()).pw_dir) in homes


def test_docker_argv_keeps_kicad_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """KICAD_API_SOCKET/CIRCUIT_KONNECT still forward (config, not home paths)."""
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc:///tmp/k.sock")
    monkeypatch.setenv("CIRCUIT_KONNECT", "/usr/bin/konnect")
    module = _load_launcher()
    argv = module._docker_argv(image="img", source=None, inner_argv=["doctor"])
    env = {
        argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1]
        for i, arg in enumerate(argv)
        if arg == "-e"
    }
    assert env["KICAD_API_SOCKET"] == "ipc:///tmp/k.sock"
    assert env["CIRCUIT_KONNECT"] == "/usr/bin/konnect"


def test_main_mcp_server_argv_is_module_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mcp_server` must exec `python3 -m circuit.mcp_server`; argv entries are str."""
    module = _load_launcher()
    captured: list[list[str]] = []

    def fake_execvp(file: str, args: list[str]) -> None:
        captured.append([file, *args])

    def fake_ensure_image(_root: Path, *, pull: bool = True, prewarm: bool = False) -> str:
        return "img@sha256:abc"

    def fake_resolve_source(_root: Path) -> Path | None:
        return None

    monkeypatch.setattr(module, "_ensure_image", fake_ensure_image)
    monkeypatch.setattr(module, "resolve_source", fake_resolve_source)
    monkeypatch.setattr(os, "execvp", fake_execvp)
    monkeypatch.setattr(sys, "argv", ["circuit_launcher.py", "mcp_server", "--extra"])
    assert module.main() == 0
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "docker"
    assert all(isinstance(arg, str) for arg in argv)
    assert argv[-4:] == ["python3", "-m", "circuit.mcp_server", "--extra"]


def test_warn_fallback_json_matches_doctor_key(capsys: pytest.CaptureFixture[str]) -> None:
    """The --warn fallback payload uses the same top-level key as circuit.doctor."""
    module = _load_launcher()
    assert module._warn_or_die("boom", ["doctor", "--warn"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    # circuit.doctor reports under "status" (not "verdict" like the sibling repos).
    assert payload == {"status": "fail", "detail": "boom"}
    assert next(iter(payload)) == "status"
