"""Text-to-speech via Kokoro ONNX (CPU, offline, free).

Default voice `af_bella` is a warm feminine American English voice. Other
feminine options via env: `af_nicole` (breathy), `af_heart` (sweet),
`af_sarah` (clear), `bf_isabella` (British).

Output is 48 kHz mono OGG/Opus ready for Telegram `send_voice`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_MODEL_RELEASE_BASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
_MODEL_URL = f"{_MODEL_RELEASE_BASE}/kokoro-v1.0.onnx"
_VOICES_URL = f"{_MODEL_RELEASE_BASE}/voices-v1.0.bin"

_DEFAULT_MODEL_DIR = Path(os.getenv("KOKORO_MODEL_DIR", "/app/data/models"))
_MODEL_PATH = Path(os.getenv("KOKORO_MODEL_PATH", _DEFAULT_MODEL_DIR / "kokoro-v1.0.onnx"))
_VOICES_PATH = Path(os.getenv("KOKORO_VOICES_PATH", _DEFAULT_MODEL_DIR / "voices-v1.0.bin"))

DEFAULT_VOICE = os.getenv("KOKORO_VOICE", "af_bella")

_MAX_TTS_CHARS = int(os.getenv("TTS_MAX_CHARS", "600"))

_kokoro = None
_load_lock = asyncio.Lock()


async def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("Downloading %s → %s", url, dest)
    async with httpx.AsyncClient(follow_redirects=True, timeout=600.0) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(chunk_size=1 << 20):
                    f.write(chunk)
    tmp.rename(dest)
    log.info("Downloaded %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)


async def _ensure_models() -> None:
    if not _MODEL_PATH.exists():
        await _download(_MODEL_URL, _MODEL_PATH)
    if not _VOICES_PATH.exists():
        await _download(_VOICES_URL, _VOICES_PATH)


async def _ensure_kokoro():
    global _kokoro
    if _kokoro is not None:
        return _kokoro
    async with _load_lock:
        if _kokoro is not None:
            return _kokoro
        await _ensure_models()
        from kokoro_onnx import Kokoro

        def _load():
            return Kokoro(str(_MODEL_PATH), str(_VOICES_PATH))

        log.info("Loading Kokoro TTS model…")
        _kokoro = await asyncio.to_thread(_load)
        log.info("Kokoro TTS ready (voice=%s)", DEFAULT_VOICE)
    return _kokoro


_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_MD_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_EMPH = re.compile(r"(\*\*|\*|_{1,2})(.+?)\1")
_URL = re.compile(r"https?://\S+")

# Emoji + pictograph ranges — Kokoro/espeak-ng will otherwise speak the
# unicode name ("grinning face with smiling eyes"), which sounds terrible.
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"  # alchemical
    "\U0001F780-\U0001F7FF"  # geometric shapes ext
    "\U0001F800-\U0001F8FF"  # supplemental arrows
    "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols & pictographs ext-A
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"  # flags (regional indicators)
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"             # zero-width joiner
    "]+",
    flags=re.UNICODE,
)


def _strip_markdown(text: str) -> str:
    """Make Markdown-ish agent text speakable."""
    text = _MD_CODE_BLOCK.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    text = _MD_EMPH.sub(r"\2", text)
    text = _URL.sub("", text)
    text = _EMOJI.sub("", text)
    text = text.replace("#", "number ")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def prepare_for_tts(text: str, *, max_chars: int = _MAX_TTS_CHARS) -> str:
    """Strip markdown and cap length at a sentence boundary."""
    clean = _strip_markdown(text)
    if len(clean) <= max_chars:
        return clean
    # Cut at last sentence end before the cap.
    head = clean[:max_chars]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut > max_chars // 2:
        return head[: cut + 1]
    return head.rstrip() + "…"


def _pcm_to_ogg_opus(pcm_f32_bytes: bytes, sample_rate: int) -> bytes:
    """Pipe 32-bit float PCM through ffmpeg to 48 kHz mono OGG/Opus."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-f", "f32le",
            "-ar", str(sample_rate),
            "-ac", "1",
            "-i", "pipe:0",
            "-c:a", "libopus",
            "-b:a", "32k",
            "-ar", "48000",
            "-ac", "1",
            "-f", "ogg",
            "pipe:1",
        ],
        input=pcm_f32_bytes,
        capture_output=True,
        check=True,
    )
    return proc.stdout


async def synthesize(text: str, *, voice: str | None = None) -> bytes | None:
    """Generate a Telegram-ready OGG/Opus voice clip. Returns None on empty text."""
    speakable = prepare_for_tts(text)
    if not speakable:
        return None
    kokoro = await _ensure_kokoro()

    def _run() -> bytes:
        samples, sr = kokoro.create(speakable, voice=voice or DEFAULT_VOICE, speed=1.0, lang="en-us")
        # samples is float32 numpy; tobytes in native order is fine on x86/ARM little-endian hosts
        return _pcm_to_ogg_opus(samples.astype("float32").tobytes(), int(sr))

    return await asyncio.to_thread(_run)


async def prefetch() -> None:
    """Warm up on startup so the first voice note isn't 30s slow."""
    try:
        await _ensure_kokoro()
    except Exception:
        log.exception("TTS prefetch failed (will retry on demand)")
