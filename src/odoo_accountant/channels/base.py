"""Channel contract: how the employee talks to a human (CLI, Claude Code, Telegram...)."""
from __future__ import annotations

import abc

from ..models import ChannelContext


class ChannelAdapter(abc.ABC):
    """A channel delivers messages and the one-time approval code to the *human*.

    The approval code must reach the human out-of-band, never the model.
    """

    name = "base"

    @abc.abstractmethod
    def send_message(self, ctx: ChannelContext, text: str) -> None: ...

    @abc.abstractmethod
    def request_approval(self, ctx: ChannelContext, approval_view: dict) -> None:
        """Present summary + Approve/Reject affordance (buttons on Telegram)."""

    @abc.abstractmethod
    def deliver_approval_code(self, ctx: ChannelContext, approval_id: str, code: str) -> str:
        """Deliver the one-time code to the human. Returns a hint safe to show the model."""


class LocalChannel(ChannelAdapter):
    """Claude Code / CLI: the code stays in .runtime/pending_codes (0600, denied to the agent).

    The human retrieves it with `python -m odoo_accountant.cli show-code <approval_id>`.
    """

    name = "local"

    def send_message(self, ctx, text):  # pragma: no cover - console only
        print(text)

    def request_approval(self, ctx, approval_view):
        return None

    def deliver_approval_code(self, ctx, approval_id, code):
        return (
            "الكود المؤقت محفوظ محليًا ولم يُعرض للنموذج. "
            f"يحصل عليه المستخدم بنفسه: python -m odoo_accountant.cli show-code {approval_id}"
        )
