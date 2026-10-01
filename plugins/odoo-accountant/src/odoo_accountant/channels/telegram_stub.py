"""Telegram adapter CONTRACT ONLY — no bot token, no network.

TODO(telegram):
  * Long-poll/webhook receiver that turns `message` updates into CommandEnvelope.
  * Send approval requests with InlineKeyboard buttons [Approve] [Reject] using
    the callback_data built by `callback_data()`. The one-time code is sent to the
    allow-listed human chat only (deliver_approval_code), never to the model.
  * Read TELEGRAM_BOT_TOKEN from the environment at runtime (not in this repo).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from ..models import ChannelContext, CommandEnvelope
from .base import ChannelAdapter


@dataclass(frozen=True)
class TelegramPolicy:
    allowed_user_ids: frozenset

    @classmethod
    def from_env(cls, env: dict | None = None) -> "TelegramPolicy":
        env = os.environ if env is None else env
        raw = env.get("TELEGRAM_ALLOWED_USER_IDS", "")
        return cls(frozenset(x.strip() for x in raw.split(",") if x.strip()))

    def is_allowed(self, user_id) -> bool:
        return str(user_id) in self.allowed_user_ids


def callback_data(kind: str, approval_id: str) -> str:
    if kind not in ("approve", "reject"):
        raise ValueError("kind must be approve|reject")
    return f"{kind}:{approval_id}"


def parse_callback_data(data: str) -> tuple[str, str]:
    kind, _, approval_id = data.partition(":")
    if kind not in ("approve", "reject") or not approval_id.startswith("apr_"):
        raise ValueError("bad callback data")
    return kind, approval_id


def envelope_from_text(policy: TelegramPolicy, user_id, chat_id, text: str) -> CommandEnvelope:
    """Pure mapping of an incoming allow-listed message to a command (no I/O)."""
    if not policy.is_allowed(user_id):
        raise PermissionError("Telegram user is not allow-listed")
    ctx = ChannelContext(channel="telegram", actor_id=str(user_id), chat_id=str(chat_id))
    return CommandEnvelope(command="free_text", params={"text": text}, context=ctx)


class TelegramAdapter(ChannelAdapter):
    name = "telegram"

    def __init__(self, policy: TelegramPolicy):
        self.policy = policy

    def send_message(self, ctx, text):
        raise NotImplementedError("Telegram transport not implemented (contract only)")

    def request_approval(self, ctx, approval_view):
        raise NotImplementedError("Telegram transport not implemented (contract only)")

    def deliver_approval_code(self, ctx, approval_id, code):
        raise NotImplementedError("Telegram transport not implemented (contract only)")
