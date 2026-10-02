from __future__ import annotations

import argparse
from pathlib import Path

from circuit.mutation import library_mutation_fixture, run_mutations


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run seeded mutations against the real library verifier."
    )
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--spec-check", type=Path)
    parser.add_argument("--symbol-lib", type=Path, required=True)
    parser.add_argument("--symbol-name", required=True)
    parser.add_argument("--footprint", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--export-oracle", action="store_true")
    parser.add_argument("--density", choices=("most", "nominal", "least"), default="nominal")
    parser.add_argument("--seed", type=int, default=32032)
    return parser


def main() -> int:
    args = _parser().parse_args()
    fixture = library_mutation_fixture(
        spec_path=args.spec,
        spec_check_path=args.spec_check,
        symbol_lib=args.symbol_lib,
        symbol_name=args.symbol_name,
        footprint_path=args.footprint,
        model_path=args.model,
        work_dir=args.out.parent / f"{args.out.stem}-work",
        run_export_oracle=args.export_oracle,
        density=args.density,
        seed=args.seed,
    )
    report = run_mutations(fixture)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
