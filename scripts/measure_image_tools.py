#!/usr/bin/env python3
"""Measure the runtime metadata required by the image digest lock."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

_IMAGE_REF = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}\Z")
_LOCAL_IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9_.-]*:[a-z0-9][a-z0-9_.-]*\Z")


def measure(image_ref: str) -> dict[str, str]:
    if _IMAGE_REF.fullmatch(image_ref) is None and _LOCAL_IMAGE_REF.fullmatch(image_ref) is None:
        raise ValueError("image ref must be digest-pinned or a local tag")
    script = (
        "set -eu; "
        "kicad-cli --version; "
        "konnect --version; "
        "python3 --version; "
        "java -version 2>&1 | "
        "sed -n 's/.*IBM Semeru Runtime Open Edition \\([0-9.]*\\).*/semeru_jre=\\1/p' | head -1; "
        "java -Djava.awt.headless=true -jar /opt/freerouting/freerouting.jar --version 2>&1 | "
        "sed -n 's/.*Freerouting v\\([0-9.]*\\).*/freerouting=\\1/p' | head -1; "
        "python3 -c 'import circuit, mcp, pydantic; print(\"circuit=\" + circuit.__version__)'; "
        "cat /opt/circuit/libraries/cern-kicad-libs.commit; "
        "dpkg-query -W -f='kicad-nightly=${Version}\\n' kicad-nightly; "
        "dpkg-query -W -f='kicad-nightly-footprints=${Version}\\n' kicad-nightly-footprints; "
        "dpkg-query -W -f='kicad-nightly-symbols=${Version}\\n' kicad-nightly-symbols"
    )
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "", image_ref, "sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "metadata probe failed")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("10.99."):
            values["kicad-cli"] = line
        elif line.startswith("konnect "):
            values["konnect"] = line.removeprefix("konnect ")
        elif line.startswith("Python "):
            values["python"] = line
        elif line.startswith("circuit="):
            values["circuit"] = line.removeprefix("circuit=")
        elif line.startswith("semeru_jre="):
            values["semeru_jre"] = line.removeprefix("semeru_jre=")
        elif line.startswith("freerouting="):
            values["freerouting"] = line.removeprefix("freerouting=")
        elif re.fullmatch(r"[0-9a-f]{40}", line):
            values["cern_commit"] = line
        elif "=" in line and line.startswith("kicad-nightly"):
            key, value = line.split("=", 1)
            values[key] = value
    required = {
        "kicad-cli",
        "konnect",
        "python",
        "circuit",
        "semeru_jre",
        "freerouting",
        "cern_commit",
        "kicad-nightly",
        "kicad-nightly-footprints",
        "kicad-nightly-symbols",
    }
    if required - values.keys():
        raise ValueError(f"metadata probe omitted: {sorted(required - values.keys())}")
    return dict(sorted(values.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = measure(args.image_ref)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError, RuntimeError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"WROTE {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
