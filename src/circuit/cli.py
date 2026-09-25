"""circuit command line interface.

Subcommands:
  doctor       probe the tool environment (JSON verdict)
  intake       validate an intake.json against a design brief (JSON verdict)
  sch-lint     lint a .kicad_sch for readability defects (JSON verdict)
  connectivity emit the wire-agent ConnectivitySource contract (JSON verdict)
  author       run e2e authoring from a design brief (JSON verdict)

All commands print a JSON verdict to stdout; the verdict is fail-closed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from . import brief, connectivity, doctor, fit_sheet, intake, netlist, sch_lint
from .advisory import VisualChecklist

_E2E_CANDIDATES = [
    # repo checkout: <root>/src/circuit/cli.py -> <root>/scripts/e2e_authoring.py
    Path(__file__).resolve().parents[2] / "scripts" / "e2e_authoring.py",
    # circuit-tools image bakes the script next to the package
    Path("/opt/circuit/bin/e2e_authoring.py"),
]


def _e2e_script() -> Path | None:
    for candidate in _E2E_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    verdict = payload.get("verdict", payload.get("status"))
    return 0 if verdict in ("pass", "ready", "ok") else 1


def _load_brief(path: str) -> brief.DesignBrief:
    return brief.load_brief(Path(path))


def cmd_doctor(args: argparse.Namespace) -> int:
    results = doctor.checks()
    ok = all(item["status"] != "fail" for item in results)
    payload = {"status": "ok" if ok else "fail", "checks": results}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok or args.warn else 1


def cmd_intake(args: argparse.Namespace) -> int:
    brief_path = Path(args.brief)
    intake_path = Path(args.intake)
    try:
        design = _load_brief(args.brief)
        report = intake.check_intake(
            design,
            intake.load_intake(intake_path),
            brief_path=brief_path,
            intake_path=intake_path,
        )
    except (ValueError, OSError) as exc:
        return _emit({"verdict": "fail", "stage": "intake", "detail": str(exc)})
    payload = report.model_dump(mode="json")
    payload["verdict"] = "pass" if report.verdict == "ready" else "fail"
    return _emit(payload)


def cmd_sch_lint(args: argparse.Namespace) -> int:
    try:
        report = sch_lint.lint_file(
            Path(args.schematic),
            Path(args.output) if args.output else None,
        )
    except (ValueError, OSError) as exc:
        return _emit({"verdict": "fail", "stage": "sch-lint", "detail": str(exc)})
    return _emit(report.model_dump(mode="json"))


def cmd_fit_sheet(args: argparse.Namespace) -> int:
    try:
        moves = fit_sheet.clamp_labels(Path(args.schematic), margin=args.margin)
    except (ValueError, OSError) as exc:
        return _emit({"verdict": "fail", "stage": "fit-sheet", "detail": str(exc)})
    return _emit({"verdict": "pass", "clamped": len(moves), "items": moves})


def cmd_review_record(args: argparse.Namespace) -> int:
    """Write `review-visual-<slug>.advisory.json` for a reviewed image.

    The reviewer supplies impression/findings as JSON; this command binds
    them to the image bytes (sha256), validates the detail against the
    typed schema, and writes the record deterministically — instead of a
    hand-assembled JSON that could drift from `advisory.py`.
    """
    from .advisory import write_review_record

    image = Path(args.image)
    try:
        raw: Any = json.loads(Path(args.findings).read_text(encoding="utf-8"))
        raw_list = cast(list[Any], raw) if isinstance(raw, list) else None
        if raw_list is None or not all(isinstance(item, dict) for item in raw_list):
            raise ValueError("findings JSON must be a list of objects")
        findings = cast(list[dict[str, Any]], raw_list)
        impression = (
            Path(args.impression_file).read_text(encoding="utf-8").strip()
            if args.impression_file
            else (args.impression or "").strip()
        )
        path = write_review_record(
            image,
            model=args.model,
            checklist=args.checklist,
            impression=impression,
            findings=findings,
            summary=args.summary or "",
            out_dir=Path(args.out) if args.out else None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _emit({"verdict": "fail", "stage": "review-record", "detail": str(exc)})
    return _emit({"verdict": "pass", "record": str(path)})


def cmd_connectivity(args: argparse.Namespace) -> int:
    try:
        design = _load_brief(args.brief)
        parsed = netlist.parse_netlist(Path(args.netlist)) if args.netlist else None
        payload = connectivity.connectivity_source(design, parsed)
        connectivity.write_connectivity(design, Path(args.out), parsed)
    except (ValueError, OSError) as exc:
        return _emit({"verdict": "fail", "stage": "connectivity-export", "detail": str(exc)})
    return _emit(
        {
            "verdict": "pass",
            "design": design.name,
            "connectors": [item["ref"] for item in payload["connectors"]],
            "nets": [item["ref"] for item in payload["nets"]],
            "out": args.out,
        }
    )


def cmd_author(args: argparse.Namespace) -> int:
    script = _e2e_script()
    if script is None:
        return _emit(
            {
                "verdict": "fail",
                "stage": "author",
                "detail": (
                    "e2e_authoring.py not found "
                    f"(searched {', '.join(str(p) for p in _E2E_CANDIDATES)})"
                ),
            }
        )
    argv = [
        sys.executable,
        str(script),
        "--brief",
        args.brief,
        "--workdir",
        args.workdir,
        "--kicad-share",
        args.kicad_share,
    ]
    if args.intake:
        argv += ["--intake", args.intake]
    if args.json:
        argv += ["--json", args.json]
    proc = subprocess.run(argv, check=False)
    if proc.returncode != 0:
        return _emit(
            {
                "verdict": "fail",
                "stage": "author",
                "detail": f"e2e_authoring exited with status {proc.returncode}",
            }
        )
    return _emit({"verdict": "pass", "workdir": args.workdir})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m circuit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="probe the tool environment")
    doctor_parser.add_argument("--warn", action="store_true")
    doctor_parser.set_defaults(handler=cmd_doctor)

    intake_parser = subparsers.add_parser(
        "intake", help="validate an intake.json against a design brief"
    )
    intake_parser.add_argument("--brief", required=True)
    intake_parser.add_argument("--intake", required=True)
    intake_parser.set_defaults(handler=cmd_intake)

    sch_lint_parser = subparsers.add_parser(
        "sch-lint", help="lint a .kicad_sch for readability defects"
    )
    sch_lint_parser.add_argument("schematic")
    sch_lint_parser.add_argument("--output", default=None)
    sch_lint_parser.set_defaults(handler=cmd_sch_lint)

    fit_sheet_parser = subparsers.add_parser(
        "fit-sheet", help="clamp out-of-bounds schematic labels inside the sheet"
    )
    fit_sheet_parser.add_argument("schematic")
    fit_sheet_parser.add_argument("--margin", type=float, default=fit_sheet.EDGE_MARGIN_MM)
    fit_sheet_parser.set_defaults(handler=cmd_fit_sheet)

    review_record_parser = subparsers.add_parser(
        "review-record",
        help="write a validated review-visual-<slug>.advisory.json for an image",
    )
    review_record_parser.add_argument("--image", required=True)
    review_record_parser.add_argument("--model", required=True, help="reviewer model name")
    review_record_parser.add_argument(
        "--checklist",
        required=True,
        choices=list(VisualChecklist.__args__),
    )
    review_record_parser.add_argument("--impression", default=None)
    review_record_parser.add_argument(
        "--impression-file",
        default=None,
        help="text file with the subjective reading (alternative to --impression)",
    )
    review_record_parser.add_argument(
        "--findings",
        required=True,
        help="JSON file: list of {category, severity, note, bbox?}",
    )
    review_record_parser.add_argument("--summary", default=None)
    review_record_parser.add_argument("--out", default=None, help="output dir (default: image dir)")
    review_record_parser.set_defaults(handler=cmd_review_record)

    connectivity_parser = subparsers.add_parser(
        "connectivity", help="emit the wire-agent ConnectivitySource contract"
    )
    connectivity_parser.add_argument("--brief", required=True)
    connectivity_parser.add_argument("--netlist", default=None)
    connectivity_parser.add_argument("--out", required=True)
    connectivity_parser.set_defaults(handler=cmd_connectivity)

    author_parser = subparsers.add_parser("author", help="run e2e authoring from a design brief")
    author_parser.add_argument("--brief", required=True)
    author_parser.add_argument("--workdir", required=True)
    author_parser.add_argument("--kicad-share", default="/usr/share/kicad-nightly")
    author_parser.add_argument("--intake", default=None)
    author_parser.add_argument("--json", default=None)
    author_parser.set_defaults(handler=cmd_author)

    args = parser.parse_args(argv)
    handler: Any = args.handler
    result: int = handler(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
