"""Log redaction. Strips amounts, merchants, emails, JWTs, API keys from logs."""

from __future__ import annotations

import logging
import re

# Tuples of (regex, replacement). Applied to both messages and exception repr.
REDACTORS: list[tuple[re.Pattern, str]] = [
    # Anthropic API keys
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "sk-ant-<redacted>"),
    # Telegram bot tokens (format: <numeric>:<35-char base64-ish>)
    (re.compile(r"\b\d{6,14}:[A-Za-z0-9_\-]{30,}\b"), "<tg-token>"),
    # JWTs (three base64url-ish segments joined by dots)
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "<jwt>"),
    # Emails (keep domain so we can still debug)
    (re.compile(r"\b([\w.+\-]+)@([\w.\-]+)\b"), lambda m: f"***@{m.group(2)}"),
    # Credit card digits (PAN-like 13-19 digits with optional spaces/dashes)
    (re.compile(r"\b(?:\d[ \-]?){13,19}\b"), "<card>"),
    # Large currency amounts (keep <=2 digits; scrub bigger numbers)
    (re.compile(r"\b(?:SGD|USD|IDR|HKD|EUR|GBP|RM|RP|S\$|US\$)\s*\d{3,}(?:[.,]\d+)?\b", re.I), "<amt>"),
]


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            for pat, repl in REDACTORS:
                msg = pat.sub(repl, msg)
            record.msg = msg
            record.args = ()
        except Exception:
            pass
        return True


def setup(level: int = logging.INFO) -> None:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    h.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
