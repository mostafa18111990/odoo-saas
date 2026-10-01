"""Regression for the observed incident: after a manual import, the two new lines showed NO description on the
reconciliation screen because the header "Payment Reference" is mapped by Odoo to `payment_reference`
(a hidden field of the journal entry) instead of `payment_ref` ("Label"), which is what the screen displays."""
import dataclasses
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from odoo_accountant.approvals import ApprovalStore
from odoo_accountant.models import ChannelContext, CommandEnvelope
from odoo_accountant.service import AccountingEmployee
from odoo_accountant.statements import readers
from odoo_accountant.statements.normalizer import normalize_statement_file, verify_normalized_output
from odoo_accountant.statements.visibility import check_description_visibility, resolve_header
from odoo_accountant.workflows import bank_match_suggest
from tests.fakes import FakeOdoo, TempRuntime, seed_statement_tables
from tests.statement_fixtures import b64, build_xlsx, excel_serial

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_bank_export.xls"
TARGET = {"company_id": 1, "journal_id": 14, "bank_account_id": 9, "currency": "SAR"}
needs_xlrd = unittest.skipUnless(readers.xls_available(), 'needs the pinned optional dependency: pip install "xlrd==2.0.1"')
ARABIC_MULTILINE = "حوالة محلية واردة\n      ACME SENDER CO المرسل\n      من مصرف تجريبي\n      مرجع العملية SYN000001\n"
FULL_TEXT = "حوالة محلية واردة ACME SENDER CO المرسل من مصرف تجريبي مرجع العملية SYN000001"


class Base(unittest.TestCase):
    def setUp(self):
        self.rt = TempRuntime().__enter__()
        self.tmp = Path(tempfile.mkdtemp(prefix="oa_lbl_"))
        self.cfg = dataclasses.replace(self.rt.config, output_dir=self.tmp / "out", input_dirs=(self.tmp / "out", self.tmp))
        self.fake = FakeOdoo(seed_statement_tables())
        self.emp = AccountingEmployee(self.cfg, client=self.fake)

    def tearDown(self):
        self.rt.__exit__()

    def cmd(self, name, **params):
        return self.emp.handle(CommandEnvelope(name, params, ChannelContext(channel="cli", actor_id="tester")))

    def csv_normalized(self, desc=ARABIC_MULTILINE) -> dict:
        text = 'Date,Description,Amount\n15/09/2026,"%s",7452.00\n16/09/2026,"%s",5589.00\n' % (desc, desc.replace("SYN000001", "SYN000002"))
        return normalize_statement_file(self.cfg, {"content_base64": b64(text), "filename": "stmt.csv"})

    def propose(self, path, **extra):
        r = self.cmd("propose_statement_import", source_path=path, **TARGET, **extra)
        self.assertTrue(r.ok, r.message)
        aid = r.data["approval"]["approval_id"]
        return r, aid, ApprovalStore(self.cfg).read_code_file(aid)

    def run_import(self, path):
        r, aid, code = self.propose(path)
        self.cmd("approve_action", approval_id=aid, code=code, payload_hash=r.data["approval"]["payload_hash"])
        return r, aid, self.cmd("execute_approved_action", approval_id=aid, code=code)


class OutputColumnTests(Base):
    def test_description_goes_in_the_label_column_not_payment_reference(self):
        rep = self.csv_normalized()
        self.assertTrue(rep["ok"], rep["blockers"])
        data = Path(rep["output"]["path"]).read_bytes()
        t = readers.read_xlsx(data, type("P", (), {"sheet": "Bank Transactions", "encoding": None, "delimiter": None})())
        self.assertEqual(t.rows[0], ["Date", "Label", "Amount"])
        self.assertNotIn("Payment Reference", t.rows[0])
        self.assertEqual(t.rows[1][1], FULL_TEXT)                         # full Arabic multi-line text, compressed to one line
        self.assertEqual(t.rows[2][1], FULL_TEXT.replace("SYN000001", "SYN000002"))
        self.assertEqual(rep["output"]["description_column"], "Label"); self.assertEqual(rep["output"]["description_field"], "payment_ref")
        self.assertEqual(rep["output"]["odoo_field_map"]["Label"], "payment_ref")
        props = verify_normalized_output(data)
        self.assertIn("Label=payment_ref", props["odoo_field_map"]); self.assertFalse(props["_legacy_header"])
        self.assertTrue(any("payment_ref" in w and "Payment Reference" in w for w in rep["warnings"]))

    @needs_xlrd
    def test_synthetic_xls_like_the_real_file(self):
        rep = normalize_statement_file(self.cfg, {"content_base64": b64(FIXTURE.read_bytes()), "filename": "accountTransactions_35.xls"})
        self.assertTrue(rep["ok"], rep["blockers"])
        t = readers.read_xlsx(Path(rep["output"]["path"]).read_bytes(), type("P", (), {"sheet": None, "encoding": None, "delimiter": None})())
        self.assertEqual(t.rows[0][:3], ["Date", "Label", "Amount"])
        for row, ref in zip(t.rows[1:], ("SYN000001", "SYN000002")):
            for needle in ("حوالة محلية واردة", "ACME SENDER CO", "من مصرف تجريبي", ref):
                self.assertIn(needle, row[1])
            self.assertNotIn("\n", row[1])


class ToolDescriptionTests(unittest.TestCase):
    """The agent plans from these descriptions: they must not advertise the header that caused the incident."""

    def test_mcp_descriptions_match_the_label_fix_and_new_inputs(self):
        from odoo_accountant.mcp_server import TOOLS
        norm = TOOLS["normalize_statement_file"][0]
        self.assertIn("Date وLabel وAmount", norm); self.assertNotIn("Date وPayment Reference", norm)
        self.assertIn("payment_ref", norm)
        prev = TOOLS["statement_import_preview"][0]
        self.assertIn("source_path", prev); self.assertIn("XLS", prev); self.assertIn("description_visibility", prev)
        props = TOOLS["statement_import_preview"][1]["properties"]
        self.assertIn("XLS", props["content_base64"]["description"])
        self.assertIn("description_visibility", TOOLS["propose_statement_import"][0])


class HeaderResolutionTests(Base):
    def test_replica_of_odoo_header_matching(self):
        fields = seed_statement_tables()["_fields"]["account.bank.statement.line"]
        self.assertEqual(resolve_header("Label", fields), "payment_ref")
        self.assertEqual(resolve_header("payment_ref", fields), "payment_ref")
        self.assertEqual(resolve_header("Payment Reference", fields), "payment_reference")     # <- the bug: hidden field
        self.assertEqual(resolve_header("Date", fields), "date")
        self.assertEqual(resolve_header("Amount", fields), "amount")
        self.assertIsNone(resolve_header("Transaction Details", fields))                      # read-only json: not importable
        self.assertIsNone(resolve_header("Description", fields))

    def test_visibility_evidence_ok(self):
        v = check_description_visibility(self.fake, [{"payment_ref": "x"}], ["Date", "Label", "Amount"])
        self.assertEqual(v["status"], "ok"); self.assertEqual(v["display_field"], "payment_ref")
        self.assertEqual(len(v["evidence"]["views_showing_payment_ref"]), 2)
        self.assertEqual(v["header_resolution"]["Label"], "payment_ref")

    def test_legacy_header_is_flagged(self):
        v = check_description_visibility(self.fake, None, ["Payment Reference"])
        self.assertEqual(v["header_resolution"]["Payment Reference"], "payment_reference")
        self.assertTrue(any("payment_reference" in w for w in v["warnings"]))

    def test_blocked_when_screen_does_not_display_payment_ref(self):
        for v in self.fake.tables["ir.ui.view"]:
            v["arch_db"] = v["arch_db"].replace("payment_ref", "payment_reference")
        out = check_description_visibility(self.fake, [{"payment_ref": "x"}])
        self.assertEqual(out["status"], "blocked"); self.assertTrue(any("لن يظهر" in p for p in out["problems"]))

    def test_fail_closed_when_evidence_unreadable(self):
        self.fake.fail_views = True
        out = check_description_visibility(self.fake, [{"payment_ref": "x"}])
        self.assertEqual(out["status"], "unverified")
        self.fake.fail_views = False
        self.fake.tables["ir.ui.view"] = []
        self.assertEqual(check_description_visibility(self.fake, [{"payment_ref": "x"}])["status"], "unverified")


class PreviewProposalPayloadTests(Base):
    def test_preview_shows_mandatory_visibility_and_blocks_without_it(self):
        out = self.csv_normalized()["output"]["path"]
        pv = self.cmd("statement_import_preview", source_path=out, **TARGET).data
        self.assertTrue(pv["ready_to_propose"], pv["blockers"])
        self.assertEqual(pv["description_visibility"]["status"], "ok")
        self.assertEqual(pv["description_visibility"]["lines_without_description"], 0)
        self.assertEqual(pv["profile"]["mapping"]["columns"]["payment_ref"], "Label")
        self.fake.tables["ir.ui.view"] = []                                   # evidence missing -> no approval request possible
        blocked = self.cmd("statement_import_preview", source_path=out, **TARGET).data
        self.assertFalse(blocked["ready_to_propose"])
        self.assertTrue(any("شاشات التسوية" in b or "التحقق" in b for b in blocked["blockers"]))
        r = self.cmd("propose_statement_import", source_path=out, **TARGET)
        self.assertFalse(r.ok); self.assertEqual(ApprovalStore(self.cfg).list_pending(), [])

    def test_full_description_survives_into_payload_and_is_written_to_payment_ref(self):
        out = self.csv_normalized()["output"]["path"]
        r, aid, res = self.run_import(out)
        params = ApprovalStore(self.cfg).get(aid)["params"]
        self.assertEqual([l["payment_ref"] for l in params["lines"]], [FULL_TEXT, FULL_TEXT.replace("SYN000001", "SYN000002")])
        self.assertTrue(res.ok, res.message)
        created = [m for m in self.fake.mutations if m[1] == "create"][0][2]["vals_list"]
        for v, line in zip(created, params["lines"]):
            self.assertEqual(v["payment_ref"], line["payment_ref"])
            self.assertNotIn("payment_reference", v); self.assertNotIn("name", v)
        rows = self.fake.tables["account.bank.statement.line"]
        self.assertEqual([r_["payment_ref"] for r_ in rows], [l["payment_ref"] for l in params["lines"]])
        labels = [c for c in res.data["verification"]["checks"] if "payment_ref" in c[0]]
        self.assertTrue(labels and all(ok for _, ok in labels))

    def test_legacy_header_file_still_imports_correctly_via_payment_ref_with_a_warning(self):
        rows = [["Date", "Payment Reference", "Amount"], [excel_serial(2026, 9, 29), FULL_TEXT, 7452.0]]
        p = self.tmp / "legacy.xlsx"; p.write_bytes(build_xlsx(rows, date_cols=(0,), sheets={"Bank Transactions": rows}))
        pv = self.cmd("statement_import_preview", source_path=str(p), **TARGET).data
        self.assertTrue(pv["ready_to_propose"], pv["blockers"])
        self.assertTrue(any("payment_reference" in w for w in pv["warnings"]))
        self.assertEqual(pv["profile"]["mapping"]["columns"]["payment_ref"], "Payment Reference")
        r, aid, res = self.run_import(str(p))
        self.assertEqual(self.fake.tables["account.bank.statement.line"][0]["payment_ref"], FULL_TEXT)

    def test_execution_flags_a_blank_label_after_import(self):
        """Regression for the incident: if Odoo stores the lines without a label, the result must NOT be reported as verified."""
        self.fake.drop_label = True
        r, aid, res = self.run_import(self.csv_normalized()["output"]["path"])
        self.assertFalse(res.ok)
        self.assertEqual(res.data["status"], "executed_unverified")
        failed = [c for c in res.data["verification"]["checks"] if not c[1]]
        self.assertTrue(any("payment_ref" in c[0] for c in failed), failed)


class RepairPlanTests(Base):
    def blank_lines(self):
        self.fake.tables["account.bank.statement.line"] = [
            {"id": 101, "journal_id": [14, "Bank A"], "date": "2026-09-29", "amount": 7452.0, "payment_ref": False, "payment_reference": FULL_TEXT, "is_reconciled": False, "partner_id": False, "currency_id": False},
            {"id": 102, "journal_id": [14, "Bank A"], "date": "2026-09-30", "amount": 5589.0, "payment_ref": False, "payment_reference": FULL_TEXT.replace("SYN000001", "SYN000002"), "is_reconciled": False, "partner_id": False, "currency_id": False},
            {"id": 103, "journal_id": [14, "Bank A"], "date": "2026-09-28", "amount": 11385.0, "payment_ref": "existing label", "payment_reference": False, "is_reconciled": False, "partner_id": False, "currency_id": False},
        ]

    def test_detection_builds_a_ready_but_unexecuted_repair_proposal(self):
        self.blank_lines()
        rep = bank_match_suggest(self.fake, journal_id=14, date_from="2026-09-01", date_to="2026-09-30")
        blank = rep["lines_without_label"]
        self.assertEqual((blank["count"], blank["repairable"]), (2, 2))
        self.assertEqual({c["statement_line_id"] for c in blank["candidates"]}, {101, 102})
        self.assertEqual(blank["proposal"]["action"], "fill_statement_line_label")
        self.assertEqual(self.fake.mutations, [])
        r = self.cmd("propose_action", **blank["proposal"])
        self.assertTrue(r.ok, r.message)
        self.assertEqual((r.data["approval"]["risk"], r.data["approval"]["status"]), ("FINANCIAL_FINAL", "pending"))
        self.assertEqual(self.fake.mutations, [])                 # nothing written until explicit approval

    def test_approved_repair_fills_only_blank_labels(self):
        self.blank_lines()
        prop = bank_match_suggest(self.fake, journal_id=14, date_from="2026-09-01", date_to="2026-09-30")["lines_without_label"]["proposal"]
        r = self.cmd("propose_action", **prop)
        aid = r.data["approval"]["approval_id"]; code = ApprovalStore(self.cfg).read_code_file(aid)
        self.assertEqual(self.cmd("execute_approved_action", approval_id=aid, code=code).data["status"], "blocked")
        self.cmd("approve_action", approval_id=aid, code=code, payload_hash=r.data["approval"]["payload_hash"])
        res = self.cmd("execute_approved_action", approval_id=aid, code=code)
        self.assertTrue(res.ok, res.message); self.assertEqual(res.data["status"], "executed")
        rows = {x["id"]: x for x in self.fake.tables["account.bank.statement.line"]}
        self.assertEqual(rows[101]["payment_ref"], FULL_TEXT)
        self.assertEqual(rows[103]["payment_ref"], "existing label")                        # untouched
        self.assertEqual({(m, meth) for m, meth, _ in self.fake.mutations}, {("account.bank.statement.line", "write")})
        self.assertEqual((rows[101]["amount"], rows[101]["date"], rows[101]["is_reconciled"]), (7452.0, "2026-09-29", False))

    def test_repair_refuses_overwrite_mismatch_and_reconciled(self):
        self.blank_lines()
        ok_text = FULL_TEXT
        cases = {
            "overwrite": {"statement_line_id": 103, "payment_ref": "new label"},
            "mismatched text": {"statement_line_id": 101, "payment_ref": "some other invented text"},
            "missing line": {"statement_line_id": 999, "payment_ref": ok_text},
        }
        for label, line in cases.items():
            r = self.cmd("propose_action", action="fill_statement_line_label", params={"lines": [line]})
            self.assertFalse(r.ok, label)
        self.fake.tables["account.bank.statement.line"][0]["is_reconciled"] = True
        self.assertFalse(self.cmd("propose_action", action="fill_statement_line_label", params={"lines": [{"statement_line_id": 101, "payment_ref": ok_text}]}).ok)
        for bad in ({"lines": []}, {"lines": [{"statement_line_id": 1, "payment_ref": "x"}] * 2}, {"lines": [{"statement_line_id": 1}]}, {"lines": [{"statement_line_id": 1, "payment_ref": "x", "amount": 5}]}, {"lines": [{"statement_line_id": 1, "payment_ref": "x"}], "x": 1}):
            self.assertFalse(self.cmd("propose_action", action="fill_statement_line_label", params=bad).ok)
        self.assertEqual(self.fake.mutations, [])


if __name__ == "__main__":
    unittest.main()
