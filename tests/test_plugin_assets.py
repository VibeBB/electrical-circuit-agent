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
    assert len(plugin.skills) == 7
    assert set(plugin.mcp_config) == {"circuit", "konnect"}
    assert plugin.hooks is not None
    assert plugin.entry_slash_command == "/circuit:doctor"
    guard_trigger_types = {
        type(skill.trigger) for skill in plugin.skills if skill.name == "circuit-library-guard"
    }
    assert guard_trigger_types == {PathTrigger}
    assert any(skill.name == "circuit-library-guard" for skill in plugin.skills)
    brief_trigger_types = {
        type(skill.trigger) for skill in plugin.skills if skill.name == "circuit-brief-rules"
    }
    assert brief_trigger_types == {PathTrigger}

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


def test_ensure_llm_profiles_provisions(tmp_path: Path) -> None:
    """The session_start hook clones active_profile into vibebb-* slots."""
    import subprocess
    import sys

    script = PLUGIN / "hooks" / "scripts" / "ensure_llm_profiles.py"
    assert script.is_file()
    home = tmp_path / "home"
    profiles = home / ".openhands" / "profiles"
    profiles.mkdir(parents=True)
    (home / ".openhands" / "settings.json").write_text(
        json.dumps({"active_profile": "test-model"}), encoding="utf-8"
    )
    template = {"schema_version": 1, "model": "test-model", "auth_type": "api_key"}
    (profiles / "test-model.json").write_text(json.dumps(template), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["missing"] == []
    assert (
        json.loads((profiles / "vibebb-author.json").read_text(encoding="utf-8"))["model"]
        == "test-model"
    )
    assert (
        json.loads((profiles / "vibebb-review.json").read_text(encoding="utf-8"))["model"]
        == "test-model"
    )
    # Idempotent: a second run provisions nothing and still reports ok.
    proc2 = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert json.loads(proc2.stdout)["findings"] == []


def test_ensure_llm_profiles_tolerates_missing_settings(tmp_path: Path) -> None:
    import subprocess
    import sys

    script = PLUGIN / "hooks" / "scripts" / "ensure_llm_profiles.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path / "nohome"), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["missing"] == ["vibebb-author", "vibebb-review"]
