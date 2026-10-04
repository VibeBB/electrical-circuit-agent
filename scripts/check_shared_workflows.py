from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

EXPECTED: dict[str, str] = {
    ".github/workflows/pr-branch-cleanup.yml": (
        "a75506d959fbf41f8a7bef4ef072b8eb4f21e684f7e1c7aeb7a6400a2443d47e"
    ),
    ".github/workflows/workflow-lint.yml#jobs": (
        "f1fcc63297dbc62baa3f31631f2ee1d91cb8ad197ffb36c5cd9b814bdf17995c"
    ),
    ".github/workflows/main-ci-failure-issue.yml#jobs": (
        "c300a8c9af84887351a0e8be882876a713e2c487b8ee5b069090c8c02bb37d5a"
    ),
    ".github/workflows/dependency-review.yml": (
        "38cab160d217b67eec286dfa917ad76f0cc673293eb22af6bc114818ba2eb4a2"
    ),
    ".github/workflows/scorecard.yml": (
        "ed54c51170b60646b307bb53e540f3cbc16fd465ab8de198f26b6830717ce45b"
    ),
    ".github/workflows/codeql.yml": (
        "77d6a96b009d445db5bd7fdfc645389ab366b059b5712774b517ace7eccd1af6"
    ),
}
UNITS: dict[str, tuple[str, bytes | None]] = {
    ".github/workflows/pr-branch-cleanup.yml": (
        ".github/workflows/pr-branch-cleanup.yml",
        None,
    ),
    ".github/workflows/workflow-lint.yml#jobs": (
        ".github/workflows/workflow-lint.yml",
        b"jobs:",
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
