import json
import unittest

from odoo_accountant.approvals import ApprovalStore
from odoo_accountant.audit import AuditLog, redact
from odoo_accountant.errors import ApprovalError
from odoo_accountant.models import ActionPlan, ChannelContext, Risk, compute_payload_hash
from odoo_accountant.policy import Policy
from tests.fakes import TempRuntime


class Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t


def make_plan(action="post_move", params=None, key="k1"):
    params = params or {"move_ids": [1], "expected": {"1": {"amount_total": 1.0, "currency_id": 1}}}
    return ActionPlan(action, params, key, Risk.FINANCIAL_FINAL, "s", compute_payload_hash(action, params, key))


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.rt = TempRuntime().__enter__()
        self.clock = Clock()
        self.store = ApprovalStore(self.rt.config, clock=self.clock)
        self.ctx = ChannelContext(channel="cli", actor_id="tester")
        self.policy = Policy(self.rt.config)

    def tearDown(self): self.rt.__exit__()

    def propose(self, action="post_move"):
        plan = make_plan()
        return plan, *self.store.propose(plan, self.ctx, self.policy.requirement(action))

    def test_happy_path_and_replay_blocked(self):
        plan, rec, code = self.propose()
        aid = rec["approval_id"]
        self.store.approve(aid, code, self.ctx, plan.payload_hash)
        auth = self.store.authorize_execution(aid, code, plan.payload_hash)
        self.assertEqual(auth.payload_hash, plan.payload_hash)
        with self.assertRaisesRegex(ApprovalError, "replay"):
            self.store.authorize_execution(aid, code)

    def test_execute_without_approval_fails(self):
        _, rec, code = self.propose()
        with self.assertRaises(ApprovalError):
            self.store.authorize_execution(rec["approval_id"], code)

    def test_financial_requires_explicit_hash(self):
        _, rec, code = self.propose()
        with self.assertRaises(ApprovalError):
            self.store.approve(rec["approval_id"], code, self.ctx)
        with self.assertRaises(ApprovalError):
            self.store.approve(rec["approval_id"], code, self.ctx, "0" * 64)

    def test_wrong_code_and_lockout(self):
        plan, rec, code = self.propose()
        for _ in range(5):
            with self.assertRaises(ApprovalError):
                self.store.approve(rec["approval_id"], "WRONG123", self.ctx, plan.payload_hash)
        with self.assertRaisesRegex(ApprovalError, "rejected"):
            self.store.approve(rec["approval_id"], code, self.ctx, plan.payload_hash)

    def test_expiry(self):
        plan, rec, code = self.propose()
        self.clock.t += 601
        with self.assertRaisesRegex(ApprovalError, "expired"):
            self.store.approve(rec["approval_id"], code, self.ctx, plan.payload_hash)

    def test_payload_tamper_detected(self):
        plan, rec, code = self.propose()
        path = self.store._path(rec["approval_id"])
        data = json.loads(path.read_text())
        data["params"]["move_ids"] = [999]
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ApprovalError, "tampered|mismatch"):
            self.store.approve(rec["approval_id"], code, self.ctx, plan.payload_hash)

    def test_payload_tamper_even_if_resigned_without_key_hash_check(self):
        plan, rec, code = self.propose()
        path = self.store._path(rec["approval_id"])
        data = json.loads(path.read_text())
        data["params"]["move_ids"] = [999]
        data["sig"] = self.store._sign(data)  # attacker who somehow got the key still breaks payload_hash
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ApprovalError, "Payload hash mismatch"):
            self.store.get(rec["approval_id"])

    def test_reject_and_non_approver(self):
        plan, rec, code = self.propose()
        with self.assertRaises(ApprovalError):
            self.store.approve(rec["approval_id"], code, ChannelContext(channel="telegram", actor_id="999"), plan.payload_hash)
        self.store.reject(rec["approval_id"], self.ctx, "no")
        with self.assertRaises(ApprovalError):
            self.store.approve(rec["approval_id"], code, self.ctx, plan.payload_hash)

    def test_code_is_stored_hashed_and_file_perms(self):
        _, rec, code = self.propose()
        raw = self.store._path(rec["approval_id"]).read_text()
        self.assertNotIn(code, raw)
        import os, stat
        self.assertEqual(stat.S_IMODE(os.stat(self.store._path(rec["approval_id"])).st_mode), 0o600)


class AuditTests(unittest.TestCase):
    def test_redaction(self):
        out = redact({"password": "p", "Authorization": "Bearer abc.def", "nested": {"api_key": "k", "note": "Bearer zzz123 here"}, "approval_code": "X", "ok": 1, "text": "has TOPSECRET inside"}, ["TOPSECRET"])
        blob = json.dumps(out)
        for leaked in ("abc.def", "zzz123", "TOPSECRET", '"p"', '"k"'):
            self.assertNotIn(leaked, blob)
        self.assertEqual(out["ok"], 1)

    def test_append_only_chain_and_tamper(self):
        with TempRuntime() as rt:
            log = AuditLog(rt.dir, secrets=["S3CR3T"])
            log.append("a", params={"token": "S3CR3T", "x": 1})
            log.append("b", result="value S3CR3T")
            self.assertEqual(log.verify_chain(), (True, 2))
            self.assertNotIn("S3CR3T", log.path.read_text())
            lines = log.path.read_text().splitlines()
            log.path.write_text(lines[0].replace('"x":1', '"x":2') + "\n" + lines[1] + "\n")
            self.assertFalse(log.verify_chain()[0])


if __name__ == "__main__":
    unittest.main()
