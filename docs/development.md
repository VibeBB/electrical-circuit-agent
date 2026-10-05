# Development

## Setup

```bash
uv sync --locked          # Python ≥3.12, uv, ruff, pyright strict, pytest
```

## Verification

The entry point is `scripts/verify_all.py`:

```bash
uv run python scripts/verify_all.py --stage docs     # links + ADR index + docs checks
uv run python scripts/verify_all.py --stage fast     # lint + unit + shared-hook checker + coverage
uv run python scripts/verify_all.py --stage docs --list   # machine-readable command table
```

Groups (`lint`, `unit`, `docker`) selectable via `--group`, narrowable with
`--match`, partitionable with `--shard K/N`. The `docker` group requires
`CIRCUIT_TOOLS_IMAGE`.

pytest is run through a sanitized environment (the shell's BASH_ENV/`gh`
function export breaks the harness otherwise):

```bash
env -u BASH_ENV -u "BASH_FUNC_gh%%" uv run pytest -q tests/test_liaison.py
```

Direct tool checks:

```bash
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run python scripts/check_shared_hooks.py
uv run python scripts/check_plugin_load.py
```

## CI checks

Required branch-protection checks: `fast`, `fast (3.13)`, `docker-smoke`,
`plugin-load`, `zizmor` (see `docs/operations.md`). The matrix also runs
non-required `fast (3.14)` and a 3.15 canary leg.

## Shared-hook rule

`_records.py`, `require_records.py`, and the other files listed in
`scripts/check_shared_hooks.py` are canonical across the 11 sister repos —
never edit them locally; EXPECTED pins normalized-AST sha256 digests. Before
editing any workflow/script/launcher/hook, grep `tests/` for its literals and
update the guard tests in the same commit.

## Release

Bumps are coordinated by `scripts/release_bump.sh` and `release.yml` (dry-run
supported). Versions are kept in sync across `pyproject.toml`,
`plugins/circuit/.plugin/plugin.json`, and `src/circuit/__init__.py`
(currently `0.1.0`). GHCR images are digest-locked via
`docker/image-digests.json` and `scripts/publish_image_pin_pr.sh`.
