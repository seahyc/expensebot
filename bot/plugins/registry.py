"""Plugin registry — feature-flagged capabilities loaded at startup."""
from __future__ import annotations
import importlib
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PLUGINS: dict[str, dict[str, Any]] = {
    "miles_maximiser": {
        "enabled": False,
        "skill_md": "bot/plugins/miles/skill.md",
        "tools_module": "bot.plugins.miles.tools",
        "hook": "on_receipt",   # fires after parse_receipt, before confirm
        "description": "Credit card miles maximiser — suggests best card for each receipt",
    },
    "tax_advisor": {
        "enabled": False,
        "skill_md": "bot/plugins/tax/skill.md",
        "tools_module": "bot.plugins.tax.tools",
        "hook": "on_demand",    # only when user explicitly asks
        "description": "Income tax advisor — reads payslips/CPF from ~/Documents/BankStatements/",
    },
    "fraud_detection": {
        "enabled": False,
        "skill_md": "bot/plugins/fraud/skill.md",
        "tools_module": "bot.plugins.fraud.tools",
        "hook": "on_receipt",
        "description": "Fraud detection — cross-checks receipt against bank statements",
        "source": str(Path.home() / "Documents/BankStatements/scripts/pdf_txn_checker.py"),
    },
}


def load_enabled_skills() -> str:
    """Return concatenated skill markdown for all enabled plugins."""
    parts = []
    for name, cfg in PLUGINS.items():
        if not cfg.get("enabled"):
            continue
        skill_path = Path(cfg["skill_md"])
        if skill_path.exists():
            parts.append(skill_path.read_text())
            log.info("Plugin %s skill loaded", name)
        else:
            log.warning("Plugin %s skill_md not found: %s", name, skill_path)
    return "\n\n".join(parts)


def load_enabled_tools() -> list[dict]:
    """Return tool definitions from all enabled plugins."""
    tools = []
    for name, cfg in PLUGINS.items():
        if not cfg.get("enabled"):
            continue
        try:
            mod = importlib.import_module(cfg["tools_module"])
            tools.extend(getattr(mod, "TOOLS", []))
            log.info("Plugin %s tools loaded", name)
        except ImportError:
            log.warning("Plugin %s tools_module not found: %s", name, cfg["tools_module"])
    return tools


def enabled_plugins_by_hook(hook: str) -> list[str]:
    """Return names of enabled plugins that fire on the given hook."""
    return [name for name, cfg in PLUGINS.items() if cfg.get("enabled") and cfg.get("hook") == hook]
