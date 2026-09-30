"""In-memory fakes: no network, no live Odoo."""
from __future__ import annotations

import operator
import shutil
import tempfile
from pathlib import Path

from odoo_accountant.config import Config
from odoo_accountant.models import ExecutionAuthorization

_OPS = {"=": operator.eq, "!=": operator.ne, ">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}


def _m2o(v):
    return v[0] if isinstance(v, (list, tuple)) and v else v


def _match(row: dict, leaf) -> bool:
    field, op, val = leaf
    cur = _m2o(row.get(field))
    if op == "in":
        return cur in val
    if op == "not in":
        return cur not in val
    if op in _OPS:
        if val is False:
            return (not cur) if op == "=" else bool(cur)
        try:
            return _OPS[op](cur, val)
        except TypeError:
            return False
    return True


class FakeOdoo:
    """Implements the read API + _mutate; records every call."""

    def __init__(self, tables: dict | None = None):
        self.tables = tables or {}
        self.reads: list = []
        self.mutations: list = []
        self.next_id = 1000

    def _rows(self, model, domain):
        rows = self.tables.get(model, [])
        return [r for r in rows if all(_match(r, l) for l in domain if isinstance(l, (list, tuple)) and len(l) == 3 and "." not in str(l[0]))]

    def search_count(self, model, domain):
        self.reads.append(("search_count", model)); return len(self._rows(model, domain))

    def search_read(self, model, domain, fields, limit=None, order=None, offset=None):
        self.reads.append(("search_read", model)); rows = self._rows(model, domain)
        return [dict(r) for r in (rows[:limit] if limit else rows)]

    def read(self, model, ids, fields):
        self.reads.append(("read", model))
        return [dict(r) for r in self.tables.get(model, []) if r["id"] in ids]

    def formatted_read_group(self, model, domain, groupby, aggregates, order=None, limit=None):
        self.reads.append(("formatted_read_group", model)); return []

    def fields_get(self, model, attributes=None):
        self.reads.append(("fields_get", model)); return {}

    def _mutate(self, model, method, body, authorization):
        assert isinstance(authorization, ExecutionAuthorization)
        self.mutations.append((model, method, body))
        tbl = self.tables.setdefault(model, [])
        if model == "account.move" and method == "action_post":
            for r in tbl:
                if r["id"] in body["ids"]: r["state"] = "posted"
        elif model == "account.move" and method == "write":
            for r in tbl:
                if r["id"] in body["ids"]: r.update(body["vals"])
        elif model == "account.move" and method == "create":
            self.next_id += 1
            vals = body["vals_list"][0]
            untaxed = sum(l[2]["quantity"] * l[2]["price_unit"] for l in vals["invoice_line_ids"])
            tbl.append({"id": self.next_id, "state": "draft", "move_type": vals["move_type"], "partner_id": [vals["partner_id"], "P"],
                        "amount_untaxed": untaxed, "amount_total": untaxed * 1.15, "currency_id": [1, "SAR"], "name": "/"})
            return [self.next_id]
        elif model == "account.payment.register" and method == "create":
            return [1]
        elif model == "account.payment.register" and method == "action_create_payments":
            amt = self.pending_payment
            for r in self.tables["account.move"]:
                if r["id"] == self.pay_move: 
                    r["amount_residual"] -= amt
                    r["payment_state"] = "paid" if r["amount_residual"] <= 0.001 else "partial"
        return None


def seed_invoice_tables() -> dict:
    return {
        "account.move": [
            {"id": 1, "name": "INV/1", "state": "draft", "move_type": "out_invoice", "partner_id": [7, "P"], "invoice_date": "2026-09-01",
             "amount_total": 115.0, "amount_residual": 115.0, "payment_state": "not_paid", "currency_id": [1, "SAR"]},
            {"id": 2, "name": "INV/2", "state": "posted", "move_type": "out_invoice", "partner_id": [7, "P"], "invoice_date": "2026-09-01",
             "amount_total": 230.0, "amount_residual": 230.0, "payment_state": "not_paid", "currency_id": [1, "SAR"]},
        ],
        "account.journal": [{"id": 5, "name": "Bank", "type": "bank", "currency_id": False}, {"id": 6, "name": "Sales", "type": "sale", "currency_id": False}],
        "res.partner": [{"id": 7, "display_name": "P", "email": "x@example.com"}],
        "account.tax": [{"id": 3, "name": "15%", "amount": 15.0, "amount_type": "percent", "type_tax_use": "sale", "price_include": False}],
    }


class TempRuntime:
    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="oa_test_"))
        self.config = Config(url="https://odoo.invalid", db="testdb", login="tester", runtime_dir=self.dir)
        return self

    def __exit__(self, *a):
        shutil.rmtree(self.dir, ignore_errors=True)
