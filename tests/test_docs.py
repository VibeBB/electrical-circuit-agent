import re
from pathlib import Path
from typing import Any, cast


def test_adr_number_prefixes_are_unique() -> None:
    adr_dir = Path(__file__).parents[1] / "docs" / "adr"
    paths = sorted(adr_dir.glob("ADR-*.md"))
    matches = [re.match(r"ADR-(\d{4})-", path.name) for path in paths]

    assert all(match is not None for match in matches)
    numbers = [match.group(1) for match in matches if match is not None]
    assert len(numbers) == len(set(numbers))


ROOT = Path(__file__).parents[1]
DOCS = ROOT / "docs"


def _frontmatter(path: Path) -> dict[str, Any]:
    import yaml

    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match is not None, f"{path}: missing frontmatter"
    loaded = yaml.safe_load(match.group(1))
    if isinstance(loaded, dict):
        return cast(dict[str, Any], loaded)
    return {}


def test_every_mcp_tool_appears_in_mcp_docs() -> None:
    script = (ROOT / "scripts" / "check_plugin_load.py").read_text(encoding="utf-8")
    block = re.search(r"EXPECTED_CIRCUIT_MCP_TOOLS\s*=\s*\{(.*?)\}", script, re.DOTALL)
    assert block is not None
    expected = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    body = (DOCS / "mcp.md").read_text(encoding="utf-8")
    missing = {name for name in expected if name not in body}
    assert not missing, f"docs/mcp.md missing tools: {sorted(missing)}"


def test_every_agent_skill_command_file_appears_in_docs() -> None:
    agents = {p.stem for p in (ROOT / "plugins" / "circuit" / "agents").glob("*.md")}
    skills = {p.name for p in (ROOT / "plugins" / "circuit" / "skills").iterdir() if p.is_dir()}
    commands = {p.stem for p in (ROOT / "plugins" / "circuit" / "commands").glob("*.md")}
    for doc_name, names in (
        ("agents.md", agents),
        ("skills.md", skills),
        ("commands.md", commands),
    ):
        body = (DOCS / doc_name).read_text(encoding="utf-8")
        missing = {name for name in names if name not in body}
        assert not missing, f"docs/{doc_name} missing: {sorted(missing)}"


def test_every_hook_name_appears_in_hooks_docs() -> None:
    import json

    hooks = json.loads(
        (ROOT / "plugins" / "circuit" / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )
    names = {
        hook["name"] for groups in hooks.values() for group in groups for hook in group["hooks"]
    }
    for agent_path in (ROOT / "plugins" / "circuit" / "agents").glob("*.md"):
        frontmatter = _frontmatter(agent_path)
        for event_hooks in cast(dict[str, Any], frontmatter.get("hooks") or {}).values():
            for group in cast(list[dict[str, Any]], event_hooks):
                for hook in group.get("hooks", []):
                    names.add(cast(str, hook["name"]))
    body = (DOCS / "hooks.md").read_text(encoding="utf-8")
    missing = {name for name in names if name not in body}
    assert not missing, f"docs/hooks.md missing hooks: {sorted(missing)}"


def test_every_module_appears_in_modules_docs() -> None:
    body = (DOCS / "modules.md").read_text(encoding="utf-8")
    missing = {p.name for p in (ROOT / "src" / "circuit").glob("*.py") if p.name not in body}
    assert not missing, f"docs/modules.md missing modules: {sorted(missing)}"
