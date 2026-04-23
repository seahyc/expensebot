"""Speech-to-text via Moonshine ONNX (CPU, offline, free).

Pattern borrowed from yjwong/open-shrimp, but in-process instead of shelling
out to a vendored binary. The model is loaded once on first use and kept in
memory for sub-second transcription of short voice notes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_MODEL_NAME = os.getenv("MOONSHINE_MODEL", "moonshine/base")

_model = None
_tokenizer = None
_load_lock = asyncio.Lock()


async def _ensure_model():
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer
    async with _load_lock:
        if _model is not None:
            return _model, _tokenizer
        # Import inside the lock so we don't pay the import cost at module load.
        from moonshine_onnx import MoonshineOnnxModel, load_tokenizer

        def _load():
            return MoonshineOnnxModel(model_name=_MODEL_NAME), load_tokenizer()

        log.info("Loading Moonshine model %s…", _MODEL_NAME)
        _model, _tokenizer = await asyncio.to_thread(_load)
        log.info("Moonshine model ready")
    return _model, _tokenizer


async def transcribe(audio_bytes: bytes, *, suffix: str = ".ogg") -> str:
    """Transcribe OGG/Opus (Telegram voice/video_note) bytes to text."""
    model, tokenizer = await _ensure_model()

    def _run() -> str:
        import librosa

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            audio, _ = librosa.load(tmp_path, sr=16_000)
            tokens = model.generate(audio[None, ...])
            out = tokenizer.decode_batch(tokens)
            return (out[0] if out else "").strip()
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    return await asyncio.to_thread(_run)
