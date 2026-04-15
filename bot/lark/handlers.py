"""Lark Open Platform handlers — same shape as Telegram, different SDK.

v3 deliverable. Stubbed for now to lock in the channel-adapter pattern.
"""

from __future__ import annotations


async def on_message(event):
    raise NotImplementedError("v3: lark p2p / group_at parser → bot.common pipeline")


async def on_file_uploaded(event):
    raise NotImplementedError("v3: download via lark file API → pipeline")
