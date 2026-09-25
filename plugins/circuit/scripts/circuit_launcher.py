"""Resolve the circuit package and tools image, then exec inside Docker.

Plugin installs update the assets under plugins/circuit but do not reinstall
the `circuit` Python package, and the host interpreter is not guaranteed to
carry the package dependencies or KiCad tooling. The launcher therefore runs
every circuit module inside the pinned circuit-tools image — which bundles
KiCad nightly, Konnect, and the baked `circuit` package — mounting the
resolved source tree, the workspace, and the KiCad API socket so paths stay
identical inside the container.

Source resolution order (first directory containing circuit/__init__.py wins):
  1. $CIRCUIT_SRC
  2. newest ~/.openhands/cache/extensions/electrical-circuit-agent-*/src
  3. /opt/circuit/src (circuit-tools image)
  4. <repo>/src when running from a repository checkout
  5. none found -> the image's own baked package is used

The OpenHands cache candidates are searched under both $HOME and the
account's real home directory: callers sometimes override HOME for the
tools container (the image runs as the host uid and its baked-in home is
not writable), and a redirected HOME must not blind the cache lookup.

Image resolution order (first hit wins):
  1. $CIRCUIT_TOOLS_IMAGE (full ref, e.g. ghcr.io/.../circuit-tools@sha256:...)
  2. <plugin>/tools-image.json or <plugin>/skills/*/tools-image.json or
     repo-cache docker/image-digests.json
     (circuit_tools entry: image + digest, falling back to image + tag)
  3. none resolvable, or the pinned ref cannot be pulled -> error
     (docker-only: the launcher never falls back to a local build)

Usage: mcp_server | doctor | connectivity | author | intake | sch-lint |
prewarm | <module args...>. `author`, `intake`, and `sch-lint` exec the
unified `python3 -m circuit` dispatcher; any other first argument is execed
as `python3 -m circuit.<arg>` inside the container. When `--warn` is present
(SessionStart doctor mode), a failed image resolution prints a warning and
exits 0.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from pathlib import Path

_MODULES = {
    "mcp_server": "circuit.mcp_server",
    "doctor": "circuit.doctor",
    "connectivity": "circuit.connectivity",
}

# Subcommands handled by the unified `python -m circuit` dispatcher rather
# than a same-named module (they contain a dash or have no module entry point).
_CLI_SUBCOMMANDS = {"author", "fit-sheet", "intake", "review-record", "sch-lint"}

_CONTAINER_SRC = "/plugin-src"
_ENV_PREFIXES = ("OPENHANDS_", "CIRCUIT_")
_ENV_KEYS = ("TMPDIR", "KICAD_API_SOCKET", "CIRCUIT_KONNECT")
_API_SOCKET_PATH = "/tmp/circuit-kicad.sock"
_MODULE_NAME = re.compile(r"^[a-z_]+$")

# The container runs as the host uid, whose passwd entry and home do not
# exist inside the image: a forwarded HOME/XDG leaves fontconfig, KiCad and
# friends without writable directories. Point the transient state at /tmp.
_CONTAINER_ENV = {
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "XDG_CACHE_HOME": "/tmp/.cache",
    "XDG_CONFIG_HOME": "/tmp/.config",
    "XDG_DATA_HOME": "/tmp/.local/share",
}


def _homes() -> list[Path]:
    """$HOME first, then the account's real home (HOME may be overridden)."""
    homes = [Path.home()]
    try:
        real = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return homes
    if real != homes[0]:
        homes.append(real)
    return homes


def _candidates(plugin_root: Path) -> list[Path]:
    candidates: list[Path] = []
    env_src = os.environ.get("CIRCUIT_SRC")
    if env_src:
        candidates.append(Path(env_src))
    for home in _homes():
        cache = home / ".openhands" / "cache" / "extensions"
        try:
            if cache.is_dir():
                candidates.extend(
                    sorted(
                        cache.glob("electrical-circuit-agent-*/src"),
                        key=lambda path: path.stat().st_mtime,
                        reverse=True,
                    )
                )
        except OSError:
            pass
    candidates.append(Path("/opt/circuit/src"))
    candidates.append(plugin_root.parent.parent / "src")
    return candidates


def resolve_source(plugin_root: Path) -> Path | None:
    for candidate in _candidates(plugin_root):
        try:
            if (candidate / "circuit" / "__init__.py").is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _repo_dirs(plugin_root: Path) -> list[Path]:
    """Directories that may carry docker/ build assets for this plugin."""
    dirs: list[Path] = []
    repo_checkout = plugin_root.parent.parent
    if (repo_checkout / "docker").is_dir():
        dirs.append(repo_checkout)
    for home in _homes():
        cache = home / ".openhands" / "cache" / "extensions"
        try:
            if cache.is_dir():
                dirs.extend(
                    sorted(
                        (p for p in cache.glob("electrical-circuit-agent-*") if p.is_dir()),
                        key=lambda path: path.stat().st_mtime,
                        reverse=True,
                    )
                )
        except OSError:
            pass
    return dirs


def _lock_entry_ref(lock_path: Path, key: str | None) -> str | None:
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(key) if key else data
    if not isinstance(entry, dict) or not entry.get("image"):
        return None
    if entry.get("digest"):
        return f"{entry['image']}@{entry['digest']}"
    if entry.get("tag"):
        return f"{entry['image']}:{entry['tag']}"
    return None


def _image_from_lock(plugin_root: Path) -> str | None:
    ref = _lock_entry_ref(plugin_root / "tools-image.json", None)
    if ref:
        return ref
    try:
        skill_pins = sorted(plugin_root.glob("skills/*/tools-image.json"))
    except OSError:
        skill_pins = []
    for pin in skill_pins:
        ref = _lock_entry_ref(pin, None)
        if ref:
            return ref
    for repo_dir in _repo_dirs(plugin_root):
        ref = _lock_entry_ref(repo_dir / "docker" / "image-digests.json", "circuit_tools")
        if ref:
            return ref
    return None


def _docker() -> str | None:
    return shutil.which("docker")


def _ensure_image(plugin_root: Path, *, pull: bool = True) -> str:
    """Resolve the pinned tools image ref; fail when none is available.

    ``pull=False`` reports a missing local image without pulling it — the
    SessionStart doctor hook (--warn) must stay lightweight."""
    docker = _docker()
    if docker is None:
        raise RuntimeError("docker not found on PATH (circuit runs docker-only)")

    ref = os.environ.get("CIRCUIT_TOOLS_IMAGE") or _image_from_lock(plugin_root)
    if ref is None:
        raise RuntimeError(
            "no circuit tools image resolvable: set CIRCUIT_TOOLS_IMAGE or pin "
            "image+digest in tools-image.json / docker/image-digests.json"
        )
    if (
        subprocess.run(
            [docker, "image", "inspect", ref],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    ):
        return ref
    if not pull:
        raise RuntimeError(
            f"circuit tools image {ref} not pulled locally; "
            "run 'circuit_launcher.py prewarm' to fetch it"
        )
    print(f"circuit_launcher: pulling tools image {ref}", file=sys.stderr)
    if (
        subprocess.run(
            [docker, "pull", ref],
            check=False,
            stdout=subprocess.DEVNULL,
        ).returncode
        == 0
    ):
        return ref
    raise RuntimeError(f"circuit tools image {ref} not present locally and pull failed")


def _socket_mounts() -> list[str]:
    """Bind-mount the KiCad API socket into the container when it exists."""
    url = os.environ.get("KICAD_API_SOCKET", f"ipc://{_API_SOCKET_PATH}")
    path = url[len("ipc://") :] if url.startswith("ipc://") else url
    if Path(path).exists():
        return ["-v", f"{path}:{path}"]
    return []


def _docker_argv(image: str, source: Path | None, inner_argv: list[str]) -> list[str]:
    workdir = os.environ.get("OPENHANDS_PROJECT_DIR") or os.getcwd()
    argv = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network",
        "none",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{workdir}:{workdir}",
        "-w",
        workdir,
    ]
    argv += _socket_mounts()
    if source is not None:
        argv += ["-v", f"{source}:{_CONTAINER_SRC}:ro", "-e", f"PYTHONPATH={_CONTAINER_SRC}"]
    for key, value in os.environ.items():
        if key in _ENV_KEYS or any(key.startswith(p) for p in _ENV_PREFIXES):
            argv += ["-e", f"{key}={value}"]
    for key, value in _CONTAINER_ENV.items():
        argv += ["-e", f"{key}={value}"]
    argv.append(image)
    argv += inner_argv
    return argv


def _warn_or_die(message: str, module_args: list[str]) -> int:
    if "--warn" in module_args:
        print(f"warn: {message}", file=sys.stderr)
        print(json.dumps({"status": "fail", "detail": message}))
        return 0
    print(f"circuit_launcher: {message}", file=sys.stderr)
    return 1


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(
            "usage: circuit_launcher.py {mcp_server|doctor|connectivity|"
            "author|intake|sch-lint|prewarm|<module>}",
            file=sys.stderr,
        )
        return 2

    plugin_root = Path(__file__).resolve().parents[1]
    try:
        image = _ensure_image(plugin_root, pull="--warn" not in argv)
    except RuntimeError as exc:
        return _warn_or_die(str(exc), argv)
    if argv[0] == "prewarm":
        print(f"circuit_launcher: tools image ready: {image}")
        return 0

    source = resolve_source(plugin_root)
    if argv[0] in _CLI_SUBCOMMANDS:
        inner = ["python3", "-m", "circuit", *argv]
    elif argv[0] in _MODULES:
        inner = ["python3", "-m", _MODULES[argv[0]], *argv[1:]]
    elif _MODULE_NAME.match(argv[0]):
        inner = ["python3", "-m", f"circuit.{argv[0]}", *argv[1:]]
    else:
        print(f"circuit_launcher: unknown module {argv[0]}", file=sys.stderr)
        return 2
    os.execvp("docker", _docker_argv(image, source, inner))
    return 0


if __name__ == "__main__":
    sys.exit(main())
