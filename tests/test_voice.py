import json
from pathlib import Path

from bot import storage
from bot.server import step1_prompt
from bot.voice import build_agent_system_prompt, resolve_assignment, voice_for_user


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_default_voice_is_generic():
    voice = voice_for_user(None)
    assert voice.text("brand_name") == "Expensebot"
    assert "darling" not in voice.text("step1_prompt", name="Alex", brand_name="Expensebot").lower()
    assert "# Memory" in storage.DEFAULT_MEMORY_TEMPLATE


def test_owner_assignment_overrides_voice(monkeypatch, tmp_path):
    voice_root = tmp_path / "voices"
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(voice_root))
    _write(
        voice_root / "oswald" / "agent_system.md",
        "You are Oswald's expense assistant. Be flirtatious but still accurate.",
    )
    _write(
        voice_root / "oswald" / "copy.json",
        json.dumps(
            {
                "anonymous_name": "handsome",
                "step1_prompt": "Hello, {name}. I'm tuned for Oswald.",
                "agent_progress": "One sec, handsome…",
            }
        ),
    )
    _write(
        voice_root / "assignments.json",
        json.dumps({"telegram:154869784": {"voice": "oswald", "locked": True}}),
    )

    user = {"channel": "telegram", "channel_user_id": "154869784", "id": 3}
    assert resolve_assignment(user).voice == "oswald"
    assert resolve_assignment(user).locked is True
    assert step1_prompt(None, user) == "Hello, handsome. I'm tuned for Oswald."
    assert "flirtatious" in build_agent_system_prompt(user)


def test_missing_override_voice_falls_back_to_default(monkeypatch, tmp_path):
    voice_root = tmp_path / "voices"
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(voice_root))
    _write(voice_root / "assignments.json", json.dumps({"telegram:1": "missing"}))

    user = {"channel": "telegram", "channel_user_id": "1", "id": 1}
    voice = voice_for_user(user)
    assert voice.voice_id == "missing"
    assert voice.text("brand_name") == "Expensebot"
    assert "expense assistant" in voice.agent_system.lower()
