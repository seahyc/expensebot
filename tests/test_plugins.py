"""Tests for the plugin extensibility registry (bot/plugins/registry.py).

Covers:
- load_enabled_skills returns empty string when all plugins disabled
- load_enabled_tools returns empty list when all plugins disabled
- enabled_plugins_by_hook returns correct names for a given hook
- enabling a plugin causes load_enabled_skills to include its skill content
- missing skill_md logs a warning gracefully (no exception)
- enabling a plugin with a valid tools_module loads its TOOLS
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from bot.plugins import registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _with_plugins(overrides: dict) -> dict:
    """Return a copy of PLUGINS with the given plugin configs overridden."""
    plugins = {}
    for name, cfg in registry.PLUGINS.items():
        plugins[name] = dict(cfg)
    for name, cfg in overrides.items():
        if name in plugins:
            plugins[name] = dict(plugins[name], **cfg)
        else:
            plugins[name] = cfg
    return plugins


# ---------------------------------------------------------------------------
# load_enabled_skills — none enabled
# ---------------------------------------------------------------------------

def test_load_enabled_skills_none_enabled():
    """When all plugins are disabled, load_enabled_skills returns empty string."""
    result = registry.load_enabled_skills()
    assert result == ""


# ---------------------------------------------------------------------------
# load_enabled_tools — none enabled
# ---------------------------------------------------------------------------

def test_load_enabled_tools_none_enabled():
    """When all plugins are disabled, load_enabled_tools returns empty list."""
    result = registry.load_enabled_tools()
    assert result == []


# ---------------------------------------------------------------------------
# enabled_plugins_by_hook
# ---------------------------------------------------------------------------

def test_enabled_plugins_by_hook_none_enabled():
    """When no plugins are enabled, no hooks fire."""
    result = registry.enabled_plugins_by_hook("on_receipt")
    assert result == []


def test_enabled_plugins_by_hook_with_enabled_plugin():
    """Enabling a plugin returns it from the correct hook query."""
    patched = _with_plugins({
        "miles_maximiser": {"enabled": True},
    })
    with patch.object(registry, "PLUGINS", patched):
        on_receipt = registry.enabled_plugins_by_hook("on_receipt")
        on_demand = registry.enabled_plugins_by_hook("on_demand")

    assert "miles_maximiser" in on_receipt
    assert "tax_advisor" not in on_receipt
    assert "miles_maximiser" not in on_demand


def test_enabled_plugins_by_hook_on_demand():
    """Tax advisor fires on on_demand hook when enabled."""
    patched = _with_plugins({
        "tax_advisor": {"enabled": True},
    })
    with patch.object(registry, "PLUGINS", patched):
        on_demand = registry.enabled_plugins_by_hook("on_demand")
        on_receipt = registry.enabled_plugins_by_hook("on_receipt")

    assert "tax_advisor" in on_demand
    assert "tax_advisor" not in on_receipt


def test_enabled_plugins_by_hook_multiple():
    """Multiple plugins on the same hook all appear."""
    patched = _with_plugins({
        "miles_maximiser": {"enabled": True},
        "fraud_detection": {"enabled": True},
    })
    with patch.object(registry, "PLUGINS", patched):
        on_receipt = registry.enabled_plugins_by_hook("on_receipt")

    assert "miles_maximiser" in on_receipt
    assert "fraud_detection" in on_receipt
    assert "tax_advisor" not in on_receipt


# ---------------------------------------------------------------------------
# enabling a plugin loads its skill
# ---------------------------------------------------------------------------

def test_load_enabled_skills_loads_content(tmp_path):
    """When a plugin is enabled and skill_md exists, its content is returned."""
    skill_file = tmp_path / "test_skill.md"
    skill_file.write_text("# Test Skill\nDo some magic.")

    patched = _with_plugins({
        "miles_maximiser": {
            "enabled": True,
            "skill_md": str(skill_file),
        },
    })
    with patch.object(registry, "PLUGINS", patched):
        result = registry.load_enabled_skills()

    assert "# Test Skill" in result
    assert "Do some magic." in result


def test_load_enabled_skills_concatenates_multiple(tmp_path):
    """When multiple plugins are enabled, their skills are concatenated."""
    skill_a = tmp_path / "skill_a.md"
    skill_a.write_text("# Skill A")
    skill_b = tmp_path / "skill_b.md"
    skill_b.write_text("# Skill B")

    patched = _with_plugins({
        "miles_maximiser": {"enabled": True, "skill_md": str(skill_a)},
        "tax_advisor": {"enabled": True, "skill_md": str(skill_b), "hook": "on_demand"},
    })
    with patch.object(registry, "PLUGINS", patched):
        result = registry.load_enabled_skills()

    assert "# Skill A" in result
    assert "# Skill B" in result


# ---------------------------------------------------------------------------
# missing skill_md logs a warning gracefully (no exception)
# ---------------------------------------------------------------------------

def test_load_enabled_skills_missing_file_logs_warning(caplog, tmp_path):
    """A missing skill_md file produces a warning log, not an exception."""
    missing_path = str(tmp_path / "nonexistent_skill.md")

    patched = _with_plugins({
        "miles_maximiser": {
            "enabled": True,
            "skill_md": missing_path,
        },
    })

    with patch.object(registry, "PLUGINS", patched):
        with caplog.at_level(logging.WARNING, logger="bot.plugins.registry"):
            result = registry.load_enabled_skills()

    # Should not raise, should return empty, should warn
    assert result == ""
    assert any("miles_maximiser" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# enabling a plugin with a valid tools_module loads its TOOLS
# ---------------------------------------------------------------------------

def test_load_enabled_tools_loads_from_module():
    """When a plugin is enabled and its tools_module has TOOLS, they are returned."""
    fake_tools = [{"name": "fake_tool", "description": "A fake tool", "input_schema": {}}]

    import types
    fake_mod = types.ModuleType("bot.plugins._test_fake.tools")
    fake_mod.TOOLS = fake_tools

    patched = _with_plugins({
        "miles_maximiser": {
            "enabled": True,
            "tools_module": "bot.plugins._test_fake.tools",
        },
    })

    with patch.object(registry, "PLUGINS", patched):
        with patch.dict("sys.modules", {"bot.plugins._test_fake.tools": fake_mod}):
            result = registry.load_enabled_tools()

    assert result == fake_tools


def test_load_enabled_tools_missing_module_logs_warning(caplog):
    """A missing tools_module produces a warning log, not an exception."""
    patched = _with_plugins({
        "miles_maximiser": {
            "enabled": True,
            "tools_module": "bot.plugins.nonexistent_module.tools",
        },
    })

    with patch.object(registry, "PLUGINS", patched):
        with caplog.at_level(logging.WARNING, logger="bot.plugins.registry"):
            result = registry.load_enabled_tools()

    assert result == []
    assert any("miles_maximiser" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Stub tools files are empty (no accidental tool definitions)
# ---------------------------------------------------------------------------

def test_stub_tools_are_empty():
    """All stub tools.py files define TOOLS as empty lists."""
    from bot.plugins.miles.tools import TOOLS as miles_tools
    from bot.plugins.tax.tools import TOOLS as tax_tools
    from bot.plugins.fraud.tools import TOOLS as fraud_tools

    assert miles_tools == []
    assert tax_tools == []
    assert fraud_tools == []
