from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from circuit import partspec, revwatch


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check every library PartSpec against its manufacturer datasheet source."
    )
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project = args.project.resolve()
    library = project / "library"
    results: list[dict[str, Any]] = []
    for path in sorted(library.rglob("*.json")):
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        artifact = cast(dict[str, Any], raw)
        if artifact.get("artifact_kind") != "circuit_part_spec":
            continue
        try:
            spec = partspec.load_part_spec(path)
        except (OSError, ValueError) as exc:
            results.append(
                {
                    "part_spec_path": str(path),
                    "check": {
                        "findings": [
                            {
                                "code": "datasheet_part_spec_invalid",
                                "severity": "error",
                                "message": str(exc),
                            }
                        ]
                    },
                }
            )
            continue
        result = revwatch.check_revision(
            spec,
            revwatch.fetch_current_revision,
            spec_path=path,
            project_path=project,
        )
        results.append({"part_spec_path": str(path), "check": result.model_dump(mode="json")})
    report = {
        "artifact_kind": "circuit_weekly_datasheet_revision_watch",
        "checked": len(results),
        "results": results,
        "verdict": (
            "pass"
            if results
            and all(
                finding["severity"] != "error"
                for item in results
                for finding in item["check"]["findings"]
            )
            else "fail"
        ),
    }
    output = args.output or (library / "datasheet-revision-watch" / "report.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps({"verdict": report["verdict"], "checked": len(results), "output": str(output)})
    )
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
