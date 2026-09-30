import json
import unittest

from odoo_accountant.approvals import ApprovalStore
from odoo_accountant.audit import AuditLog
from odoo_accountant.models import ChannelContext, CommandEnvelope
from odoo_accountant.service import AccountingEmployee
from tests.fakes import FakeOdoo, TempRuntime, seed_invoice_tables

CTX = ChannelContext(channel="cli", actor_id="tester")


class ServiceFlowTests(unittest.TestCase):
    def setUp(self):
        self.rt = TempRuntime().__enter__()
        self.fake = FakeOdoo(seed_invoice_tables())
        self.emp = AccountingEmployee(self.rt.config, client=self.fake)

    def tearDown(self): self.rt.__exit__()

    def cmd(self, name, **params):
        return self.emp.handle(CommandEnvelope(name, params, ChannelContext(channel="cli", actor_id="tester")))

    def propose_post(self, ids=(1,)):
        r = self.cmd("propose_action", action="post_move", params={"move_ids": list(ids)})
        self.assertTrue(r.ok, r.message)
        aid = r.data["approval"]["approval_id"]
        code = ApprovalStore(self.rt.config).read_code_file(aid)
        return r, aid, code

    def test_propose_never_returns_code_nor_mutates(self):
        r, aid, code = self.propose_post()
        self.assertNotIn(code, json.dumps(r.data))
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(r.data["approval"]["risk"], "FINANCIAL_FINAL")

    def test_full_cycle_post_move_with_readback(self):
        r, aid, code = self.propose_post()
        h = r.data["approval"]["payload_hash"]
        self.assertTrue(self.cmd("approve_action", approval_id=aid, code=code, payload_hash=h).ok)
        dry = self.cmd("execute_approved_action", approval_id=aid, code=code, dry_run=True)
        self.assertEqual(dry.data["status"], "dry_run")
        self.assertEqual(self.fake.mutations, [])
        res = self.cmd("execute_approved_action", approval_id=aid, code=code)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.data["status"], "executed")
        self.assertEqual(self.fake.mutations[0][:2], ("account.move", "action_post"))
        self.assertEqual(self.fake.tables["account.move"][0]["state"], "posted")
        self.assertTrue(all(ok for _, ok in res.data["verification"]["checks"]))
        # replay
        again = self.cmd("execute_approved_action", approval_id=aid, code=code)
        self.assertFalse(again.ok)
        self.assertEqual(len(self.fake.mutations), 1)
        self.assertTrue(AuditLog(self.rt.dir).verify_chain()[0])
        events = [e["event"] for e in AuditLog(self.rt.dir).read_all()]
        for needed in ("propose", "approve", "execute_start", "execute_result"):
            self.assertIn(needed, events)

    def test_mutation_never_without_approval(self):
        r, aid, code = self.propose_post()
        res = self.cmd("execute_approved_action", approval_id=aid, code=code)
        self.assertFalse(res.ok)
        self.assertEqual(self.fake.mutations, [])

    def test_wrong_code_no_mutation(self):
        r, aid, code = self.propose_post()
        self.cmd("approve_action", approval_id=aid, code=code, payload_hash=r.data["approval"]["payload_hash"])
        res = self.cmd("execute_approved_action", approval_id=aid, code="BADCODE1")
        self.assertFalse(res.ok)
        self.assertEqual(self.fake.mutations, [])

    def test_drift_blocks_execution(self):
        r, aid, code = self.propose_post()
        self.cmd("approve_action", approval_id=aid, code=code, payload_hash=r.data["approval"]["payload_hash"])
        self.fake.tables["account.move"][0]["amount_total"] = 999.0  # changed after proposal
        res = self.cmd("execute_approved_action", approval_id=aid, code=code)
        self.assertFalse(res.ok)
        self.assertIn("drift", res.message)
        self.assertEqual(self.fake.mutations, [])

    def test_idempotency_blocks_second_proposal_after_execution(self):
        r, aid, code = self.propose_post()
        self.cmd("approve_action", approval_id=aid, code=code, payload_hash=r.data["approval"]["payload_hash"])
        self.assertTrue(self.cmd("execute_approved_action", approval_id=aid, code=code).ok)
        r2 = self.cmd("propose_action", action="post_move", params={"move_ids": [1]})
        self.assertFalse(r2.ok)  # already posted -> blocker

    def test_same_key_reproposal_reports_duplicate(self):
        self.fake.tables["account.move"].append({"id": 3, "name": "INV/3", "state": "draft", "move_type": "out_invoice", "partner_id": [7, "P"], "invoice_date": "2026-09-01", "amount_total": 5.0, "currency_id": [1, "SAR"]})
        r = self.cmd("propose_action", action="post_move", params={"move_ids": [3]}, idempotency_key="fixed-key")
        aid = r.data["approval"]["approval_id"]; code = ApprovalStore(self.rt.config).read_code_file(aid)
        self.cmd("approve_action", approval_id=aid, code=code, payload_hash=r.data["approval"]["payload_hash"])
        self.assertTrue(self.cmd("execute_approved_action", approval_id=aid, code=code).ok)
        self.fake.tables["account.move"][2]["state"] = "draft"  # even if it looks draft again
        r2 = self.cmd("propose_action", action="post_move", params={"move_ids": [3]}, idempotency_key="fixed-key")
        self.assertFalse(r2.ok)
        self.assertEqual(r2.error, "duplicate")

    def test_blockers_and_unknown_and_destructive(self):
        self.assertFalse(self.cmd("propose_action", action="post_move", params={"move_ids": [2]}).ok)  # not draft
        self.assertFalse(self.cmd("propose_action", action="delete_record", params={}).ok)
        self.assertFalse(self.cmd("propose_action", action="raw_call", params={"method": "unlink"}).ok)
        self.assertFalse(self.cmd("nope").ok)
        self.assertEqual(self.fake.mutations, [])

    def test_unknown_params_rejected(self):
        r = self.cmd("propose_action", action="post_move", params={"move_ids": [1], "sudo": True})
        self.assertFalse(r.ok)

    def test_draft_invoice_creation_requires_explicit_tax_and_account(self):
        base = {"partner_id": 7, "invoice_date": "2026-09-29", "journal_id": 6, "lines": [{"name": "svc", "quantity": 1, "price_unit": 100, "product_id": 1}]}
        self.assertFalse(self.cmd("propose_action", action="create_draft_customer_invoice", params=base).ok)  # tax_ids missing
        good = dict(base, lines=[{"name": "svc", "quantity": 1, "price_unit": 100, "product_id": 1, "tax_ids": [3]}])
        r = self.cmd("propose_action", action="create_draft_customer_invoice", params=good)
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.data["approval"]["risk"], "DRAFT_WRITE")
        aid = r.data["approval"]["approval_id"]; code = ApprovalStore(self.rt.config).read_code_file(aid)
        self.assertTrue(self.cmd("approve_action", approval_id=aid, code=code).ok)  # hash optional for drafts
        res = self.cmd("execute_approved_action", approval_id=aid, code=code)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(self.fake.mutations[0][1], "create")

    def test_payment_readback(self):
        self.fake.pay_move, self.fake.pending_payment = 2, 100.0
        r = self.cmd("propose_action", action="register_payment", params={"move_id": 2, "amount": 100.0, "payment_date": "2026-09-29", "journal_id": 5})
        self.assertTrue(r.ok, r.message)
        aid = r.data["approval"]["approval_id"]; code = ApprovalStore(self.rt.config).read_code_file(aid)
        self.cmd("approve_action", approval_id=aid, code=code, payload_hash=r.data["approval"]["payload_hash"])
        res = self.cmd("execute_approved_action", approval_id=aid, code=code)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(self.fake.tables["account.move"][1]["amount_residual"], 130.0)

    def test_overpayment_is_blocker(self):
        r = self.cmd("propose_action", action="register_payment", params={"move_id": 2, "amount": 500.0, "payment_date": "2026-09-29", "journal_id": 5})
        self.assertFalse(r.ok)

    def test_reconcile_without_partner_is_blocked(self):
        t = self.fake.tables
        t["account.bank.statement.line"] = [{"id": 50, "amount": 230.0, "partner_id": False, "is_reconciled": False, "move_id": [60, "m"], "journal_id": [5, "Bank"], "currency_id": [1, "SAR"]}]
        r = self.cmd("propose_action", action="reconcile_statement_line", params={"statement_line_id": 50, "move_id": 2})
        self.assertFalse(r.ok)
        self.assertIn("partner", r.message)

    def test_period_lock_cannot_move_backwards(self):
        self.fake.tables["res.company"] = [{"id": 1, "name": "C", "fiscalyear_lock_date": "2026-06-30"}]
        r = self.cmd("propose_action", action="set_period_lock", params={"company_id": 1, "field": "fiscalyear_lock_date", "date": "2026-05-31"})
        self.assertFalse(r.ok)
        r = self.cmd("propose_action", action="set_period_lock", params={"company_id": 1, "field": "hard_lock_date", "date": "2026-07-31"})
        self.assertFalse(r.ok)

    def test_secrets_never_in_responses_or_audit(self):
        cfg = self.rt.config.__class__(url=self.rt.config.url, db="d", token="TOK-SECRET-123", runtime_dir=self.rt.dir)
        emp = AccountingEmployee(cfg, client=self.fake)
        r = emp.handle(CommandEnvelope("propose_action", {"action": "post_move", "params": {"move_ids": [1]}, "token": "TOK-SECRET-123"}, CTX))
        self.assertNotIn("TOK-SECRET-123", json.dumps(r.data, default=str))
        self.assertNotIn("TOK-SECRET-123", (self.rt.dir / "audit.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
