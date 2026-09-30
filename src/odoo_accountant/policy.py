"""Risk classification and approval requirements. Deny by default."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .errors import PolicyError
from .models import Risk

READ_ODOO_METHODS = frozenset(
    {"search_count", "search_read", "read", "formatted_read_group", "read_group", "fields_get"}
)
DRAFT_ODOO_METHODS = frozenset({"create", "write", "activity_schedule"})
FINAL_ODOO_METHODS = frozenset(
    {
        "action_post",
        "button_cancel",
        "button_draft",
        "action_create_payments",
        "reconcile",
        "refund_moves",
        "reverse_moves",
        "message_post",
    }
)
DESTRUCTIVE_ODOO_METHODS = frozenset({"unlink"})

# Models whose plain create/write is financially final rather than draft-level.
FINAL_WRITE_MODELS = frozenset({"res.company", "account.move.line", "account.bank.statement.line"})

ACTION_RISK = {
    "create_draft_customer_invoice": Risk.DRAFT_WRITE,
    "create_draft_vendor_bill": Risk.DRAFT_WRITE,
    "update_draft_move": Risk.DRAFT_WRITE,
    "create_followup_activity": Risk.DRAFT_WRITE,
    "post_move": Risk.FINANCIAL_FINAL,
    "register_payment": Risk.FINANCIAL_FINAL,
    "reconcile_statement_line": Risk.FINANCIAL_FINAL,
    "create_credit_note": Risk.FINANCIAL_FINAL,
    "cancel_or_reverse_move": Risk.FINANCIAL_FINAL,
    "send_followup_message": Risk.FINANCIAL_FINAL,  # outward-facing, irreversible
    "set_period_lock": Risk.FINANCIAL_FINAL,
    "import_bank_statement_lines": Risk.FINANCIAL_FINAL,  # creates posted bank-statement entries
    # never executable by default; no handler exists for them
    "delete_record": Risk.DESTRUCTIVE,
    "unlink_record": Risk.DESTRUCTIVE,
}


@dataclass(frozen=True)
class Requirement:
    action: str
    risk: Risk
    allowed: bool
    needs_approval: bool
    explicit_payload_hash: bool
    ttl_seconds: int
    reason: str = ""


class Policy:
    def __init__(self, config: Config):
        self.config = config

    def classify_action(self, action: str) -> Risk:
        try:
            return ACTION_RISK[action]
        except KeyError:
            raise PolicyError(f"Unknown action '{action}' is not in the allowlist") from None

    def requirement(self, action: str) -> Requirement:
        risk = self.classify_action(action)
        if risk is Risk.DESTRUCTIVE:
            ok = self.config.allow_destructive
            return Requirement(
                action, risk, ok, True, True, self.config.ttl_financial_final,
                "" if ok else "Destructive actions are disabled by default",
            )
        if risk is Risk.FINANCIAL_FINAL:
            return Requirement(action, risk, True, True, True, self.config.ttl_financial_final)
        if risk is Risk.DRAFT_WRITE:
            return Requirement(action, risk, True, True, False, self.config.ttl_draft_write)
        return Requirement(action, risk, True, False, False, 0)

    def classify_odoo_call(self, model: str, method: str) -> Risk:
        return classify_odoo_call(model, method)


def classify_odoo_call(model: str, method: str) -> Risk:
    if method in READ_ODOO_METHODS:
        return Risk.READ
    if method in DESTRUCTIVE_ODOO_METHODS:
        return Risk.DESTRUCTIVE
    if method in FINAL_ODOO_METHODS:
        return Risk.FINANCIAL_FINAL
    if method in DRAFT_ODOO_METHODS:
        return Risk.FINANCIAL_FINAL if (method in {"create", "write"} and model in FINAL_WRITE_MODELS) else Risk.DRAFT_WRITE
    return Risk.DESTRUCTIVE  # unknown methods are denied
