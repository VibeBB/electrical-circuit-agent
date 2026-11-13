from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

EXPECTED: dict[str, str] = {
    ".github/workflows/pr-branch-cleanup.yml": (
        "314f848990fb8272b32ed5f6ad63c0ca9197b0559051eb87a74a9d9596b0f56c"
    ),
    ".github/workflows/workflow-lint.yml": (
        "254b46c37294bf9782821bd2216f5716bd2a488d8ccf54cc87382fc47d45ab59"
    ),
    ".github/workflows/main-ci-failure-issue.yml#jobs": (
        "a22745c5ea18e2a876025cae0041ceafcce2e7d8983971cc8c8959a4bf9a8f07"
    ),
    ".github/workflows/dependency-review.yml": (
        "ac6aa33bde1dd3b87fee1e45d3d282aea776bd58ea51d25e78ab8a0dc79f28f5"
    ),
    ".github/workflows/scorecard.yml": (
        "1eac88b7876ce6118986325546cf3a493c171c645f61233fcd5c4181a4f5bc2d"
    ),
    ".github/workflows/codeql.yml": (
        "d716121a83fcd8b6a55c470121b78be0deaa67b6001c77778545a0c3effaa953"
    ),
}
UNITS: dict[str, tuple[str, bytes | None]] = {
    ".github/workflows/pr-branch-cleanup.yml": (
        ".github/workflows/pr-branch-cleanup.yml",
        None,
    ),
    ".github/workflows/workflow-lint.yml": (
        ".github/workflows/workflow-lint.yml",
        None,
    ),
    ".github/workflows/main-ci-failure-issue.yml#jobs": (
        ".github/workflows/main-ci-failure-issue.yml",
        b"jobs:",
    ),
    ".github/workflows/dependency-review.yml": (
        ".github/workflows/dependency-review.yml",
        None,
    ),
    ".github/workflows/scorecard.yml": (
        ".github/workflows/scorecard.yml",
        None,
    ),
    ".github/workflows/codeql.yml": (
        ".github/workflows/codeql.yml",
        None,
    ),
}


def _read_unit(root: Path, path: str, marker: bytes | None) -> bytes:
    content = (root / path).read_bytes()
    if marker is None:
        return content
    offset = 0
    for line in content.splitlines(keepends=True):
        if line.rstrip(b"\r\n") == marker:
            return content[offset:]
        offset += len(line)
    raise ValueError("jobs: marker not found")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--print",
        action="store_true",
        help="print current SHA-256 hashes without checking EXPECTED",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for unit, (path, marker) in UNITS.items():
        try:
            content = _read_unit(root, path, marker)
        except (OSError, ValueError) as exc:
            actual = "missing"
            if args.print:
                print(f"{unit}: {actual}")
            else:
                failure = f"{path}: expected {EXPECTED[unit]}, got {actual} ({exc})"
                failures.append(failure)
            continue
        actual = hashlib.sha256(content).hexdigest()
        if args.print:
            print(f"{unit}: {actual}")
        elif actual != EXPECTED[unit]:
            failures.append(f"{path}: expected {EXPECTED[unit]}, got {actual}")
    if args.print:
        return 1 if failures else 0
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("shared workflows match canonical hashes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
