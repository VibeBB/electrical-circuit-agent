"""circuit_launcher.py behavior checks (no docker required — argv/lock only)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "circuit"
LAUNCHER = PLUGIN_ROOT / "scripts" / "circuit_launcher.py"


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


def test_docker_argv_transient_state_env() -> None:
    """HOME/XDG point at /tmp inside the container (host HOME is unwritable)."""
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
    entry = {"image": "ghcr.io/x/circuit-tools", "digest": "sha256:abc", "tag": "t1"}
    (skill_dir / "tools-image.json").write_text(json.dumps(entry), encoding="utf-8")
    assert module._image_from_lock(tmp_path) == "ghcr.io/x/circuit-tools@sha256:abc"


def test_ensure_image_warn_mode_never_pulls(tmp_path: Path) -> None:
    """pull=False must report failure without running docker pull."""
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
