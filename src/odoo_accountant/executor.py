"""Allowlisted action handlers + the executor (policy -> approval -> run -> verify -> audit)."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .approvals import ApprovalStore
from .audit import AuditLog
from .config import Config
from .errors import ApprovalError, OdooAccountantError, PolicyError, ValidationError
from .models import ExecutionAuthorization, ExecutionResult, Risk, canonical_json
from .policy import Policy
from .storage import atomic_write, file_lock

TOL = 0.011

# --------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------

def _int(p: dict, k: str, required: bool = True):
    v = p.get(k)
    if v is None:
        if required:
            raise ValidationError(f"Missing '{k}'")
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValidationError(f"'{k}' must be a positive integer")
    return v


def _num(p: dict, k: str, positive: bool = True):
    v = p.get(k)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValidationError(f"'{k}' must be a number")
    if (positive and v <= 0) or v < 0:
        raise ValidationError(f"'{k}' must be {'> 0' if positive else '>= 0'}")
    return float(v)


def _date(p: dict, k: str, required: bool = True):
    v = p.get(k)
    if v is None:
        if required:
            raise ValidationError(f"Missing '{k}'")
        return None
    try:
        _dt.date.fromisoformat(str(v))
    except ValueError:
        raise ValidationError(f"'{k}' must be YYYY-MM-DD") from None
    return str(v)


def _str(p: dict, k: str, max_len: int = 500, required: bool = True):
    v = p.get(k)
    if v is None or v == "":
        if required:
            raise ValidationError(f"Missing '{k}'")
        return None
    if not isinstance(v, str) or len(v) > max_len:
        raise ValidationError(f"'{k}' must be a string up to {max_len} chars")
    return v


def _int_list(p: dict, k: str, min_len: int = 1, max_len: int = 50):
    v = p.get(k)
    if not isinstance(v, list) or not (min_len <= len(v) <= max_len):
        raise ValidationError(f"'{k}' must be a list of {min_len}..{max_len} ids")
    if any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in v) or len(set(v)) != len(v):
        raise ValidationError(f"'{k}' must contain unique positive integers")
    return v


def _reject_unknown(params: dict, allowed: set) -> None:
    extra = set(params) - allowed - {"expected"}
    if extra:
        raise ValidationError("Unknown parameters: " + ", ".join(sorted(extra)))


def _m2o(v):
    return v[0] if isinstance(v, (list, tuple)) and v else v


def _one(client, model: str, rec_id: int, fields: list) -> dict | None:
    rows = client.read(model, [rec_id], fields)
    return rows[0] if rows else None


def _close(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= TOL


def _target(model: str, rec_id, label: str = "") -> dict:
    return {"model": model, "id": rec_id, "label": label}


def _res(blockers=None, warnings=None, **kw) -> dict:
    return {"blockers": blockers or [], "warnings": warnings or [], **kw}


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------

class Handler:
    name = ""
    keys: set = set()

    def validate(self, params: dict) -> None:
        _reject_unknown(params, self.keys)

    def preview(self, client, params: dict) -> dict:  # -> {summary,targets,blockers,warnings,expected,details}
        raise NotImplementedError

    def run(self, client, params: dict, auth: ExecutionAuthorization) -> dict:
        raise NotImplementedError

    def verify(self, client, params: dict, run_result: dict) -> dict:
        raise NotImplementedError


class _CreateDraftInvoice(Handler):
    move_type = ""
    journal_type = ""
    tax_use = ""
    label = ""
    keys = {"partner_id", "invoice_date", "journal_id", "currency_id", "ref", "lines", "allow_duplicate"}

    def validate(self, p):
        super().validate(p)
        _int(p, "partner_id"); _date(p, "invoice_date"); _int(p, "journal_id")
        _int(p, "currency_id", required=False); _str(p, "ref", 100, required=self.move_type == "in_invoice")
        lines = p.get("lines")
        if not isinstance(lines, list) or not (1 <= len(lines) <= 100):
            raise ValidationError("'lines' must be a list of 1..100 lines")
        for i, ln in enumerate(lines):
            if not isinstance(ln, dict):
                raise ValidationError(f"line {i}: must be an object")
            _reject_unknown(ln, {"name", "quantity", "price_unit", "tax_ids", "product_id", "account_id"})
            _str(ln, "name", 300); _num(ln, "quantity"); _num(ln, "price_unit", positive=False)
            if "tax_ids" not in ln or not isinstance(ln["tax_ids"], list):
                raise ValidationError(f"line {i}: 'tax_ids' must be given explicitly (use [] for no tax)")
            if any(isinstance(t, bool) or not isinstance(t, int) or t <= 0 for t in ln["tax_ids"]):
                raise ValidationError(f"line {i}: invalid tax id")
            if not ln.get("product_id") and not ln.get("account_id"):
                raise ValidationError(f"line {i}: product_id or account_id is required (accounts are never guessed)")

    def preview(self, client, p):
        blockers, warnings = [], []
        partner = _one(client, "res.partner", p["partner_id"], ["display_name"])
        journal = _one(client, "account.journal", p["journal_id"], ["name", "type", "currency_id"])
        if not partner: blockers.append("partner not found")
        if not journal:
            blockers.append("journal not found")
        elif journal["type"] != self.journal_type:
            blockers.append(f"journal type must be '{self.journal_type}', got '{journal['type']}'")
        tax_ids = sorted({t for ln in p["lines"] for t in ln["tax_ids"]})
        taxes = {t["id"]: t for t in (client.read("account.tax", tax_ids, ["name", "amount", "amount_type", "type_tax_use", "price_include"]) if tax_ids else [])}
        untaxed = tax = 0.0
        for ln in p["lines"]:
            sub = ln["quantity"] * ln["price_unit"]
            untaxed += sub
            for tid in ln["tax_ids"]:
                t = taxes.get(tid)
                if not t:
                    blockers.append(f"tax {tid} not found"); continue
                if t["type_tax_use"] != self.tax_use:
                    blockers.append(f"tax {tid} is '{t['type_tax_use']}', expected '{self.tax_use}'")
                if t["amount_type"] == "percent" and not t["price_include"]:
                    tax += sub * t["amount"] / 100.0
                else:
                    warnings.append(f"tax {tid} total is not estimable here (type/price_include)")
        if self.move_type == "in_invoice":
            dup = client.search_count("account.move", [["move_type", "=", "in_invoice"], ["partner_id", "=", p["partner_id"]], ["ref", "=", p["ref"]], ["state", "!=", "cancel"]])
            if dup and not p.get("allow_duplicate"):
                blockers.append(f"possible duplicate bill (same partner+ref, {dup} existing)")
        return _res(
            blockers, warnings,
            summary=f"{self.label}: partner {p['partner_id']}، {len(p['lines'])} بنود، قبل الضريبة ≈ {untaxed:.2f}، الضريبة ≈ {tax:.2f} (مسودة)",
            targets=[_target("res.partner", p["partner_id"], (partner or {}).get("display_name", "")), _target("account.journal", p["journal_id"], (journal or {}).get("name", ""))],
            expected={"untaxed": round(untaxed, 2)},
            details={"untaxed_estimate": round(untaxed, 2), "tax_estimate": round(tax, 2)},
        )

    def run(self, client, p, auth):
        lines = []
        for ln in p["lines"]:
            v = {"name": ln["name"], "quantity": ln["quantity"], "price_unit": ln["price_unit"], "tax_ids": [[6, 0, ln["tax_ids"]]]}
            if ln.get("product_id"): v["product_id"] = ln["product_id"]
            if ln.get("account_id"): v["account_id"] = ln["account_id"]
            lines.append([0, 0, v])
        vals = {"move_type": self.move_type, "partner_id": p["partner_id"], "invoice_date": p["invoice_date"], "journal_id": p["journal_id"], "invoice_line_ids": lines}
        if p.get("currency_id"): vals["currency_id"] = p["currency_id"]
        if p.get("ref"): vals["ref"] = p["ref"]
        ids = client._mutate("account.move", "create", {"vals_list": [vals]}, auth)
        return {"move_id": ids[0] if isinstance(ids, list) else ids}

    def verify(self, client, p, r):
        m = _one(client, "account.move", r["move_id"], ["name", "state", "move_type", "partner_id", "amount_untaxed", "amount_total", "currency_id"])
        checks = [
            ("state == draft", bool(m) and m["state"] == "draft"),
            ("move_type", bool(m) and m["move_type"] == self.move_type),
            ("partner", bool(m) and _m2o(m["partner_id"]) == p["partner_id"]),
            ("amount_untaxed", bool(m) and _close(m["amount_untaxed"], sum(l["quantity"] * l["price_unit"] for l in p["lines"]))),
        ]
        return {"ok": all(c[1] for c in checks), "checks": checks, "record": m}


class CreateDraftCustomerInvoice(_CreateDraftInvoice):
    name = "create_draft_customer_invoice"; move_type = "out_invoice"; journal_type = "sale"; tax_use = "sale"; label = "فاتورة عميل"


class CreateDraftVendorBill(_CreateDraftInvoice):
    name = "create_draft_vendor_bill"; move_type = "in_invoice"; journal_type = "purchase"; tax_use = "purchase"; label = "فاتورة مورد"


class UpdateDraftMove(Handler):
    name = "update_draft_move"
    keys = {"move_id", "changes"}
    ALLOWED = {"ref", "invoice_date", "invoice_date_due", "narration", "payment_reference", "partner_id", "invoice_payment_term_id"}

    def validate(self, p):
        super().validate(p)
        _int(p, "move_id")
        ch = p.get("changes")
        if not isinstance(ch, dict) or not ch:
            raise ValidationError("'changes' must be a non-empty object")
        bad = set(ch) - self.ALLOWED
        if bad:
            raise ValidationError("Fields not editable: " + ", ".join(sorted(bad)))
        for k in ("invoice_date", "invoice_date_due"):
            if k in ch: _date(ch, k)
        for k in ("partner_id", "invoice_payment_term_id"):
            if k in ch: _int(ch, k)

    def preview(self, client, p):
        fields = ["name", "state"] + list(p["changes"])
        m = _one(client, "account.move", p["move_id"], fields)
        blockers = []
        if not m: blockers.append("move not found")
        elif m["state"] != "draft": blockers.append(f"move is '{m['state']}', only draft moves can be edited")
        return _res(blockers, summary=f"تعديل مسودة {m['name'] if m else p['move_id']}: {', '.join(p['changes'])}",
                    targets=[_target("account.move", p["move_id"], (m or {}).get("name", ""))],
                    expected={"state": "draft"}, details={"before": {k: (m or {}).get(k) for k in p["changes"]}, "after": p["changes"]})

    def run(self, client, p, auth):
        client._mutate("account.move", "write", {"ids": [p["move_id"]], "vals": p["changes"]}, auth)
        return {"move_id": p["move_id"]}

    def verify(self, client, p, r):
        m = _one(client, "account.move", p["move_id"], ["state"] + list(p["changes"]))
        checks = [("still draft", bool(m) and m["state"] == "draft")]
        for k, v in p["changes"].items():
            checks.append((f"{k} updated", bool(m) and _m2o(m.get(k)) == v))
        return {"ok": all(c[1] for c in checks), "checks": checks}


_INVOICE_TYPES = {"out_invoice", "in_invoice", "out_refund", "in_refund"}


class PostMove(Handler):
    name = "post_move"
    keys = {"move_ids"}

    def validate(self, p):
        super().validate(p); _int_list(p, "move_ids")

    def preview(self, client, p):
        rows = client.read("account.move", p["move_ids"], ["name", "state", "move_type", "partner_id", "invoice_date", "amount_total", "currency_id"])
        found = {r["id"]: r for r in rows}
        blockers, expected = [], {}
        for mid in p["move_ids"]:
            r = found.get(mid)
            if not r: blockers.append(f"move {mid} not found"); continue
            if r["state"] != "draft": blockers.append(f"{r['name']} is '{r['state']}', not draft")
            if r["move_type"] in _INVOICE_TYPES and not r["partner_id"]: blockers.append(f"{r['name']} has no partner")
            if r["move_type"] == "in_invoice" and not r["invoice_date"]: blockers.append(f"{r['name']} has no invoice_date")
            expected[str(mid)] = {"amount_total": r["amount_total"], "currency_id": _m2o(r["currency_id"])}
        total = sum(v["amount_total"] for v in expected.values())
        return _res(blockers, summary=f"ترحيل {len(p['move_ids'])} قيد/فاتورة، الإجمالي {total:.2f}",
                    targets=[_target("account.move", i, found.get(i, {}).get("name", "")) for i in p["move_ids"]],
                    expected=expected, details={"records": rows})

    def run(self, client, p, auth):
        client._mutate("account.move", "action_post", {"ids": p["move_ids"]}, auth)
        return {"move_ids": p["move_ids"]}

    def verify(self, client, p, r):
        rows = {x["id"]: x for x in client.read("account.move", p["move_ids"], ["name", "state", "amount_total", "currency_id"])}
        checks = []
        for mid in p["move_ids"]:
            x, exp = rows.get(mid), p["expected"][str(mid)]
            checks.append((f"{mid} posted", bool(x) and x["state"] == "posted"))
            checks.append((f"{mid} amount/currency unchanged", bool(x) and _close(x["amount_total"], exp["amount_total"]) and _m2o(x["currency_id"]) == exp["currency_id"]))
        return {"ok": all(c[1] for c in checks), "checks": checks}


class RegisterPayment(Handler):
    name = "register_payment"
    keys = {"move_id", "amount", "payment_date", "journal_id", "payment_method_line_id", "memo"}

    def validate(self, p):
        super().validate(p)
        _int(p, "move_id"); _num(p, "amount"); _date(p, "payment_date"); _int(p, "journal_id")
        _int(p, "payment_method_line_id", required=False); _str(p, "memo", 200, required=False)

    def preview(self, client, p):
        m = _one(client, "account.move", p["move_id"], ["name", "state", "move_type", "payment_state", "amount_residual", "currency_id", "partner_id"])
        j = _one(client, "account.journal", p["journal_id"], ["name", "type", "currency_id"])
        blockers = []
        if not m: blockers.append("move not found")
        else:
            if m["state"] != "posted": blockers.append(f"{m['name']} is '{m['state']}', must be posted")
            if m["move_type"] not in ("out_invoice", "in_invoice"): blockers.append("only customer invoices / vendor bills supported")
            if m["payment_state"] not in ("not_paid", "partial"): blockers.append(f"payment_state is '{m['payment_state']}'")
            if p["amount"] - m["amount_residual"] > TOL: blockers.append(f"amount {p['amount']} exceeds residual {m['amount_residual']}")
        if not j: blockers.append("journal not found")
        else:
            if j["type"] not in ("bank", "cash"): blockers.append("journal must be bank or cash")
            if m and j["currency_id"] and _m2o(j["currency_id"]) != _m2o(m["currency_id"]): blockers.append("journal currency differs from invoice currency")
        exp = {"amount_residual": m["amount_residual"], "currency_id": _m2o(m["currency_id"]), "payment_state": m["payment_state"]} if m else {}
        return _res(blockers, summary=f"دفعة {p['amount']:.2f} على {m['name'] if m else p['move_id']} عبر {j['name'] if j else p['journal_id']} بتاريخ {p['payment_date']}",
                    targets=[_target("account.move", p["move_id"], (m or {}).get("name", "")), _target("account.journal", p["journal_id"], (j or {}).get("name", ""))],
                    expected=exp, details={"residual_after": round(m["amount_residual"] - p["amount"], 2) if m else None})

    def run(self, client, p, auth):
        ctx = {"active_model": "account.move", "active_ids": [p["move_id"]]}
        vals = {"amount": p["amount"], "payment_date": p["payment_date"], "journal_id": p["journal_id"]}
        if p.get("payment_method_line_id"): vals["payment_method_line_id"] = p["payment_method_line_id"]
        if p.get("memo"): vals["communication"] = p["memo"]
        wiz = client._mutate("account.payment.register", "create", {"context": ctx, "vals_list": [vals]}, auth)
        wid = wiz[0] if isinstance(wiz, list) else wiz
        client._mutate("account.payment.register", "action_create_payments", {"ids": [wid], "context": ctx}, auth)
        return {"wizard_id": wid}

    def verify(self, client, p, r):
        m = _one(client, "account.move", p["move_id"], ["payment_state", "amount_residual", "currency_id"])
        exp = p["expected"]
        checks = [
            ("residual reduced by amount", bool(m) and _close(m["amount_residual"], exp["amount_residual"] - p["amount"])),
            ("payment_state advanced", bool(m) and m["payment_state"] in ("partial", "paid", "in_payment")),
            ("currency unchanged", bool(m) and _m2o(m["currency_id"]) == exp["currency_id"]),
        ]
        return {"ok": all(c[1] for c in checks), "checks": checks, "record": m}


class ReconcileStatementLine(Handler):
    """NOTE: written from Odoo 19 ORM semantics; never exercised against live Odoo (no live writes in build)."""
    name = "reconcile_statement_line"
    keys = {"statement_line_id", "move_id"}

    def validate(self, p):
        super().validate(p); _int(p, "statement_line_id"); _int(p, "move_id")

    def _gather(self, client, p):
        st = _one(client, "account.bank.statement.line", p["statement_line_id"], ["amount", "partner_id", "is_reconciled", "move_id", "journal_id", "currency_id", "payment_ref"])
        inv = _one(client, "account.move", p["move_id"], ["name", "state", "move_type", "payment_state", "amount_residual", "currency_id", "partner_id"])
        return st, inv

    def preview(self, client, p):
        st, inv = self._gather(client, p)
        blockers, suspense_id, inv_line_id, target_acc = [], None, None, None
        if not st: blockers.append("statement line not found")
        if not inv: blockers.append("invoice not found")
        if st and inv:
            if st["is_reconciled"]: blockers.append("statement line already reconciled")
            if not st["partner_id"]: blockers.append("statement line has no partner (no auto-match without partner)")
            elif _m2o(st["partner_id"]) != _m2o(inv["partner_id"]): blockers.append("partner mismatch")
            if inv["state"] != "posted" or inv["payment_state"] not in ("not_paid", "partial"): blockers.append("invoice not open")
            want = inv["amount_residual"] if inv["move_type"] == "out_invoice" else -inv["amount_residual"]
            if inv["move_type"] not in ("out_invoice", "in_invoice"): blockers.append("unsupported move type")
            elif not _close(st["amount"], want): blockers.append(f"amount {st['amount']} != residual {want}")
            if st["currency_id"] and _m2o(st["currency_id"]) != _m2o(inv["currency_id"]): blockers.append("currency mismatch")
            j = _one(client, "account.journal", _m2o(st["journal_id"]), ["default_account_id"])
            lines = client.search_read("account.move.line", [["move_id", "=", _m2o(st["move_id"])]], ["account_id", "reconciled", "balance"])
            cands = [l for l in lines if _m2o(l["account_id"]) != _m2o((j or {}).get("default_account_id")) and not l["reconciled"]]
            if len(cands) != 1: blockers.append(f"expected exactly 1 suspense line, found {len(cands)}")
            else: suspense_id = cands[0]["id"]
            il = client.search_read("account.move.line", [["move_id", "=", p["move_id"]], ["account_id.account_type", "in", ["asset_receivable", "liability_payable"]], ["reconciled", "=", False]], ["account_id", "amount_residual"])
            if len(il) != 1: blockers.append(f"expected exactly 1 open receivable/payable line, found {len(il)}")
            else: inv_line_id, target_acc = il[0]["id"], _m2o(il[0]["account_id"])
        exp = {"amount": st["amount"], "amount_residual": inv["amount_residual"], "suspense_line_id": suspense_id, "invoice_line_id": inv_line_id, "account_id": target_acc} if (st and inv) else {}
        return _res(blockers, ["unverified against live Odoo: run first on a single low-value item"],
                    summary=f"مطابقة سطر كشف {p['statement_line_id']} ({st['amount'] if st else '?'}) مع {inv['name'] if inv else p['move_id']}",
                    targets=[_target("account.bank.statement.line", p["statement_line_id"]), _target("account.move", p["move_id"], (inv or {}).get("name", ""))],
                    expected=exp, details={})

    def run(self, client, p, auth):
        e = p["expected"]; st_partner = _m2o(_one(client, "account.bank.statement.line", p["statement_line_id"], ["partner_id"])["partner_id"])
        client._mutate("account.move.line", "write", {"ids": [e["suspense_line_id"]], "vals": {"account_id": e["account_id"], "partner_id": st_partner}}, auth)
        client._mutate("account.move.line", "reconcile", {"ids": [e["invoice_line_id"], e["suspense_line_id"]]}, auth)
        return {"reconciled": [e["invoice_line_id"], e["suspense_line_id"]]}

    def verify(self, client, p, r):
        st, inv = self._gather(client, p)
        e = p["expected"]
        checks = [
            ("statement line reconciled", bool(st) and st["is_reconciled"]),
            ("invoice residual reduced", bool(inv) and _close(inv["amount_residual"], e["amount_residual"] - abs(e["amount"]))),
        ]
        return {"ok": all(c[1] for c in checks), "checks": checks}


class CreateCreditNote(Handler):
    name = "create_credit_note"
    keys = {"move_id", "reason", "date", "journal_id"}

    def validate(self, p):
        super().validate(p); _int(p, "move_id"); _str(p, "reason", 200); _date(p, "date"); _int(p, "journal_id", required=False)

    def preview(self, client, p):
        m = _one(client, "account.move", p["move_id"], ["name", "state", "move_type", "amount_total", "currency_id", "payment_state"])
        blockers = []
        if not m: blockers.append("move not found")
        else:
            if m["state"] != "posted": blockers.append("source must be posted")
            if m["move_type"] not in ("out_invoice", "in_invoice"): blockers.append("source must be invoice/bill")
            if client.search_count("account.move", [["reversed_entry_id", "=", p["move_id"]], ["state", "!=", "cancel"]]): blockers.append("a credit note/reversal already exists for this move")
        return _res(blockers, summary=f"إشعار دائن كامل (مسودة) لـ {m['name'] if m else p['move_id']} بمبلغ {m['amount_total'] if m else '?'}",
                    targets=[_target("account.move", p["move_id"], (m or {}).get("name", ""))],
                    expected={"amount_total": m["amount_total"], "currency_id": _m2o(m["currency_id"]), "move_type": m["move_type"]} if m else {}, details={})

    def _wizard(self, client, p, auth, method):
        ctx = {"active_model": "account.move", "active_ids": [p["move_id"]]}
        vals = {"reason": p["reason"], "date": p["date"]}
        if p.get("journal_id"): vals["journal_id"] = p["journal_id"]
        wiz = client._mutate("account.move.reversal", "create", {"context": ctx, "vals_list": [vals]}, auth)
        wid = wiz[0] if isinstance(wiz, list) else wiz
        return client._mutate("account.move.reversal", method, {"ids": [wid], "context": ctx}, auth)

    def run(self, client, p, auth):
        self._wizard(client, p, auth, "refund_moves")
        return {}

    def verify(self, client, p, r):
        rows = client.search_read("account.move", [["reversed_entry_id", "=", p["move_id"]]], ["name", "state", "move_type", "amount_total", "currency_id"], limit=5)
        e = p["expected"]
        ok_rows = [x for x in rows if x["state"] == "draft" and _close(x["amount_total"], e["amount_total"]) and _m2o(x["currency_id"]) == e["currency_id"]]
        return {"ok": len(rows) == 1 and len(ok_rows) == 1, "checks": [("exactly one draft credit note, same total/currency", len(ok_rows) == 1 and len(rows) == 1)], "records": rows}


class CancelOrReverseMove(CreateCreditNote):
    name = "cancel_or_reverse_move"
    keys = {"move_id", "mode", "reason", "date"}

    def validate(self, p):
        Handler.validate(self, p)
        _int(p, "move_id"); _str(p, "reason", 200)
        if p.get("mode") not in ("cancel", "reverse"): raise ValidationError("'mode' must be 'cancel' or 'reverse'")
        if p["mode"] == "reverse": _date(p, "date")

    def preview(self, client, p):
        m = _one(client, "account.move", p["move_id"], ["name", "state", "payment_state", "amount_total", "currency_id"])
        blockers = []
        if not m: blockers.append("move not found")
        else:
            if m["state"] != "posted": blockers.append("move must be posted")
            if p["mode"] == "cancel" and m.get("payment_state") not in ("not_paid", False, None): blockers.append("cannot cancel a move with payments/reconciliation; use reverse")
            if p["mode"] == "reverse" and client.search_count("account.move", [["reversed_entry_id", "=", p["move_id"]], ["state", "!=", "cancel"]]): blockers.append("a reversal already exists")
        return _res(blockers, summary=f"{'إلغاء' if p['mode']=='cancel' else 'عكس (مسودة)'} {m['name'] if m else p['move_id']} — {p['reason']}",
                    targets=[_target("account.move", p["move_id"], (m or {}).get("name", ""))],
                    expected={"amount_total": m["amount_total"], "currency_id": _m2o(m["currency_id"])} if m else {}, details={})

    def run(self, client, p, auth):
        if p["mode"] == "cancel":
            client._mutate("account.move", "button_draft", {"ids": [p["move_id"]]}, auth)
            client._mutate("account.move", "button_cancel", {"ids": [p["move_id"]]}, auth)
        else:
            self._wizard(client, {**p, "date": p["date"]}, auth, "reverse_moves")
        return {}

    def verify(self, client, p, r):
        if p["mode"] == "cancel":
            m = _one(client, "account.move", p["move_id"], ["state"])
            return {"ok": bool(m) and m["state"] == "cancel", "checks": [("state == cancel", bool(m) and m["state"] == "cancel")]}
        return CreateCreditNote.verify(self, client, p, r)


class CreateFollowupActivity(Handler):
    name = "create_followup_activity"
    keys = {"move_id", "summary", "note", "date_deadline", "user_id"}

    def validate(self, p):
        super().validate(p); _int(p, "move_id"); _str(p, "summary", 200); _str(p, "note", 1000, required=False); _date(p, "date_deadline"); _int(p, "user_id")

    def _count(self, client, p):
        return client.search_count("mail.activity", [["res_model", "=", "account.move"], ["res_id", "=", p["move_id"]], ["user_id", "=", p["user_id"]]])

    def preview(self, client, p):
        m = _one(client, "account.move", p["move_id"], ["name"]); u = _one(client, "res.users", p["user_id"], ["name"])
        blockers = ([] if m else ["move not found"]) + ([] if u else ["user not found"])
        return _res(blockers, summary=f"نشاط متابعة على {m['name'] if m else p['move_id']} للمستخدم {u['name'] if u else p['user_id']} بحلول {p['date_deadline']}: {p['summary']}",
                    targets=[_target("account.move", p["move_id"], (m or {}).get("name", ""))],
                    expected={"activities_before": self._count(client, p) if m and u else 0}, details={})

    def run(self, client, p, auth):
        body = {"ids": [p["move_id"]], "act_type_xmlid": "mail.mail_activity_data_todo", "summary": p["summary"], "date_deadline": p["date_deadline"], "user_id": p["user_id"]}
        if p.get("note"): body["note"] = p["note"]
        client._mutate("account.move", "activity_schedule", body, auth)
        return {}

    def verify(self, client, p, r):
        after = self._count(client, p)
        return {"ok": after == p["expected"]["activities_before"] + 1, "checks": [("activity count +1", after == p["expected"]["activities_before"] + 1)]}


class SendFollowupMessage(Handler):
    name = "send_followup_message"
    keys = {"move_id", "body", "partner_ids", "allow_other_recipients"}

    def validate(self, p):
        super().validate(p); _int(p, "move_id"); _str(p, "body", 2000); _int_list(p, "partner_ids", 1, 5)

    def _msgs(self, client, p):
        return client.search_count("mail.message", [["model", "=", "account.move"], ["res_id", "=", p["move_id"]], ["message_type", "=", "comment"]])

    def preview(self, client, p):
        m = _one(client, "account.move", p["move_id"], ["name", "partner_id", "state"])
        blockers, warnings = [], []
        if not m: blockers.append("move not found")
        else:
            if m["state"] != "posted": blockers.append("only posted invoices")
            if not p.get("allow_other_recipients") and any(pid != _m2o(m["partner_id"]) for pid in p["partner_ids"]): blockers.append("recipients must be the invoice partner (set allow_other_recipients to override)")
        without_email = client.search_count("res.partner", [["id", "in", p["partner_ids"]], ["email", "=", False]])
        if without_email: warnings.append(f"{without_email} recipient(s) have no email on file")
        return _res(blockers, warnings, summary=f"إرسال رسالة متابعة على {m['name'] if m else p['move_id']} إلى الشركاء {p['partner_ids']}: «{p['body'][:80]}»",
                    targets=[_target("account.move", p["move_id"], (m or {}).get("name", ""))] + [_target("res.partner", i) for i in p["partner_ids"]],
                    expected={"messages_before": self._msgs(client, p) if m else 0}, details={"body": p["body"]})

    def run(self, client, p, auth):
        client._mutate("account.move", "message_post", {"ids": [p["move_id"]], "body": p["body"], "partner_ids": p["partner_ids"], "message_type": "comment", "subtype_xmlid": "mail.mt_comment"}, auth)
        return {}

    def verify(self, client, p, r):
        after = self._msgs(client, p)
        return {"ok": after == p["expected"]["messages_before"] + 1, "checks": [("message count +1", after == p["expected"]["messages_before"] + 1)]}


class SetPeriodLock(Handler):
    name = "set_period_lock"
    keys = {"company_id", "field", "date", "acknowledge_drafts"}
    FIELDS = ("fiscalyear_lock_date", "sale_lock_date", "purchase_lock_date", "tax_lock_date")  # hard_lock_date excluded: irreversible

    def validate(self, p):
        super().validate(p); _int(p, "company_id"); _date(p, "date")
        if p.get("field") not in self.FIELDS: raise ValidationError("field must be one of: " + ", ".join(self.FIELDS))

    def preview(self, client, p):
        c = _one(client, "res.company", p["company_id"], ["name", p["field"]])
        blockers, warnings = [], []
        if not c: blockers.append("company not found")
        else:
            cur = c.get(p["field"])
            if cur and p["date"] < cur: blockers.append(f"lock date cannot move backwards (current {cur})")
        drafts = client.search_count("account.move", [["state", "=", "draft"], ["date", "<=", p["date"]], ["company_id", "=", p["company_id"]]])
        if drafts and not p.get("acknowledge_drafts"): blockers.append(f"{drafts} draft moves on/before {p['date']} (set acknowledge_drafts to accept)")
        elif drafts: warnings.append(f"{drafts} draft moves on/before the lock date")
        return _res(blockers, warnings, summary=f"قفل {p['field']} للشركة {c['name'] if c else p['company_id']} حتى {p['date']} (كان: {(c or {}).get(p['field'])})",
                    targets=[_target("res.company", p["company_id"], (c or {}).get("name", ""))],
                    expected={"current": (c or {}).get(p["field"]), "drafts": drafts}, details={})

    def run(self, client, p, auth):
        client._mutate("res.company", "write", {"ids": [p["company_id"]], "vals": {p["field"]: p["date"]}}, auth)
        return {}

    def verify(self, client, p, r):
        c = _one(client, "res.company", p["company_id"], [p["field"]])
        ok = bool(c) and c.get(p["field"]) == p["date"]
        return {"ok": ok, "checks": [(f"{p['field']} == {p['date']}", ok)]}


HANDLERS: dict = {h.name: h for h in (
    CreateDraftCustomerInvoice(), CreateDraftVendorBill(), UpdateDraftMove(), PostMove(), RegisterPayment(),
    ReconcileStatementLine(), CreateCreditNote(), CancelOrReverseMove(), CreateFollowupActivity(),
    SendFollowupMessage(), SetPeriodLock(),
)}


def get_handler(action: str) -> Handler:
    try:
        return HANDLERS[action]
    except KeyError:
        raise PolicyError(f"No executor handler for action '{action}'") from None


def derive_idempotency_key(action: str, params: dict) -> str:
    return hashlib.sha256(canonical_json({"a": action, "p": params}).encode()).hexdigest()[:24]


# --------------------------------------------------------------------------
# idempotency store
# --------------------------------------------------------------------------

class IdempotencyStore:
    def __init__(self, config: Config):
        self.config = config
        self.path = Path(config.runtime_dir) / "idempotency.json"

    def _all(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def lookup(self, key: str) -> dict | None:
        with file_lock(self.config.runtime_dir):
            return self._all().get(key)

    def set(self, key: str, **fields) -> None:
        with file_lock(self.config.runtime_dir):
            data = self._all()
            data[key] = {**data.get(key, {}), **fields, "updated_at": time.time()}
            atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=1))


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------

class Executor:
    def __init__(self, config: Config, client, store: ApprovalStore, audit: AuditLog, policy: Policy | None = None):
        self.config, self.client, self.store, self.audit = config, client, store, audit
        self.policy = policy or Policy(config)
        self.idem = IdempotencyStore(config)

    def _blocked(self, action, msg, ctx, approval_id=None, **extra) -> ExecutionResult:
        self.audit.append("execute_blocked", request_id=ctx.request_id, channel=ctx.channel, actor=ctx.actor_id, action=action, approval_id=approval_id, result=msg, **extra)
        return ExecutionResult(False, action, "blocked", msg, extra)

    def execute(self, approval_id: str, code: str, ctx, dry_run: bool = False, payload_hash: str | None = None) -> ExecutionResult:
        try:
            rec = self.store.check_executable(approval_id, code)
        except ApprovalError as exc:
            return self._blocked("?", str(exc), ctx, approval_id)
        action, params = rec["action"], rec["params"]
        req = self.policy.requirement(action)
        if not req.allowed:
            return self._blocked(action, req.reason or "not allowed", ctx, approval_id)
        if payload_hash and payload_hash.strip().lower() != rec["payload_hash"]:
            return self._blocked(action, "payload_hash does not match the approved payload", ctx, approval_id)
        handler = get_handler(action)
        try:
            handler.validate(params)
            prev = handler.preview(self.client, params)
        except OdooAccountantError as exc:
            return self._blocked(action, f"precheck failed: {exc}", ctx, approval_id)
        if prev["blockers"]:
            return self._blocked(action, "precheck blockers: " + "; ".join(prev["blockers"]), ctx, approval_id)
        if canonical_json(prev["expected"]) != canonical_json(params.get("expected", {})):
            return self._blocked(action, "state drifted since proposal; re-propose", ctx, approval_id, expected=params.get("expected"), actual=prev["expected"])
        prior = self.idem.lookup(rec["idempotency_key"])
        if prior:
            if prior.get("status") == "completed":
                return ExecutionResult(True, action, "duplicate", "already executed under this idempotency key", {"prior": prior})
            return self._blocked(action, f"idempotency key is '{prior.get('status')}'; outcome unknown, verify in Odoo and use a new key", ctx, approval_id)
        if dry_run:
            self.audit.append("execute_dry_run", request_id=ctx.request_id, channel=ctx.channel, actor=ctx.actor_id, action=action, approval_id=approval_id, targets=prev["targets"])
            return ExecutionResult(True, action, "dry_run", "dry-run: approval valid, nothing executed", {"preview": prev})
        try:
            auth = self.store.authorize_execution(approval_id, code, rec["payload_hash"])
        except ApprovalError as exc:
            return self._blocked(action, str(exc), ctx, approval_id)
        self.idem.set(rec["idempotency_key"], status="in_progress", approval_id=approval_id, action=action)
        self.audit.append("execute_start", request_id=ctx.request_id, channel=ctx.channel, actor=ctx.actor_id, action=action, approval_id=approval_id, targets=prev["targets"], before=prev["details"], payload_hash=rec["payload_hash"])
        try:
            run_result = handler.run(self.client, params, auth)
        except Exception as exc:  # noqa: BLE001 - outcome must be audited, never leaked
            self.idem.set(rec["idempotency_key"], status="failed_review", error=type(exc).__name__)
            self.audit.append("execute_failed", request_id=ctx.request_id, channel=ctx.channel, actor=ctx.actor_id, action=action, approval_id=approval_id, result=f"{type(exc).__name__}: {str(exc)[:300]}")
            return ExecutionResult(False, action, "failed", f"execution failed ({type(exc).__name__}); verify Odoo state manually before retrying", {"error": str(exc)[:300]})
        try:
            verification = handler.verify(self.client, params, run_result)
        except Exception as exc:  # noqa: BLE001
            verification = {"ok": False, "checks": [("verification raised " + type(exc).__name__, False)]}
        status = "executed" if verification.get("ok") else "executed_unverified"
        self.idem.set(rec["idempotency_key"], status="completed", approval_id=approval_id, verified=bool(verification.get("ok")), result=run_result)
        self.audit.append("execute_result", request_id=ctx.request_id, channel=ctx.channel, actor=ctx.actor_id, action=action, approval_id=approval_id, targets=prev["targets"], before=prev["details"], after=verification, result=status)
        msg = "تم التنفيذ والتحقق" if verification.get("ok") else "نُفّذ لكن التحقق فشل — راجع Odoo فورًا"
        return ExecutionResult(bool(verification.get("ok")), action, status, msg, {"run": run_result}, verification)
