# electrical-circuit-agent working contract

## Scope

- OpenHands Software Agent SDK v1.49.3
- Python 3.12 or later, uv, ruff, pyright strict, pytest
- KiCad 11 nightly (Ubuntu 26.04 `ppa:kicad/kicad-dev-nightly`)
- Konnect v0.12.1 (AGPL-3.0-only, separate process)

README, docs, issues, PRs, code comments, identifiers, and commit messages are
all written in English. Keep comments to the minimum necessary.

## Layout

```text
src/circuit/
plugins/circuit/       # OpenHands plugin
tests/
docs/
docs/adr/
libraries/
docker/
scripts/
```

`plugins/circuit` is the distribution asset, `src/circuit` is the runtime/MCP
boundary, `scripts/e2e_authoring.py` is the deterministic authoring entry point
from a design brief, and `tests` verifies the runtime and plugin assets. The
Pydantic models are authoritative for the design brief, intake provenance,
library gate, netlist gate, and design report contracts.

## Invariants

- ERC/DRC verdicts are based solely on `kicad-cli` JSON output.
- LLM self-reports, conversation text, and Konnect descriptions are never
  promoted to ERC/DRC verdicts.
- Missing tools, process failures, JSON parse failures, unexecuted gates, and
  unknowns are fail-closed.
- Always specify `encoding="utf-8"` when reading or writing artifacts and
  reports as text.
- Never import-bind GPL/AGPL code into Python. Konnect runs as an unmodified
  binary via subprocess.
- Never write API keys, tokens, or secrets to logs, inputs, or commits.
- Preserve the LICENSE, attribution, and provenance of externally sourced code,
  and document change history and pins.

## Plugin boundary

The OpenHands plugin lives at the `skills/`, `agents/`, `commands/`, `hooks/`,
and `.mcp.json` boundary. Use the SDK's `TaskToolSet` and `AgentDefinition` for
sub-agent delegation, together with `TaskTrackerTool`. Because plugin-level
hooks do not propagate to sub-agents, declare sub-agent-specific hooks in each
AgentDefinition's frontmatter.

`WorkflowToolSet` is not adopted at this time. Do not use the deprecated
`DelegateTool`. Do not build custom tool, event, history, task, or executor
infrastructure; delegate to the OpenHands SDK.

## Dependencies

PyPI dependencies are pinned in `pyproject.toml` and `uv.lock`. Dated package
versions from the KiCad PPA, the Konnect release, and the CERN submodule commit
are recorded in this repository's docs and Dockerfile. When updating, check the
primary sources and record the adoption decision and rationale in
`docs/operations.md`. If you add or change a Dockerfile ARG, or start using a
new external source (anything other than PyPI: another Git repository, an
apt/PPA, a release download, etc.), update the target definitions in
`scripts/check_dependency_updates.py`, its tests, and the corresponding
section of `docs/operations.md` in the same change. Dependency candidates are
aggregated by the weekly workflow into the "依存アップデート確認レポート"
(dependency update report) Issue. For deferred candidates, record the reason
and a re-check deadline in `scripts/dependency_update_deferrals.json`.

## Verification

The entry point for verification stages and command sequences is
`scripts/verify_all.py`.

```bash
uv run python scripts/verify_all.py --stage docs
uv run python scripts/verify_all.py --stage fast
```

## Git

Commit messages are written in English. Do not use `git add .`, amend,
`--no-verify`, force-push, push to main, or destructive reset/clean/checkout.
Split dependent changes into bottom-up stacked PRs, each independently
verifiable.
