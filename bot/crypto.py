"""At-rest encryption for secrets (Anthropic keys, OmniHR JWTs).

Uses libsodium SecretBox via pynacl. Key is derived from ENCRYPTION_KEY env var
(32 bytes, base64 or urlsafe_b64). Generate a fresh one:

    python -c "import secrets; print(secrets.token_urlsafe(32))"

Never log or DM the key. If it's lost/rotated, all stored secrets become
unrecoverable — users need to re-/setkey + re-/pair. That's by design.
"""

from __future__ import annotations

import base64
import hashlib
import os

from nacl.secret import SecretBox
from nacl.utils import random


def _key() -> bytes:
    raw = os.environ.get("ENCRYPTION_KEY")
    if not raw:
        raise RuntimeError(
            "ENCRYPTION_KEY missing — set in .env. "
            "Generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    # Accept either urlsafe-b64 (preferred) or raw bytes; normalize to 32-byte key
    try:
        derived = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception:
        derived = raw.encode()
    # If the supplied value isn't exactly 32 bytes, hash it (keeps startup forgiving)
    if len(derived) != SecretBox.KEY_SIZE:
        derived = hashlib.sha256(derived).digest()
    return derived


def encrypt(plaintext: str | None) -> str | None:
    if plaintext is None:
        return None
    box = SecretBox(_key())
    ct = box.encrypt(plaintext.encode("utf-8"), random(SecretBox.NONCE_SIZE))
    return base64.urlsafe_b64encode(ct).decode("ascii")


def decrypt(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    box = SecretBox(_key())
    raw = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
    return box.decrypt(raw).decode("utf-8")


def redact(s: str | None, keep: int = 4) -> str:
    if not s:
        return ""
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}…{s[-keep:]}"
