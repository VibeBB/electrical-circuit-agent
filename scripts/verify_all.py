"""Run the canonical repository verification stages.

Commands declared `barrier=True` always run alone in declaration order;
consecutive non-barrier commands run in parallel up to `--jobs` workers.
`--group`, `--match`, and `--shard K/N` select subsets so CI can spread one
stage across runner jobs without duplicating the command list; a shard that
selects no commands exits successfully. The parallelism degree never changes
the artifacts produced by the commands.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GROUPS = ("lint", "unit", "docker")


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    barrier: bool = False
    group: str | None = None


def _docker_integration_commands() -> tuple[Command, ...]:
    integration_dir = Path(ROOT) / "tests" / "integration"
    return tuple(
        Command(
            ("uv", "run", "pytest", path, "-m", "docker", "-n", "0"),
            group="docker",
        )
        for path in sorted(
            str(candidate.relative_to(ROOT)) for candidate in integration_dir.glob("test_*.py")
        )
    )


STAGES: dict[str, tuple[Command, ...]] = {
    "docs": (
        Command(("uv", "run", "python", "scripts/verify_docs.py"), group="lint"),
        Command(
            ("uv", "run", "python", "scripts/render_konnect_tools.py", "--check"),
            group="lint",
        ),
        Command(("git", "diff", "--check"), group="lint"),
    ),
    "fast": (
        Command(("uv", "sync", "--locked"), barrier=True),
        Command(("uv", "run", "ruff", "check"), group="lint"),
        Command(("uv", "run", "ruff", "format", "--check"), group="lint"),
        Command(("uv", "run", "pyright"), group="lint"),
        Command(
            ("uv", "run", "pytest", "--cov", "--cov-report=term-missing:skip-covered"),
            group="unit",
        ),
        Command(("uv", "run", "python", "scripts/check_shared_hooks.py"), group="lint"),
        Command(("uv", "run", "python", "scripts/check_shared_workflows.py"), group="lint"),
        Command(("uv", "run", "python", "scripts/verify_docs.py"), group="lint"),
        Command(("git", "diff", "--check"), group="lint"),
    ),
    "standard": (
        Command(("uv", "sync", "--locked"), barrier=True),
        Command(("uv", "run", "ruff", "check"), group="lint"),
        Command(("uv", "run", "ruff", "format", "--check"), group="lint"),
        Command(("uv", "run", "pyright"), group="lint"),
        Command(
            ("uv", "run", "pytest", "--cov", "--cov-report=term-missing:skip-covered"),
            group="unit",
        ),
        *_docker_integration_commands(),
        Command(("uv", "run", "python", "scripts/verify_docs.py"), group="lint"),
        Command(("git", "diff", "--check"), group="lint"),
    ),
}


def _select(
    commands: tuple[Command, ...],
    group: str | None,
    match: str | None,
    shard: str | None,
) -> tuple[Command, ...]:
    if group is None and match is None and shard is None:
        return commands
    selected = [
        command
        for command in commands
        if command.barrier
        or (
            (group is None or command.group == group)
            and (match is None or match in " ".join(command.argv))
        )
    ]
    if shard is not None:
        shard_index_text, _, shard_count_text = shard.partition("/")
        shard_index = int(shard_index_text)
        shard_count = int(shard_count_text)
        if not 0 <= shard_index < shard_count:
            raise ValueError("--shard requires 0 <= K < N")
        non_barrier_positions = {
            id(command)
            for index, command in enumerate(c for c in selected if not c.barrier)
            if index % shard_count == shard_index
        }
        selected = [
            command
            for command in selected
            if command.barrier or id(command) in non_barrier_positions
        ]
    return tuple(selected)


def _run_one(command: Command) -> tuple[Command, int, str]:
    print("$ " + " ".join(command.argv), flush=True)
    proc = subprocess.run(
        command.argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return command, proc.returncode, proc.stdout + proc.stderr


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=tuple(STAGES), default="fast")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 4))
    parser.add_argument("--group", choices=GROUPS, default=None)
    parser.add_argument(
        "--match",
        default=None,
        help="run only commands whose argv contains this substring",
    )
    parser.add_argument(
        "--shard",
        default=None,
        metavar="K/N",
        help="run the K-th slice (0-based) of the selected commands across N shards",
    )
    args = parser.parse_args(argv)
    if args.list:
        print(
            json.dumps(
                {
                    stage: [
                        {
                            "command": list(command.argv),
                            "barrier": command.barrier,
                            "group": command.group,
                        }
                        for command in stage_commands
                    ]
                    for stage, stage_commands in STAGES.items()
                },
                indent=2,
            )
        )
        return 0
    try:
        commands = _select(STAGES[args.stage], args.group, args.match, args.shard)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if not any(not command.barrier for command in commands):
        print("no commands matched the selection")
        return 0
    docker_selected = any(command.group == "docker" for command in commands)
    if docker_selected and not os.environ.get("CIRCUIT_TOOLS_IMAGE"):
        print("CIRCUIT_TOOLS_IMAGE is required for the docker command group")
        return 2
    if args.jobs <= 1:
        for command in commands:
            print("$ " + " ".join(command.argv), flush=True)
            result = subprocess.run(command.argv, cwd=ROOT, check=False)
            if result.returncode:
                return result.returncode
        return 0

    index = 0
    failures: list[Command] = []
    while index < len(commands):
        command = commands[index]
        if command.barrier:
            _, code, output = _run_one(command)
            sys.stdout.write(output)
            sys.stdout.flush()
            if code:
                failures.append(command)
                break
            index += 1
            continue
        group: list[Command] = []
        while index < len(commands) and not commands[index].barrier:
            group.append(commands[index])
            index += 1
        results: dict[int, tuple[Command, int, str]] = {}
        with ThreadPoolExecutor(max_workers=min(args.jobs, len(group))) as pool:
            futures = {pool.submit(_run_one, c): i for i, c in enumerate(group)}
            for future in futures:
                i = futures[future]
                results[i] = future.result()
        for i in sorted(results):
            _, code, output = results[i]
            sys.stdout.write(output)
            sys.stdout.flush()
            if code:
                failures.append(results[i][0])
    if failures:
        for command in failures:
            print("FAILED: " + " ".join(command.argv), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
