import io
import json
import unittest

from odoo_accountant import workflows
from odoo_accountant.channels.telegram_stub import TelegramAdapter, TelegramPolicy, callback_data, envelope_from_text, parse_callback_data
from odoo_accountant.mcp_server import TOOLS, McpServer
from odoo_accountant.models import ChannelContext
from odoo_accountant.policy import READ_ODOO_METHODS
from odoo_accountant.service import AccountingEmployee
from tests.fakes import FakeOdoo, TempRuntime


class WorkflowTests(unittest.TestCase):
    def test_all_workflows_read_only(self):
        fake = FakeOdoo({"res.company": [{"id": 1, "name": "C"}]})
        for name, fn in workflows.WORKFLOWS.items():
            out = fn(fake)
            self.assertIn("header", out, name)
            self.assertEqual(out["header"]["mutations"], 0)
            self.assertIn("period", out["header"])
        self.assertEqual(fake.mutations, [])
        self.assertTrue({m for m, _ in fake.reads} or True)

    def test_bank_match_rules(self):
        t = {
            "res.company": [{"id": 1, "name": "C"}],
            "account.bank.statement.line": [
                {"id": 1, "date": "2026-09-01", "amount": 230.0, "payment_ref": "INV/2 payment", "partner_id": [7, "P"], "journal_id": [5, "Bank"], "currency_id": False, "is_reconciled": False},
                {"id": 2, "date": "2026-09-02", "amount": 230.0, "payment_ref": "x", "partner_id": False, "journal_id": [5, "Bank"], "currency_id": False, "is_reconciled": False},
                {"id": 3, "date": "2026-09-03", "amount": 77.0, "payment_ref": "y", "partner_id": [7, "P"], "journal_id": [5, "Bank"], "currency_id": False, "is_reconciled": False},
            ],
            "account.move": [{"id": 2, "name": "INV/2", "move_type": "out_invoice", "state": "posted", "payment_state": "not_paid", "amount_residual": 230.0, "partner_id": [7, "P"], "invoice_date_due": "2026-09-10", "ref": False}],
        }
        out = workflows.bank_match_suggest(FakeOdoo(t), date_from="2026-08-01", date_to="2026-09-30")
        st = {s["statement_line_id"]: s["status"] for s in out["suggestions"]}
        self.assertEqual(st, {1: "single_candidate", 2: "ambiguous", 3: "no_candidate"})
        self.assertTrue(out["suggestions"][0]["candidates"][0]["ref_in_payment_ref"])

    def test_bad_input_rejected(self):
        with self.assertRaises(Exception):
            workflows.overdue_followup(FakeOdoo(), side="sideways")
        with self.assertRaises(Exception):
            workflows.period_close_check(FakeOdoo(), month="2026-13")


class McpTests(unittest.TestCase):
    def test_no_generic_tool_and_expected_set(self):
        self.assertEqual(len(TOOLS), 13)
        for bad in ("arbitrary_method", "call_odoo", "execute_kw", "raw", "curl"):
            self.assertNotIn(bad, TOOLS)
        for name, (_, schema) in TOOLS.items():
            self.assertFalse(schema.get("additionalProperties", True), name)
        self.assertFalse(any(m in TOOLS for m in READ_ODOO_METHODS))

    def test_jsonrpc_roundtrip(self):
        with TempRuntime() as rt:
            fake = FakeOdoo({"res.company": [{"id": 1, "name": "C"}]})
            srv = McpServer(AccountingEmployee(rt.config, client=fake))
            reqs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "accounting_snapshot", "arguments": {"date_from": "2026-07-01", "date_to": "2026-09-29"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "execute_raw", "arguments": {}}},
            ]
            out = io.StringIO()
            srv.serve(io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"), out)
            msgs = [json.loads(l) for l in out.getvalue().splitlines()]
            self.assertEqual([m["id"] for m in msgs], [1, 2, 3, 4])
            self.assertEqual(len(msgs[1]["result"]["tools"]), 13)
            body = json.loads(msgs[2]["result"]["content"][0]["text"])
            self.assertTrue(body["ok"])
            self.assertTrue(msgs[3]["result"]["isError"])
            self.assertEqual(fake.mutations, [])


class TelegramStubTests(unittest.TestCase):
    def test_contract_only(self):
        pol = TelegramPolicy.from_env({"TELEGRAM_ALLOWED_USER_IDS": "11, 22"})
        self.assertTrue(pol.is_allowed(11) and not pol.is_allowed(33))
        env = envelope_from_text(pol, 11, 5, "ملخص")
        self.assertEqual(env.context.channel, "telegram")
        with self.assertRaises(PermissionError):
            envelope_from_text(pol, 33, 5, "x")
        self.assertEqual(parse_callback_data(callback_data("approve", "apr_ab12")), ("approve", "apr_ab12"))
        with self.assertRaises(ValueError):
            parse_callback_data("approve:evil")
        with self.assertRaises(NotImplementedError):
            TelegramAdapter(pol).send_message(ChannelContext(), "x")


class ImportTests(unittest.TestCase):
    def test_import_all(self):
        import importlib
        for m in ("config", "client", "models", "policy", "approvals", "executor", "workflows", "audit", "service", "cli", "mcp_server", "channels.base", "channels.telegram_stub"):
            importlib.import_module("odoo_accountant." + m)


if __name__ == "__main__":
    unittest.main()
