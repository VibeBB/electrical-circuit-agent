import json
from pathlib import Path

from openhands.sdk.hooks import HookConfig
from openhands.sdk.mcp.config import coerce_mcp_config
from openhands.sdk.plugin import Plugin
from openhands.sdk.skills import PathTrigger

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "circuit"


def test_plugin_loads_all_assets() -> None:
    plugin = Plugin.load(PLUGIN)
    assert plugin.name == "circuit"
    assert len(plugin.agents) == 4
    assert {agent.name for agent in plugin.agents} == {
        "circuit-brief",
        "circuit-schematic",
        "circuit-layout",
        "circuit-review",
    }
    assert len(plugin.skills) == 6
    assert set(plugin.mcp_config) == {"circuit", "konnect"}
    assert plugin.hooks is not None
    assert plugin.entry_slash_command == "/circuit:doctor"
    guard_trigger_types = {
        type(skill.trigger) for skill in plugin.skills if skill.name == "circuit-library-guard"
    }
    assert guard_trigger_types == {PathTrigger}
    assert any(skill.name == "circuit-library-guard" for skill in plugin.skills)

    protect_command = plugin.hooks.pre_tool_use[0].hooks[0].command
    assert plugin.hooks.stop[0].hooks[0].name == "report-design-status"
    assert all(agent.max_budget_per_run == 3.0 for agent in plugin.agents)
    assert all(len(agent.when_to_use_examples) >= 2 for agent in plugin.agents)
    for agent in plugin.agents:
        assert agent.hooks is not None
        assert agent.hooks.pre_tool_use[0].hooks[0].command == protect_command


def test_plugin_assets_validate_directly() -> None:
    config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    assert set(coerce_mcp_config(config["mcpServers"])) == {"circuit", "konnect"}
    hooks = HookConfig.load(PLUGIN / "hooks" / "hooks.json")
    assert hooks.pre_tool_use
    assert hooks.session_start
    assert hooks.stop


def test_plugin_command_argument_hints() -> None:
    plugin = Plugin.load(PLUGIN)
    assert {command.name: command.argument_hint for command in plugin.commands} == {
        "design": "<project_dir> <requirements summary>",
        "doctor": None,
        "drc": "<board.kicad_pcb>",
        "erc": "<schematic.kicad_sch>",
        "export": "<board.kicad_pcb> <kind>",
    }
