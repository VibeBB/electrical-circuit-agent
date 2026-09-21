# Contributing

Thank you for your interest in contributing to electrical-circuit-agent.

## Development setup

```bash
uv sync
```

The project uses Python 3.12+, uv, ruff, pyright (strict mode), and pytest.

```bash
uv run ruff check .
uv run pyright
uv run pytest
```

## Verification

The single entry point for verification is `scripts/verify_all.py`:

```bash
uv run python scripts/verify_all.py --stage docs
uv run python scripts/verify_all.py --stage fast
```

`--stage standard` additionally requires `CIRCUIT_TOOLS_IMAGE` and runs the
Docker integration tests.

## Docker image

To build the tools image locally:

```bash
docker build -f docker/circuit-tools.Dockerfile -t circuit-tools:dev .
```

## Language and commit conventions

- Issues, pull requests, docs, code comments, identifiers, and commit messages
  are written in English. Keep comments to the minimum necessary.
- Do not use `git add .`, amend, `--no-verify`, force-push, push to main, or
  destructive reset/clean/checkout.
- Split dependent changes into bottom-up stacked PRs, each independently
  verifiable.

## Architecture decisions

Significant decisions are recorded as ADRs in `docs/adr/`. If your change alters
an accepted decision, add a new ADR or update the existing one in the same
change.

## License

Contributions are licensed under BSD-3-Clause. The Konnect AGPL boundary must
be kept: never import-bind GPL/AGPL code into Python — Konnect runs only as an
unmodified separate process. See `THIRD_PARTY_NOTICES.md` and
`docs/adr/ADR-0003-konnect-agpl-boundary.md`.
