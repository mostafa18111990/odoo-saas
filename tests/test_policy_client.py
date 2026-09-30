import json
import unittest

from odoo_accountant.client import OdooClient
from odoo_accountant.config import Config
from odoo_accountant.errors import OdooClientError, OdooError, PolicyError
from odoo_accountant.models import ExecutionAuthorization, Risk
from odoo_accountant.policy import Policy, classify_odoo_call

CFG = Config(url="https://odoo.invalid", db="d", login="u")


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = Policy(CFG)

    def test_classification(self):
        for a in ("create_draft_customer_invoice", "create_draft_vendor_bill", "update_draft_move", "create_followup_activity"):
            self.assertIs(self.p.classify_action(a), Risk.DRAFT_WRITE)
        for a in ("post_move", "register_payment", "reconcile_statement_line", "create_credit_note",
                  "cancel_or_reverse_move", "set_period_lock", "send_followup_message"):
            self.assertIs(self.p.classify_action(a), Risk.FINANCIAL_FINAL)
        self.assertIs(self.p.classify_action("delete_record"), Risk.DESTRUCTIVE)

    def test_requirements(self):
        self.assertFalse(self.p.requirement("delete_record").allowed)
        r = self.p.requirement("post_move")
        self.assertTrue(r.needs_approval and r.explicit_payload_hash)
        self.assertFalse(self.p.requirement("update_draft_move").explicit_payload_hash)
        self.assertTrue(Policy(Config(url="u", db="d", allow_destructive=True)).requirement("delete_record").allowed)

    def test_unknown_action_denied(self):
        with self.assertRaises(PolicyError):
            self.p.classify_action("drop_database")

    def test_odoo_call_classes(self):
        self.assertIs(classify_odoo_call("account.move", "search_read"), Risk.READ)
        self.assertIs(classify_odoo_call("account.move", "unlink"), Risk.DESTRUCTIVE)
        self.assertIs(classify_odoo_call("account.move", "action_post"), Risk.FINANCIAL_FINAL)
        self.assertIs(classify_odoo_call("account.move", "sudo_anything"), Risk.DESTRUCTIVE)
        self.assertIs(classify_odoo_call("res.company", "write"), Risk.FINANCIAL_FINAL)
        self.assertIs(classify_odoo_call("account.move", "create"), Risk.DRAFT_WRITE)


class ClientTests(unittest.TestCase):
    def make(self, cfg=CFG, reply=(200, b"[]")):
        calls = []

        def transport(url, headers, body, timeout):
            calls.append((url, headers, json.loads(body)))
            return reply

        return OdooClient(cfg, transport=transport), calls

    def test_read_call_shape_and_no_auth_header_when_injected(self):
        c, calls = self.make(reply=(200, b"3"))
        c.search_count("account.move", [])
        url, headers, body = calls[0]
        self.assertTrue(url.endswith("/json/2/account.move/search_count"))
        self.assertEqual(headers["X-Odoo-Database"], "d")
        self.assertNotIn("Authorization", headers)

    def test_token_sent_but_never_in_errors(self):
        cfg = Config(url="https://odoo.invalid", db="d", token="SUPERSECRET")
        c, calls = self.make(cfg, (403, json.dumps({"name": "AccessError", "message": "no"}).encode()))
        with self.assertRaises(OdooError) as cm:
            c.search_count("account.move", [])
        self.assertEqual(calls[0][1]["Authorization"], "Bearer SUPERSECRET")
        self.assertNotIn("SUPERSECRET", str(cm.exception))
        self.assertNotIn("SUPERSECRET", repr(cfg))

    def test_write_methods_blocked_on_read_path(self):
        c, calls = self.make()
        with self.assertRaises(OdooClientError):
            c._call("account.move", "action_post", {"ids": [1]})
        self.assertEqual(calls, [])

    def test_mutation_needs_authorization(self):
        c, calls = self.make()
        with self.assertRaises(PermissionError):
            c._mutate("account.move", "action_post", {"ids": [1]}, None)
        with self.assertRaises(PermissionError):
            ExecutionAuthorization("apr_x", "h", "post_move")  # cannot be forged
        self.assertEqual(calls, [])

    def test_read_only_client_and_unlink_refused(self):
        from odoo_accountant.approvals import ApprovalStore  # noqa: F401
        from odoo_accountant.models import _INTERNAL_KEY
        auth = ExecutionAuthorization("apr_x", "h", "a", _key=_INTERNAL_KEY)
        c, calls = self.make()
        with self.assertRaises(OdooClientError):
            c._mutate("account.move", "unlink", {"ids": [1]}, auth)
        ro = OdooClient(CFG, transport=lambda *a: (200, b"[]"), read_only=True)
        with self.assertRaises(OdooClientError):
            ro._mutate("account.move", "action_post", {"ids": [1]}, auth)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
