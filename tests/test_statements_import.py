import io
import json
import tempfile
import unittest
from pathlib import Path

from odoo_accountant.approvals import ApprovalStore
from odoo_accountant.audit import AuditLog
from odoo_accountant.errors import StatementError
from odoo_accountant.mcp_server import TOOLS, McpServer
from odoo_accountant.models import ChannelContext, CommandEnvelope
from odoo_accountant.service import AccountingEmployee
from odoo_accountant.statements import run_preview
from odoo_accountant.statements.dedupe import ImportRegistry
from odoo_accountant.statements.limits import MAX_BASE64_CHARS
from tests.fakes import FakeOdoo, TempRuntime, seed_statement_tables
from tests.statement_fixtures import CSV_STD, XLSX_ROWS, b64, build_xlsx, profile_std

TARGET = {"company_id": 1, "journal_id": 14, "bank_account_id": 9, "currency": "SAR"}


def args(**over):
    a = {"content_base64": b64(CSV_STD), "filename": "sept.csv", "profile": profile_std(), **TARGET,
         "opening_balance": 10000.0, "closing_balance": 12749.5}
    a.update(over)
    return a


class Base(unittest.TestCase):
    def setUp(self):
        self.rt = TempRuntime().__enter__()
        self.profiles = Path(tempfile.mkdtemp(prefix="oa_prof_"))
        self.fake = FakeOdoo(seed_statement_tables())
        self.emp = AccountingEmployee(self.rt.config, client=self.fake, profiles_dir=self.profiles)

    def tearDown(self):
        self.rt.__exit__()

    def cmd(self, name, **params):
        return self.emp.handle(CommandEnvelope(name, params, ChannelContext(channel="cli", actor_id="tester")))

    def preview(self, **over):
        return run_preview(self.fake, self.rt.config, args(**over), self.profiles)[0]


class PreviewTests(Base):
    def test_ready_with_explicit_profile_and_no_writes(self):
        r = self.preview()
        self.assertTrue(r["ready_to_propose"], r["blockers"])
        self.assertEqual(r["mutations"], 0); self.assertEqual(self.fake.mutations, [])
        self.assertEqual(r["consistency"]["status"], "ok")
        self.assertEqual(r["import_plan"]["to_import"], 3)
        self.assertEqual(r["import_plan"]["total_amount"], 2749.5)
        self.assertEqual(r["target"]["bank_account"], "****7519")
        self.assertEqual(r["file"]["format"], "csv"); self.assertEqual(len(r["file"]["sha256"]), 64)
        self.assertEqual(r["formats"]["supported"], ["csv", "xlsx"])
        self.assertIn("ofx", r["formats"]["planned_not_supported"])
        self.assertNotIn("SA0380000000608010167519", json.dumps(r, ensure_ascii=False))

    def test_not_ready_without_explicit_profile_but_detects(self):
        r = self.preview(profile=None)
        self.assertFalse(r["ready_to_propose"])
        self.assertTrue(r["detection"]["complete"])
        self.assertEqual(r["profile"]["source"], "auto_detected")
        self.assertTrue(any("profile صريح" in b for b in r["blockers"]))
        self.assertEqual(r["parsing"]["rows_parsed"], 3)

    def test_target_must_be_explicit_and_consistent(self):
        for k in TARGET:
            a = args(); a.pop(k)
            r = run_preview(self.fake, self.rt.config, a, self.profiles)[0]
            self.assertFalse(r["ready_to_propose"], k)
            self.assertTrue(any("ناقصة" in b for b in r["blockers"]), k)
        cases = {
            "wrong company": (dict(company_id=2), "تتبع الشركة"),
            "wrong bank account": (dict(bank_account_id=10), "لا يطابق حساب اليومية"),
            "sales journal": (dict(journal_id=15), "المطلوب bank"),
            "currency mismatch": (dict(currency="USD"), "لا تطابق"),
            "usd journal vs sar": (dict(journal_id=16), "لا تطابق"),
            "journal without bank account": (dict(journal_id=17), "بلا حساب بنكي"),
            "unknown journal": (dict(journal_id=999), "غير موجودة"),
            "unknown bank": (dict(bank_account_id=555), "غير موجود"),
        }
        for label, (over, frag) in cases.items():
            r = self.preview(**over)
            self.assertFalse(r["ready_to_propose"], label)
            self.assertTrue(any(frag in b for b in r["blockers"]), (label, r["blockers"]))

    def test_invalid_target_types(self):
        with self.assertRaises(StatementError):
            self.preview(journal_id="14")
        with self.assertRaises(StatementError):
            self.preview(currency="sar")

    def test_balance_mismatch_and_rejected_rows_block(self):
        self.assertFalse(self.preview(closing_balance=1.0)["ready_to_propose"])
        bad = CSV_STD + "99/99/2026,Bad,1.00,0\n"
        r = self.preview(content_base64=b64(bad))
        self.assertFalse(r["ready_to_propose"]); self.assertEqual(r["parsing"]["rows_rejected"], 1)
        self.assertTrue(any("مرفوض" in b for b in r["blockers"]))

    def test_decimals_beyond_currency_rejected(self):
        r = self.preview(content_base64=b64("Date,Description,Amount,Balance\n15/09/2026,A,1.234,0\n"), opening_balance=None, closing_balance=None)
        self.assertFalse(r["ready_to_propose"]); self.assertEqual(r["parsing"]["issues"][0]["code"], "too_many_decimals")

    def test_duplicates_exact_and_possible(self):
        self.fake.tables["account.bank.statement.line"] = [
            {"id": 1, "journal_id": [14, "J"], "date": "2026-09-15", "amount": 1000.0, "payment_ref": "transfer from acme", "unique_import_id": False, "is_reconciled": True},
            {"id": 2, "journal_id": [14, "J"], "date": "2026-09-16", "amount": -250.5, "payment_ref": "POS purchase", "unique_import_id": False, "is_reconciled": False},
        ]
        r = self.preview()
        self.assertEqual(r["duplicates"]["counts"], {"exact": 1, "possible": 1})
        self.assertEqual(r["duplicates"]["exact"][0]["existing_id"], 1)
        self.assertEqual(r["duplicates"]["possible"][0]["reason"], "same_date_amount_different_ref")
        self.assertEqual(r["import_plan"]["to_import"], 1)
        self.assertEqual(r["import_plan"]["excluded_possible"], 1)
        inc = self.preview(include_possible_duplicates=[2])
        self.assertEqual((inc["import_plan"]["to_import"], inc["import_plan"]["included_possible"]), (2, 1))
        bad = self.preview(include_possible_duplicates=[1])
        self.assertFalse(bad["ready_to_propose"])

    def test_all_duplicates_means_nothing_to_import(self):
        first = self.cmd("propose_statement_import", **args())
        aid = first.data["approval"]["approval_id"]
        code = ApprovalStore(self.rt.config).read_code_file(aid)
        self.cmd("approve_action", approval_id=aid, code=code, payload_hash=first.data["approval"]["payload_hash"])
        self.assertTrue(self.cmd("execute_approved_action", approval_id=aid, code=code).ok)
        r = self.preview(allow_reimport_file=True)
        self.assertEqual(r["duplicates"]["counts"]["exact"], 3)
        self.assertFalse(r["ready_to_propose"])
        self.assertTrue(any("لا توجد حركات جديدة" in b for b in r["blockers"]))

    def test_file_hash_registry(self):
        sha = self.preview()["file"]["sha256"]
        ImportRegistry(self.rt.dir).mark(sha, "imported")
        r = self.preview()
        self.assertFalse(r["ready_to_propose"]); self.assertTrue(any("SHA-256" in b for b in r["blockers"]))
        ImportRegistry(self.rt.dir).mark(sha, "proposed")
        r = self.preview()
        self.assertTrue(r["ready_to_propose"]); self.assertTrue(r["warnings"])

    def test_xlsx_and_rows_inputs(self):
        x = self.preview(content_base64=b64(build_xlsx(XLSX_ROWS, date_cols=(0,))), filename="s.xlsx",
                         profile=profile_std(format="xlsx", date_format=None), opening_balance=10000.0, closing_balance=10749.5)
        self.assertTrue(x["ready_to_propose"], x["blockers"]); self.assertEqual(x["file"]["format"], "xlsx")
        rows = [{"date": "2026-09-15", "amount": 1000, "payment_ref": "A"}, {"date": "2026-09-16", "amount": -250.5, "payment_ref": "B"}]
        r = run_preview(self.fake, self.rt.config, {"rows": rows, **TARGET}, self.profiles)[0]
        self.assertTrue(r["ready_to_propose"], r["blockers"]); self.assertEqual(r["file"]["format"], "normalized_rows")

    def test_planned_formats_are_refused_clearly(self):
        for fmt in ("ofx", "qfx", "camt053"):
            with self.assertRaises(StatementError) as cm:
                self.preview(format=fmt)
            self.assertIn("غير مدعومة بعد", str(cm.exception))

    def test_input_limits_and_errors_are_arabic(self):
        with self.assertRaises(StatementError) as cm:
            self.preview(content_base64="A" * (MAX_BASE64_CHARS + 1))
        self.assertIn("الحد المسموح", str(cm.exception))
        with self.assertRaises(StatementError) as cm:
            self.preview(content_base64="!!not base64!!")
        self.assertIn("base64", str(cm.exception))
        with self.assertRaises(StatementError):
            run_preview(self.fake, self.rt.config, {**TARGET}, self.profiles)
        with self.assertRaises(StatementError):
            run_preview(self.fake, self.rt.config, {"rows": [], "content_base64": "eA==", **TARGET}, self.profiles)
        with self.assertRaises(StatementError):
            self.preview(surprise=1)
        with self.assertRaises(StatementError) as cm:
            self.preview(content_base64=b64(""))
        self.assertIn("فارغ", str(cm.exception))

    def test_saved_profile_by_name(self):
        from odoo_accountant.statements.profiles import MappingProfile, save_profile
        save_profile(MappingProfile.from_dict(profile_std()), self.profiles)
        r = self.preview(profile="test-bank")
        self.assertTrue(r["ready_to_propose"], r["blockers"]); self.assertEqual(r["profile"]["name"], "test-bank")
        with self.assertRaises(StatementError):
            self.preview(profile="nope")


class ImportFlowTests(Base):
    def propose(self, **over):
        r = self.cmd("propose_statement_import", **args(**over))
        return r

    def approve_and_code(self, r):
        aid = r.data["approval"]["approval_id"]
        return aid, ApprovalStore(self.rt.config).read_code_file(aid)

    def test_propose_creates_nothing_and_needs_explicit_hash(self):
        r = self.propose()
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.data["approval"]["action"], "import_bank_statement_lines")
        self.assertEqual(r.data["approval"]["risk"], "FINANCIAL_FINAL")
        self.assertTrue(r.data["approval"]["requires_explicit_payload_hash"])
        self.assertEqual(self.fake.mutations, [])
        aid, code = self.approve_and_code(r)
        self.assertFalse(self.cmd("approve_action", approval_id=aid, code=code).ok)  # hash required
        self.assertIn("statement_preview", r.data)
        self.assertEqual(ImportRegistry(self.rt.dir).status(r.data["statement_preview"]["file"]["sha256"])["status"], "proposed")

    def test_full_import_only_creates_statement_lines(self):
        r = self.propose()
        aid, code = self.approve_and_code(r)
        self.assertEqual(self.cmd("execute_approved_action", approval_id=aid, code=code).data["status"], "blocked")  # not approved yet
        self.assertEqual(self.fake.mutations, [])
        self.assertTrue(self.cmd("approve_action", approval_id=aid, code=code, payload_hash=r.data["approval"]["payload_hash"]).ok)
        dry = self.cmd("execute_approved_action", approval_id=aid, code=code, dry_run=True)
        self.assertEqual(dry.data["status"], "dry_run"); self.assertEqual(self.fake.mutations, [])
        res = self.cmd("execute_approved_action", approval_id=aid, code=code)
        self.assertTrue(res.ok, res.message); self.assertEqual(res.data["status"], "executed")
        self.assertTrue(all(ok for _, ok in res.data["verification"]["checks"]), res.data["verification"])
        # the ONLY mutations: create on account.bank.statement.line (no post/reconcile/write/unlink/other models)
        self.assertEqual({(m, meth) for m, meth, _ in self.fake.mutations}, {("account.bank.statement.line", "create")})
        rows = self.fake.tables["account.bank.statement.line"]
        self.assertEqual(len(rows), 3); self.assertTrue(all(r["unique_import_id"].startswith("OA-") for r in rows))
        self.assertEqual(round(sum(r["amount"] for r in rows), 2), 2749.5)
        self.assertFalse(any(r["is_reconciled"] for r in rows))
        self.assertEqual(ImportRegistry(self.rt.dir).status(r.data["statement_preview"]["file"]["sha256"])["status"], "imported")
        # replay blocked, and re-proposing the same file finds nothing new
        self.assertFalse(self.cmd("execute_approved_action", approval_id=aid, code=code).ok)
        self.assertEqual(len(self.fake.tables["account.bank.statement.line"]), 3)
        self.assertFalse(self.propose().ok)
        events = [e["event"] for e in AuditLog(self.rt.dir).read_all()]
        for needed in ("statement_preview" if False else "propose", "approve", "execute_start", "execute_result"):
            self.assertIn(needed, events)
        self.assertTrue(AuditLog(self.rt.dir).verify_chain()[0])

    def test_import_approval_cannot_authorize_reconciliation(self):
        r = self.propose()
        aid, code = self.approve_and_code(r)
        self.cmd("approve_action", approval_id=aid, code=code, payload_hash=r.data["approval"]["payload_hash"])
        self.assertTrue(self.cmd("execute_approved_action", approval_id=aid, code=code).ok)
        # reconciliation is a different action that needs its own proposal/approval
        t = self.fake.tables
        t.setdefault("account.move", []).append({"id": 2, "name": "INV/2", "state": "posted", "move_type": "out_invoice", "partner_id": [7, "P"], "payment_state": "not_paid", "amount_residual": 1000.0, "currency_id": [1, "SAR"]})
        line_id = t["account.bank.statement.line"][0]["id"]
        rec = self.cmd("propose_action", action="reconcile_statement_line", params={"statement_line_id": line_id, "move_id": 2})
        self.assertFalse(rec.ok)  # no partner on imported line -> blocked, never auto-matched
        self.assertEqual(len([m for m in self.fake.mutations if m[1] != "create"]), 0)
        self.assertEqual(ApprovalStore(self.rt.config).get(aid)["status"], "consumed")

    def test_tampered_lines_are_rejected(self):
        r = self.propose()
        aid, code = self.approve_and_code(r)
        store = ApprovalStore(self.rt.config)
        path = store._path(aid)
        data = json.loads(path.read_text())
        data["params"]["lines"][0]["amount"] = 999999.0
        path.write_text(json.dumps(data))
        res = self.cmd("approve_action", approval_id=aid, code=code, payload_hash=r.data["approval"]["payload_hash"])
        self.assertFalse(res.ok); self.assertEqual(self.fake.mutations, [])

    def test_drift_after_proposal_blocks(self):
        r = self.propose()
        aid, code = self.approve_and_code(r)
        self.cmd("approve_action", approval_id=aid, code=code, payload_hash=r.data["approval"]["payload_hash"])
        self.fake.tables["account.bank.statement.line"].append({"id": 5, "journal_id": [14, "J"], "date": "2026-09-16", "amount": -250.5, "payment_ref": "Fuel station", "unique_import_id": False, "is_reconciled": False})
        res = self.cmd("execute_approved_action", approval_id=aid, code=code)
        self.assertFalse(res.ok); self.assertEqual(self.fake.mutations, [])

    def test_blocked_proposals_create_no_approval(self):
        for over in (dict(journal_id=15), dict(profile=None), dict(closing_balance=5.0)):
            r = self.propose(**over)
            self.assertFalse(r.ok, over)
            self.assertEqual(r.error, "blocked")
        self.assertEqual(ApprovalStore(self.rt.config).list_pending(), [])

    def test_audit_log_never_contains_file_content_or_account_number(self):
        self.propose()
        text = (self.rt.dir / "audit.jsonl").read_text()
        self.assertNotIn(b64(CSV_STD)[:40], text)
        self.assertNotIn("Transfer from ACME", text)
        self.assertNotIn("SA0380000000608010167519", text)
        self.assertIn("omitted", text)

    def test_handler_rejects_forged_direct_proposal(self):
        good = self.propose()
        aid, _ = self.approve_and_code(good)
        params = ApprovalStore(self.rt.config).get(aid)["params"]
        forged = {k: v for k, v in params.items() if k != "expected"}
        forged["lines"] = [dict(params["lines"][0], amount=5000.0)]   # fp no longer matches content
        r = self.cmd("propose_action", action="import_bank_statement_lines", params=forged)
        self.assertFalse(r.ok); self.assertIn("بصمة", r.message)
        for mutate in (lambda p: p.update(journal_id=-1), lambda p: p.update(currency="sar"), lambda p: p.update(lines=[]),
                       lambda p: p.update(extra=1), lambda p: p["lines"][0].update(amount=0)):
            p2 = json.loads(json.dumps({k: v for k, v in params.items() if k != "expected"}))
            mutate(p2)
            self.assertFalse(self.cmd("propose_action", action="import_bank_statement_lines", params=p2).ok)
        self.assertEqual(self.fake.mutations, [])


class McpStatementTests(Base):
    def test_tools_registered_and_strict(self):
        self.assertEqual(len(TOOLS), 13)
        self.assertIn("statement_import_preview", TOOLS); self.assertIn("propose_statement_import", TOOLS)
        for n in ("statement_import_preview", "propose_statement_import"):
            self.assertFalse(TOOLS[n][1]["additionalProperties"])
            self.assertIn("content_base64", TOOLS[n][1]["properties"]); self.assertIn("rows", TOOLS[n][1]["properties"])
        self.assertFalse(any(n.startswith(("execute_statement", "import_statement", "reconcile")) for n in TOOLS))

    def test_preview_and_propose_over_jsonrpc(self):
        srv = McpServer(self.emp)
        reqs = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "statement_import_preview", "arguments": args()}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "propose_statement_import", "arguments": args()}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "statement_import_preview", "arguments": {"rows": [{"date": "2026-09-15", "amount": 5, "payment_ref": "t"}], **TARGET}}},
        ]
        out = io.StringIO()
        srv.serve(io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"), out)
        msgs = [json.loads(l) for l in out.getvalue().splitlines()]
        prev = json.loads(msgs[0]["result"]["content"][0]["text"])
        self.assertTrue(prev["data"]["ready_to_propose"]); self.assertFalse(msgs[0]["result"]["isError"])
        prop = json.loads(msgs[1]["result"]["content"][0]["text"])
        self.assertEqual(prop["data"]["approval"]["status"], "pending")
        self.assertNotIn("approval_code", json.dumps(prop))
        self.assertTrue(json.loads(msgs[2]["result"]["content"][0]["text"])["data"]["ready_to_propose"])
        self.assertEqual(self.fake.mutations, [])


if __name__ == "__main__":
    unittest.main()
