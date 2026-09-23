"""Resolve the circuit package that matches the installed plugin, then exec.

Plugin installs update the assets under plugins/circuit but do not reinstall
the `circuit` Python package, so `python3 -m circuit.*` may import a stale
site-packages copy that lacks the tools the assets expect. This launcher
points PYTHONPATH at a source tree consistent with the plugin before execing
the requested module.

Resolution order (first directory containing circuit/__init__.py wins):
  1. $CIRCUIT_SRC
  2. newest ~/.openhands/cache/extensions/electrical-circuit-agent-*/src
  3. /opt/circuit/src (circuit-server image)
  4. <repo>/src when running from a repository checkout
  5. none found -> fall back to the already-installed package
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_MODULES = {"mcp_server": "circuit.mcp_server", "doctor": "circuit.doctor"}


def _candidates(plugin_root: Path) -> list[Path]:
    candidates: list[Path] = []
    env_src = os.environ.get("CIRCUIT_SRC")
    if env_src:
        candidates.append(Path(env_src))
    cache = Path.home() / ".openhands" / "cache" / "extensions"
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("module", choices=sorted(_MODULES))
    parser.add_argument("module_args", nargs=argparse.REMAINDER)
    namespace = parser.parse_args()
    plugin_root = Path(__file__).resolve().parents[1]
    source = resolve_source(plugin_root)
    env = dict(os.environ)
    if source is None:
        print(
            "circuit_launcher: no vendored source found; using installed package",
            file=sys.stderr,
        )
    else:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(source) + (os.pathsep + existing if existing else "")
    os.execvpe(
        sys.executable,
        [sys.executable, "-m", _MODULES[namespace.module], *namespace.module_args],
        env,
    )
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
