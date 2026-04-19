from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
REPO_VOICES_DIR = Path(__file__).parent / "voices"


def voice_config_dir() -> Path:
    return Path(os.getenv("VOICE_CONFIG_DIR", "/app/data/voices"))


@dataclass(frozen=True)
class VoiceAssignment:
    voice: str
    locked: bool = False


@dataclass(frozen=True)
class VoicePack:
    voice_id: str
    agent_system: str
    copy: dict[str, str]

    def text(self, key: str, **kwargs: Any) -> str:
        try:
            template = self.copy[key]
        except KeyError as exc:
            raise KeyError(f"missing voice copy key: {key}") from exc
        return template.format(**kwargs)


def _voice_roots() -> list[Path]:
    roots: list[Path] = []
    cfg = voice_config_dir()
    if cfg not in roots:
        roots.append(cfg)
    if REPO_VOICES_DIR not in roots:
        roots.append(REPO_VOICES_DIR)
    return roots


def _voice_dir(voice_id: str) -> Path | None:
    for root in _voice_roots():
        candidate = root / voice_id
        if candidate.is_dir():
            return candidate
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        log.warning("invalid voice json at %s", path, exc_info=True)
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""
    except Exception:
        log.warning("invalid voice text at %s", path, exc_info=True)
        return ""


def _assignments_path() -> Path:
    return voice_config_dir() / "assignments.json"


def load_assignments() -> dict[str, VoiceAssignment]:
    raw = _read_json(_assignments_path())
    out: dict[str, VoiceAssignment] = {}
    for user_key, value in raw.items():
        if isinstance(value, str):
            out[user_key] = VoiceAssignment(voice=value)
            continue
        if isinstance(value, dict) and isinstance(value.get("voice"), str):
            out[user_key] = VoiceAssignment(
                voice=value["voice"],
                locked=bool(value.get("locked", False)),
            )
    return out


def _user_keys(user: dict[str, Any] | None) -> list[str]:
    if not user:
        return []
    keys: list[str] = []
    if user.get("channel") and user.get("channel_user_id"):
        keys.append(f"{user['channel']}:{user['channel_user_id']}")
    if user.get("id") is not None:
        keys.append(f"user:{user['id']}")
    if user.get("omnihr_email"):
        keys.append(f"email:{str(user['omnihr_email']).lower()}")
    return keys


def resolve_assignment(user: dict[str, Any] | None) -> VoiceAssignment:
    assignments = load_assignments()
    for key in _user_keys(user):
        assignment = assignments.get(key)
        if assignment:
            return assignment
    return VoiceAssignment(voice="default", locked=False)


def resolve_voice_id(user: dict[str, Any] | None = None) -> str:
    return resolve_assignment(user).voice


def _load_copy_map(voice_id: str) -> dict[str, str]:
    default_dir = _voice_dir("default")
    if not default_dir:
        raise RuntimeError("default voice pack is missing")
    default_copy = {
        str(k): str(v)
        for k, v in _read_json(default_dir / "copy.json").items()
        if isinstance(v, (str, int, float))
    }
    if voice_id == "default":
        return default_copy
    voice_dir = _voice_dir(voice_id)
    if not voice_dir:
        log.warning("voice '%s' not found; falling back to default", voice_id)
        return default_copy
    overlay = {
        str(k): str(v)
        for k, v in _read_json(voice_dir / "copy.json").items()
        if isinstance(v, (str, int, float))
    }
    return {**default_copy, **overlay}


def _load_agent_system(voice_id: str) -> str:
    default_dir = _voice_dir("default")
    if not default_dir:
        raise RuntimeError("default voice pack is missing")
    default_text = _read_text(default_dir / "agent_system.md")
    if voice_id == "default":
        return default_text
    voice_dir = _voice_dir(voice_id)
    if not voice_dir:
        return default_text
    text = _read_text(voice_dir / "agent_system.md")
    return text or default_text


def load_voice_pack(voice_id: str) -> VoicePack:
    return VoicePack(
        voice_id=voice_id,
        agent_system=_load_agent_system(voice_id),
        copy=_load_copy_map(voice_id),
    )


def voice_for_user(user: dict[str, Any] | None = None) -> VoicePack:
    return load_voice_pack(resolve_voice_id(user))


def default_voice() -> VoicePack:
    return load_voice_pack("default")


def build_agent_system_prompt(user: dict[str, Any] | None = None) -> str:
    return voice_for_user(user).agent_system


def memory_template(user: dict[str, Any] | None = None) -> str:
    return voice_for_user(user).text("memory_template")
