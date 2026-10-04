"""circuit command line interface.

Subcommands:
  doctor       probe the tool environment (JSON verdict)
  intake       validate an intake.json against a design brief (JSON verdict)
  sch-lint     lint a .kicad_sch for readability defects (JSON verdict)
  datasheet-revision-check  compare a PartSpec with its manufacturer source
  connectivity emit the wire-agent ConnectivitySource contract (JSON verdict)
  firmware-export  emit MCU pin connectivity for firmware-agent (JSON verdict)
  firmware-check   check a firmware-agent pin map against the circuit (JSON verdict)
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
from typing import Any, Final, Literal, cast

from . import (
    brief,
    connectivity,
    doctor,
    firmware,
    fit_sheet,
    intake,
    libreview,
    netlist,
    partspec,
    revwatch,
    sch_lint,
)
from .advisory import VisualChecklist

Verdict = Literal["pass", "fail"]
PASS: Final[Verdict] = "pass"
FAIL: Final[Verdict] = "fail"

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
    """Print the JSON verdict; exit 0 only when ``payload["verdict"] == PASS``."""
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["verdict"] == PASS else 1


def _fail(stage: str, detail: str) -> int:
    return _emit({"verdict": FAIL, "stage": stage, "detail": detail})


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
        return _fail("intake", str(exc))
    payload = report.model_dump(mode="json")
    payload["verdict"] = PASS if report.verdict == "ready" else FAIL
    return _emit(payload)


def cmd_sch_lint(args: argparse.Namespace) -> int:
    try:
        report = sch_lint.lint_file(
            Path(args.schematic),
            Path(args.output) if args.output else None,
        )
    except (ValueError, OSError) as exc:
        return _fail("sch-lint", str(exc))
    return _emit(report.model_dump(mode="json"))


def cmd_fit_sheet(args: argparse.Namespace) -> int:
    try:
        moves = fit_sheet.clamp_labels(Path(args.schematic), margin=args.margin)
    except (ValueError, OSError) as exc:
        return _fail("fit-sheet", str(exc))
    return _emit({"verdict": PASS, "clamped": len(moves), "items": moves})


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
        return _fail("review-record", str(exc))
    return _emit({"verdict": PASS, "record": str(path)})


def cmd_datasheet_revision_check(args: argparse.Namespace) -> int:
    spec_path = Path(args.part_spec)
    try:
        spec = partspec.load_part_spec(spec_path)
        result = revwatch.check_revision(
            spec,
            revwatch.fetch_current_revision,
            spec_path=spec_path,
        )
        if args.out:
            output = Path(args.out)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        return _fail("datasheet-revision-check", str(exc))
    payload = result.model_dump(mode="json")
    payload["verdict"] = FAIL if any(item.severity == "error" for item in result.findings) else PASS
    return _emit(payload)


def cmd_library_review(args: argparse.Namespace) -> int:
    spec_path = Path(args.part_spec)
    library_dir = Path(args.library_dir)
    symbol_lib = Path(args.symbol_lib)
    footprint_path = Path(args.footprint)
    pin_source_path = Path(args.pin_source) if args.pin_source else None
    try:
        spec = partspec.load_part_spec(spec_path)
        if args.action == "packet":
            result = libreview.build_review_packet(
                spec_path,
                symbol_lib=symbol_lib,
                symbol_name=args.symbol_name,
                footprint_path=footprint_path,
                library_dir=library_dir,
                density=args.density,
                tolerance_mm=args.tolerance_mm,
                model_required=args.model_required,
                pin_source_path=pin_source_path,
                out_dir=Path(args.out_dir) if args.out_dir else library_dir / "reviews",
            )
        else:
            current_id = libreview.current_packet_id(
                spec_path,
                symbol_lib=symbol_lib,
                symbol_name=args.symbol_name,
                footprint_path=footprint_path,
                library_dir=library_dir,
                density=args.density,
                tolerance_mm=args.tolerance_mm,
                model_required=args.model_required,
                pin_source_path=pin_source_path,
            )
            result = libreview.review_status(
                library_dir,
                spec,
                current_id,
                spec_path=spec_path,
                review_scope=args.review_scope,
            )
    except (OSError, ValueError) as exc:
        return _fail("library-review", str(exc))
    payload = result.model_dump(mode="json")
    state = getattr(result, "state", None)
    payload["verdict"] = (
        PASS
        if (state == "approved" or (state is None and getattr(result, "approvable", False)))
        else FAIL
    )
    return _emit(payload)


def cmd_connectivity(args: argparse.Namespace) -> int:
    try:
        design = _load_brief(args.brief)
        parsed = netlist.parse_netlist(Path(args.netlist)) if args.netlist else None
        payload = connectivity.connectivity_source(design, parsed)
        connectivity.write_connectivity(design, Path(args.out), parsed)
    except (ValueError, OSError) as exc:
        return _emit(connectivity.connectivity_failure(exc))
    return _emit(connectivity.connectivity_result(design, payload, args.out))


def _firmware_connectivity(args: argparse.Namespace) -> firmware.CircuitFirmwareConnectivity:
    brief_path = Path(args.brief)
    netlist_path = Path(args.netlist) if args.netlist else None
    return firmware.firmware_connectivity(
        _load_brief(args.brief),
        brief_path,
        netlist.parse_netlist(netlist_path) if netlist_path else None,
        netlist_path,
    )


def cmd_firmware_export(args: argparse.Namespace) -> int:
    try:
        payload = _firmware_connectivity(args)
        out = firmware.write_firmware_connectivity(payload, Path(args.out))
    except (ValueError, OSError) as exc:
        return _fail("firmware-export", str(exc))
    return _emit(
        {
            "verdict": PASS,
            "design": payload.design,
            "source": payload.source,
            "mcus": [m.ref for m in payload.mcus],
            "out": str(out),
        }
    )


def cmd_firmware_check(args: argparse.Namespace) -> int:
    try:
        payload = _firmware_connectivity(args)
        pinmap_path = Path(args.pinmap)
        report = firmware.check_firmware_pinmap(
            payload, firmware.load_pinmap(pinmap_path), firmware.sha256_file(pinmap_path)
        )
    except (ValueError, OSError) as exc:
        return _fail("firmware-check", str(exc))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return _emit(report.model_dump(mode="json"))


def cmd_author(args: argparse.Namespace) -> int:
    script = _e2e_script()
    if script is None:
        return _fail(
            "author",
            f"e2e_authoring.py not found (searched {', '.join(str(p) for p in _E2E_CANDIDATES)})",
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
        return _fail("author", f"e2e_authoring exited with status {proc.returncode}")
    return _emit({"verdict": PASS, "workdir": args.workdir})


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

    revision_parser = subparsers.add_parser(
        "datasheet-revision-check",
        help="compare a PartSpec datasheet binding with the current manufacturer source",
    )
    revision_parser.add_argument("--part-spec", required=True)
    revision_parser.add_argument("--out", default=None)
    revision_parser.set_defaults(handler=cmd_datasheet_revision_check)

    library_review_parser = subparsers.add_parser(
        "library-review", help="build or inspect a hash-bound library human-review packet"
    )
    library_review_actions = library_review_parser.add_subparsers(dest="action", required=True)
    for action in ("packet", "status"):
        action_parser = library_review_actions.add_parser(action)
        action_parser.add_argument("--part-spec", required=True)
        action_parser.add_argument("--symbol-lib", required=True)
        action_parser.add_argument("--symbol-name", required=True)
        action_parser.add_argument("--footprint", required=True)
        action_parser.add_argument("--library-dir", required=True)
        action_parser.add_argument(
            "--density", choices=("most", "nominal", "least"), default="nominal"
        )
        action_parser.add_argument("--tolerance-mm", type=float, default=0.02)
        action_parser.add_argument("--pin-source", default=None)
        action_parser.add_argument(
            "--model-required",
            action=argparse.BooleanOptionalAction,
            default=True,
        )
        if action == "packet":
            action_parser.add_argument("--out-dir", default=None)
        else:
            action_parser.add_argument(
                "--review-scope",
                choices=("full", "relaxed"),
                default="full",
            )
        action_parser.set_defaults(handler=cmd_library_review)

    connectivity_parser = subparsers.add_parser(
        "connectivity", help="emit the wire-agent ConnectivitySource contract"
    )
    connectivity_parser.add_argument("--brief", required=True)
    connectivity_parser.add_argument("--netlist", default=None)
    connectivity_parser.add_argument("--out", required=True)
    connectivity_parser.set_defaults(handler=cmd_connectivity)

    firmware_export_parser = subparsers.add_parser(
        "firmware-export", help="emit MCU pin connectivity (*.firmware.json) for firmware-agent"
    )
    firmware_export_parser.add_argument("--brief", required=True)
    firmware_export_parser.add_argument("--netlist", default=None)
    firmware_export_parser.add_argument("--out", required=True)
    firmware_export_parser.set_defaults(handler=cmd_firmware_export)

    firmware_check_parser = subparsers.add_parser(
        "firmware-check", help="check a firmware-agent *.fw-pinmap.json against the circuit"
    )
    firmware_check_parser.add_argument("--brief", required=True)
    firmware_check_parser.add_argument("--pinmap", required=True)
    firmware_check_parser.add_argument("--netlist", default=None)
    firmware_check_parser.add_argument("--out", default=None)
    firmware_check_parser.set_defaults(handler=cmd_firmware_check)

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
