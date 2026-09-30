import base64
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from odoo_accountant import cli
from odoo_accountant.errors import StatementError
from odoo_accountant.mcp_server import TOOLS, McpServer
from odoo_accountant.models import ChannelContext, CommandEnvelope
from odoo_accountant.service import AccountingEmployee
from odoo_accountant.statements import normalizer as N
from odoo_accountant.statements import readers
from odoo_accountant.statements.dedupe import ImportRegistry
from odoo_accountant.statements.limits import MAX_BASE64_CHARS
from odoo_accountant.statements.normalizer import normalize_statement_file
from tests.fakes import FakeOdoo, TempRuntime, seed_statement_tables
from tests.statement_fixtures import b64, build_xlsx, excel_serial, profile_std

needs_xlrd = unittest.skipUnless(readers.xls_available(), 'legacy .xls tests need the pinned optional dependency: pip install "xlrd==2.0.1"')
FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_bank_export.xls"
REAL_ATTACHMENT = Path(os.environ.get("ODOO_ACCOUNTANT_TEST_XLS", "/root/.claude/uploads/dcc0629b-b026-5cd1-a399-62b5d832c40c/b00911f5-accountTransactions_35.xls"))
TARGET = {"company_id": 1, "journal_id": 14, "bank_account_id": 9, "currency": "SAR"}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oa_norm_"))
        self.inp, self.out = self.tmp / "in", self.tmp / "out"
        self.inp.mkdir()
        self.rt = TempRuntime().__enter__()
        self.cfg = dataclasses.replace(self.rt.config, output_dir=self.out, input_dirs=(self.inp, self.out))

    def tearDown(self):
        self.rt.__exit__()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, name: str, data) -> Path:
        p = self.inp / name
        p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return p

    def norm(self, **args):
        return normalize_statement_file(self.cfg, args)

    def csv(self, text, enc="utf-8", **args):
        return self.norm(content_base64=b64(text.encode(enc)), filename="stmt.csv", **args)

    def read_out(self, rep):
        data = Path(rep["output"]["path"]).read_bytes()
        t = readers.read_xlsx(data, type("P", (), {"sheet": "Bank Transactions", "encoding": None, "delimiter": None})())
        return data, t


@needs_xlrd
class XlsTests(Sandbox):
    def test_synthetic_xls_end_to_end(self):
        src = self.put("bank export.xls", FIXTURE.read_bytes())
        before = (sha(src.read_bytes()), src.stat().st_mtime_ns)
        rep = self.norm(source_path=str(src))
        self.assertTrue(rep["ok"], rep["blockers"])
        self.assertEqual(rep["counts"], {"rows_total": 2, "rows_parsed": 2, "rows_rejected": 0, "rows_accepted": 2})
        self.assertEqual((rep["totals"]["sum"], rep["totals"]["credits"], rep["totals"]["debits"]), (13041.0, 13041.0, 0))
        self.assertEqual((rep["totals"]["min_date"], rep["totals"]["max_date"]), ("2026-09-29", "2026-09-30"))
        self.assertEqual(rep["currency"], {"value": "SAR", "detected_in_file": "SAR", "provided": None})
        self.assertEqual(rep["source_account_hint"], "****0001")
        chain = rep["balance"]["chain"]
        self.assertEqual((chain["status"], chain["order"], chain["derived_opening"], chain["last_balance"]), ("ok", "ascending", 182009.1, 195050.1))
        self.assertEqual(rep["profile"]["source"], "auto_detected")
        self.assertEqual(rep["detection"]["stacked_header_rows"], 1)
        # output file
        data, t = self.read_out(rep)
        self.assertEqual(t.meta["sheet"], "Bank Transactions")
        self.assertEqual(t.rows[0], ["Date", "Payment Reference", "Amount"])
        self.assertEqual([r[0].isoformat() for r in t.rows[1:]], ["2026-09-29", "2026-09-30"])
        self.assertEqual([r[2] for r in t.rows[1:]], [7452, 5589])
        for r in t.rows[1:]:
            self.assertNotIn("\n", r[1]); self.assertNotIn("  ", r[1])
            for needle in ("حوالة محلية واردة", "ACME SENDER CO", "مبلغ التحويل", "مرجع العملية"):
                self.assertIn(needle, r[1])
        self.assertIn("SYN000001", t.rows[1][1]); self.assertIn("SYN000002", t.rows[2][1])
        self.assertTrue(rep["output"]["roundtrip_verified"])
        self.assertEqual(rep["output"]["sha256"], sha(data))
        self.assertEqual(rep["source"]["sha256"], before[0])
        # safe, deterministic name inside the output dir
        self.assertRegex(rep["output"]["filename"], r"^normalized_bank-export_2026-09-29_2026-09-30_[0-9a-f]{8}\.xlsx$")
        self.assertEqual(Path(rep["output"]["path"]).parent, self.out)
        again = self.norm(source_path=str(src))
        self.assertEqual(again["output"]["sha256"], rep["output"]["sha256"])
        # source untouched
        self.assertTrue(rep["source"]["unchanged_after_run"]); self.assertFalse(rep["source"]["modified"])
        self.assertEqual((sha(src.read_bytes()), src.stat().st_mtime_ns), before)
        # provenance travels with the file
        props = N.verify_normalized_output(data)
        self.assertEqual(props["source_sha256"], before[0]); self.assertEqual(props["rows"], "2"); self.assertEqual(props["sum"], "13041.00")
        self.assertEqual(rep["mutations"], 0); self.assertFalse(rep["odoo_touched"])

    def test_same_output_from_base64_and_path(self):
        src = self.put("a.xls", FIXTURE.read_bytes())
        p = self.norm(source_path=str(src))
        c = self.norm(content_base64=b64(FIXTURE.read_bytes()), filename="other name.xls")
        self.assertEqual(p["output"]["sha256"], c["output"]["sha256"])
        self.assertEqual(p["source"]["sha256"], c["source"]["sha256"])

    def test_currency_column_only_on_request(self):
        rep = self.norm(content_base64=b64(FIXTURE.read_bytes()), filename="a.xls", include_currency=True)
        _, t = self.read_out(rep)
        self.assertEqual(t.rows[0], ["Date", "Payment Reference", "Amount", "Currency"]); self.assertEqual(t.rows[1][3], "SAR")
        plain = self.norm(content_base64=b64(FIXTURE.read_bytes()), filename="a.xls")
        self.assertEqual(self.read_out(plain)[1].rows[0], ["Date", "Payment Reference", "Amount"])
        bad = self.csv("Date,Description,Amount\n15/09/2026,A,10.00\n", include_currency=True)
        self.assertFalse(bad["ok"]); self.assertTrue(any("currency" in b for b in bad["blockers"]))
        self.assertTrue(self.csv("Date,Description,Amount\n15/09/2026,A,10.00\n", include_currency=True, currency="SAR")["ok"])

    def test_currency_conflict_with_file(self):
        rep = self.norm(content_base64=b64(FIXTURE.read_bytes()), filename="a.xls", currency="USD")
        self.assertFalse(rep["ok"]); self.assertTrue(any("تخالف" in b for b in rep["blockers"]))

    @unittest.skipUnless(REAL_ATTACHMENT.exists(), "real attachment not present (non-live realistic check)")
    def test_real_attachment_non_live(self):
        src_hash = sha(REAL_ATTACHMENT.read_bytes())
        cfg = dataclasses.replace(self.cfg, input_dirs=(REAL_ATTACHMENT.parent,))
        rep = normalize_statement_file(cfg, {"source_path": str(REAL_ATTACHMENT)})
        self.assertTrue(rep["ok"], rep["blockers"])
        self.assertEqual(rep["counts"]["rows_accepted"], 2)
        self.assertEqual(rep["totals"]["sum"], 13041.0)
        self.assertEqual((rep["totals"]["min_date"], rep["totals"]["max_date"]), ("2026-09-29", "2026-09-30"))
        self.assertEqual(rep["balance"]["chain"]["last_balance"], 195050.1)
        self.assertEqual(sha(REAL_ATTACHMENT.read_bytes()), src_hash)


class OtherFormatTests(Sandbox):
    def test_csv_english_debit_credit(self):
        rep = self.csv("Date,Description,Debit,Credit,Balance\n15/09/2026,Pay,100.00,,900.00\n16/09/2026,Receive,,250.00,1150.00\n")
        self.assertTrue(rep["ok"], rep["blockers"])
        _, t = self.read_out(rep)
        self.assertEqual([r[2] for r in t.rows[1:]], [-100, 250]); self.assertEqual(rep["totals"]["sum"], 150.0)
        self.assertEqual(rep["balance"]["chain"]["derived_opening"], 1000.0)

    def test_csv_arabic_headers_cp1256_single_amount(self):
        rep = self.csv("التاريخ,البيان,المبلغ,الرصيد\n15/09/2026,دفع فاتورة,-100.00,900.00\n16/09/2026,إيداع,\"1,250.00\",\"2,150.00\"\n", enc="cp1256")
        self.assertTrue(rep["ok"], rep["blockers"])
        self.assertEqual(rep["source"]["encoding"], "cp1256")
        _, t = self.read_out(rep)
        self.assertEqual([r[2] for r in t.rows[1:]], [-100, 1250]); self.assertEqual(t.rows[1][1], "دفع فاتورة")

    def test_arabic_digits_and_negative_styles(self):
        rep = self.csv("Date,Description,Amount\n15/09/2026,A,(100.00)\n16/09/2026,B,50.00-\n17/09/2026,C,\"١٬٢٠٠٫٥٠\"\n")
        self.assertTrue(rep["ok"], rep["blockers"])
        self.assertEqual([r[2] for r in self.read_out(rep)[1].rows[1:]], [-100, -50, 1200.5])

    def test_xlsx_arabic_headers_real_date_cells(self):
        rows = [["التاريخ", "البيان", "المبلغ"], [excel_serial(2026, 9, 15), "تحويل وارد", 1000.0], [excel_serial(2026, 9, 16), "سداد", -250.5]]
        rep = self.norm(content_base64=b64(build_xlsx(rows, date_cols=(0,))), filename="s.xlsx")
        self.assertTrue(rep["ok"], rep["blockers"])
        self.assertEqual(rep["totals"]["sum"], 749.5); self.assertEqual(rep["source"]["format"], "xlsx")
        self.assertEqual([r[0].isoformat() for r in self.read_out(rep)[1].rows[1:]], ["2026-09-15", "2026-09-16"])

    def test_explicit_profile_for_unrecognised_headers(self):
        text = "Datum;Text;Betrag\n15.09.2026;Gehalt;1.234,56\n16.09.2026;Miete;-800,00\n"
        amb = self.csv(text)
        self.assertFalse(amb["ok"]); self.assertTrue(amb["needs_clarification"])
        prof = profile_std(columns={"date": "Datum", "amount": "Betrag", "payment_ref": "Text"}, date_format="%d.%m.%Y", decimal_separator=",", thousands_separator=".")
        rep = self.csv(text, profile=prof)
        self.assertTrue(rep["ok"], rep["blockers"]); self.assertEqual(rep["profile"]["source"], "explicit")
        self.assertEqual(self.read_out(rep)[1].rows[1][2], 1234.56)

    def test_formulas_are_never_executed_and_output_is_inert(self):
        base = build_xlsx([["Date", "Description", "Amount"], [excel_serial(2026, 9, 15), '=HYPERLINK("http://evil.example","x")', 7452.0]], date_cols=(0,))
        zin = zipfile.ZipFile(io.BytesIO(base)); buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zout:
            for n in zin.namelist():
                d = zin.read(n)
                if n == "xl/worksheets/sheet1.xml":
                    d = d.replace(b'<c r="C2"><v>7452.0</v></c>', b'<c r="C2"><f>1+1</f><v>7452</v></c>')
                zout.writestr(n, d)
        rep = self.norm(content_base64=b64(buf.getvalue()), filename="f.xlsx")
        self.assertTrue(rep["ok"], rep["blockers"])
        self.assertTrue(any("معادلة" in w for w in rep["warnings"]))
        self.assertEqual(self.read_out(rep)[1].rows[1][2], 7452)       # cached value, not evaluated
        out = Path(rep["output"]["path"]).read_bytes()
        sheet = zipfile.ZipFile(io.BytesIO(out)).read("xl/worksheets/sheet1.xml")
        self.assertNotIn(b"<f>", sheet); self.assertNotIn(b"<f ", sheet)
        self.assertEqual(self.read_out(rep)[1].rows[1][1], '=HYPERLINK("http://evil.example","x")')   # kept as text
        self.assertIn(b't="s"', sheet)

    def test_full_long_reference_preserved(self):
        long_ref = "REF " + "x" * 1500
        rep = self.csv(f"Date,Description,Amount\n15/09/2026,{long_ref},10.00\n")
        self.assertTrue(rep["ok"]); self.assertEqual(self.read_out(rep)[1].rows[1][1], long_ref)


class AmbiguityAndRejectionTests(Sandbox):
    def assertStopped(self, rep, frag=None, check_dir=True):
        self.assertFalse(rep["ok"]); self.assertIsNone(rep["output"])
        if check_dir:
            self.assertEqual(list(self.out.glob("*.xlsx")) if self.out.exists() else [], [])
        if frag:
            self.assertTrue(any(frag in b for b in rep["blockers"]), rep["blockers"])

    def test_ambiguous_date_format_stops(self):
        rep = self.csv("Date,Description,Amount\n01/02/2026,A,10.00\n03/04/2026,B,5.00\n")
        self.assertStopped(rep, "لا تخمين"); self.assertTrue(rep["needs_clarification"])
        self.assertTrue(any("date_format" in a for a in rep["detection"]["ambiguities"]))

    def test_two_date_columns_and_ambiguous_amounts_stop(self):
        self.assertStopped(self.csv("Date,Posting Date,Description,Amount\n15/09/2026,16/09/2026,A,10.00\n"))
        self.assertStopped(self.csv("Date,Description,Amount,Debit,Credit\n15/09/2026,A,10.00,1,2\n"))

    def test_no_header_and_ambiguous_decimal_stop(self):
        self.assertStopped(self.csv("foo,bar\n1,2\n"))
        self.assertStopped(self.csv('Date,Description,Amount\n15/09/2026,A,"1,234"\n'))

    def test_multiple_sheets_require_choice(self):
        data = build_xlsx(None, sheets={"Summary": [["x"]], "Txns": [["Date", "Description", "Amount"], [excel_serial(2026, 9, 15), "A", 5.0]]}, date_cols=(0,))
        with self.assertRaises(StatementError) as cm:
            self.norm(content_base64=b64(data), filename="m.xlsx")
        self.assertEqual(cm.exception.code, "xlsx_sheet_ambiguous")
        rep = self.norm(content_base64=b64(data), filename="m.xlsx", sheet="Txns")
        self.assertTrue(rep["ok"], rep["blockers"])

    def test_rejected_rows_produce_no_file_and_name_the_rows(self):
        prof = profile_std(columns={"date": "Date", "amount": "Amount", "payment_ref": "Description"})
        rep = self.csv("Date,Description,Amount\n15/09/2026,ok,10.00\n99/99/2026,bad date,1.00\n16/09/2026,,2.00\n17/09/2026,zero,0.00\n18/09/2026,junk,abc\n", profile=prof)
        self.assertStopped(rep, "مرفوض")
        self.assertEqual(rep["counts"]["rows_rejected"], 4)
        self.assertEqual({i["code"] for i in rep["issues"]}, {"bad_date", "empty_ref", "zero_amount", "bad_amount"})
        self.assertTrue(all(i["row"] for i in rep["issues"]))

    def test_both_debit_and_credit_in_a_row_is_rejected(self):
        self.assertStopped(self.csv("Date,Description,Debit,Credit\n15/09/2026,A,5.00,5.00\n"), "مرفوض")

    def test_balance_chain_broken_reports_difference(self):
        rep = self.csv("Date,Description,Amount,Balance\n15/09/2026,a,100.00,1100.00\n16/09/2026,b,50.00,1150.00\n17/09/2026,c,-40.00,1000.00\n")
        self.assertStopped(rep, "تسلسل الرصيد")
        b = rep["balance"]["chain"]["breaks"][0]
        self.assertEqual((b["expected_balance"], b["actual_balance"], b["difference"]), (1110.0, 1000.0, -110.0))
        self.assertIn("-110", " ".join(rep["blockers"]))

    def test_descending_statement_is_accepted(self):
        rep = self.csv("Date,Description,Amount,Balance\n16/09/2026,in,250.00,1150.00\n15/09/2026,out,-100.00,900.00\n")
        self.assertTrue(rep["ok"], rep["blockers"]); self.assertEqual(rep["balance"]["chain"]["order"], "descending")
        self.assertEqual(rep["balance"]["chain"]["derived_opening"], 1000.0)

    def test_opening_and_closing_checks(self):
        ok = "Date,Description,Amount,Balance\n15/09/2026,a,100.00,1100.00\n16/09/2026,b,-40.00,1060.00\n"
        self.assertTrue(self.csv(ok, opening_balance=1000.0, closing_balance=1060.0)["ok"])
        bad_close = self.csv(ok, closing_balance=999.0)
        self.assertStopped(bad_close, check_dir=False); self.assertEqual(bad_close["balance"]["chain"]["closing_difference"], 61.0)
        bad_open = self.csv(ok, opening_balance=500.0)
        self.assertStopped(bad_open, check_dir=False); self.assertEqual(bad_open["balance"]["chain"]["opening_difference"], 500.0)
        nobal = self.csv("Date,Description,Amount\n15/09/2026,a,100.00\n16/09/2026,b,-40.00\n", opening_balance=1000.0, closing_balance=1000.0)
        self.assertStopped(nobal, "الرصيد الافتتاحي", check_dir=False)

    def test_statement_currency_column_mismatch(self):
        self.assertStopped(self.csv("Date,Description,Amount,Currency\n15/09/2026,a,10.00,USD\n", currency="SAR"), "مرفوض")


class SafetyTests(Sandbox):
    def test_unsupported_and_planned_formats(self):
        for fmt in ("ofx", "qfx", "camt053"):
            with self.assertRaises(StatementError) as cm:
                self.norm(content_base64=b64("x"), format=fmt)
            self.assertIn("غير مدعومة بعد", str(cm.exception))
        with self.assertRaises(StatementError):
            self.norm(content_base64=b64("x"), format="pdf")
        with self.assertRaises(StatementError) as cm:
            self.norm(content_base64=b64("x"), format="xlsm")
        self.assertEqual(cm.exception.code, "macros")
        with self.assertRaises(StatementError) as cm:
            self.norm(content_base64=b64("x"), filename="a.xlsm")
        self.assertEqual(cm.exception.code, "macros")

    @needs_xlrd
    def test_encrypted_and_macro_files_refused(self):
        enc = readers.OLE_MAGIC + b"\x00" * 600 + "EncryptedPackage".encode("utf-16le") + b"\x00" * 50
        for fmt in (None, "xlsx", "xls"):
            with self.assertRaises(StatementError) as cm:
                self.norm(content_base64=b64(enc), filename="e.xlsx", format=fmt)
            self.assertEqual(cm.exception.code, "encrypted")
        vba = readers.OLE_MAGIC + b"\x00" * 600 + "_VBA_PROJECT".encode("utf-16le") + b"\x00" * 50
        with self.assertRaises(StatementError) as cm:
            self.norm(content_base64=b64(vba), filename="m.xls")
        self.assertEqual(cm.exception.code, "macros")
        good = build_xlsx([["Date", "Description", "Amount"], [excel_serial(2026, 9, 15), "A", 5.0]], date_cols=(0,))
        zin = zipfile.ZipFile(io.BytesIO(good)); buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in zin.namelist():
                z.writestr(n, zin.read(n))
            z.writestr("xl/vbaProject.bin", b"\x00macro")
        with self.assertRaises(StatementError) as cm:
            self.norm(content_base64=b64(buf.getvalue()), filename="m.xlsx")
        self.assertEqual(cm.exception.code, "macros")

    @needs_xlrd
    def test_corrupt_files(self):
        raw = FIXTURE.read_bytes()
        cases = [("truncated.xls", raw[:2000], "xls_corrupt"), ("garbage.xls", readers.OLE_MAGIC + b"\x00" * 600, "xls_corrupt"),
                 ("text.xls", b"Date,Description,Amount\n1,2,3\n", "xls_corrupt"), ("bad.xlsx", b"PK\x03\x04 not a zip", "xlsx_corrupt")]
        for name, data, code in cases:
            with self.assertRaises(StatementError, msg=name) as cm:
                self.norm(content_base64=b64(data), filename=name)
            self.assertEqual(cm.exception.code, code, name)
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w") as z:
            z.writestr("xl/workbook.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><workbook/>')
        with self.assertRaises(StatementError) as cm:
            self.norm(content_base64=b64(bomb.getvalue()), filename="x.xlsx")
        self.assertEqual(cm.exception.code, "xlsx_unsafe")
        with self.assertRaises(StatementError):
            self.norm(content_base64=b64(b"\xff\xfe\x00\x01" * 10), filename="x.csv", format="csv")  # no delimiter / undecodable structure

    def test_limits(self):
        with self.assertRaises(StatementError) as cm:
            self.norm(content_base64="A" * (MAX_BASE64_CHARS + 1))
        self.assertIn("الحد المسموح", str(cm.exception))
        with self.assertRaises(StatementError):
            self.norm(content_base64="!!bad!!")
        with self.assertRaises(StatementError):
            self.norm(content_base64=b64(""))
        rows = "Date,Description,Amount\n" + "".join(f"15/09/2026,row {i},1.00\n" for i in range(5201))
        with self.assertRaises(StatementError) as cm:
            self.csv(rows)
        self.assertEqual(cm.exception.code, "too_many_rows")
        with self.assertRaises(StatementError) as cm:
            self.put("big.csv", b"x" * (5 * 1024 * 1024 + 1)) and self.norm(source_path=str(self.inp / "big.csv"))
        self.assertEqual(cm.exception.code, "file_too_big")

    def test_argument_validation(self):
        for bad in ({}, {"source_path": "x", "content_base64": "eA=="}, {"content_base64": "eA==", "surprise": 1},
                    {"content_base64": "eA==", "currency": "sar"}, {"content_base64": "eA==", "decimal_places": 9}, {"content_base64": "eA==", "opening_balance": "1"}):
            with self.assertRaises(StatementError, msg=str(bad)):
                self.norm(**bad)

    def test_source_path_is_confined(self):
        outside = self.tmp / "outside.csv"; outside.write_text("Date,Description,Amount\n15/09/2026,a,1.00\n")
        with self.assertRaises(StatementError) as cm:
            self.norm(source_path=str(outside))
        self.assertEqual(cm.exception.code, "path_outside")
        link = self.inp / "link.csv"; link.symlink_to(outside)
        with self.assertRaises(StatementError) as cm:
            self.norm(source_path=str(link))
        self.assertEqual(cm.exception.code, "path_outside")
        for path, code in ((str(self.inp / "missing.csv"), "path_missing"), (str(self.inp), "path_not_file"), ("", "path_invalid")):
            with self.assertRaises(StatementError) as cm:
                self.norm(source_path=path)
            self.assertEqual(cm.exception.code, code)
        env = self.put(".env", "SECRET=1")
        with self.assertRaises(StatementError) as cm:
            self.norm(source_path=str(env))
        self.assertEqual(cm.exception.code, "path_denied")
        rt = Path(self.cfg.runtime_dir); (rt).mkdir(parents=True, exist_ok=True)
        cfg = dataclasses.replace(self.cfg, input_dirs=(rt,))
        (rt / "audit.jsonl").write_text("x")
        with self.assertRaises(StatementError) as cm:
            normalize_statement_file(cfg, {"source_path": str(rt / "audit.jsonl")})
        self.assertEqual(cm.exception.code, "path_denied")

    @needs_xlrd
    def test_output_name_is_safe_and_never_overwrites_other_content(self):
        rep = self.norm(content_base64=b64(FIXTURE.read_bytes()), filename="../../etc/تقرير كشف.xls")
        self.assertRegex(rep["output"]["filename"], r"^normalized_2026-09-29_2026-09-30_[0-9a-f]{8}\.xlsx$")
        self.assertEqual(Path(rep["output"]["path"]).parent, self.out)
        self.assertEqual(N.safe_output_name("a/../b c.xls", "2026-01-01", "2026-01-02", "f" * 64), "normalized_b-c_2026-01-01_2026-01-02_ffffffff.xlsx")
        Path(rep["output"]["path"]).write_bytes(b"tampered")
        with self.assertRaises(StatementError) as cm:
            self.norm(content_base64=b64(FIXTURE.read_bytes()), filename="../../etc/تقرير كشف.xls")
        self.assertEqual(cm.exception.code, "output_conflict")
        self.assertEqual(Path(rep["output"]["path"]).read_bytes(), b"tampered")

    def test_builtin_xlsx_writer_is_deterministic_and_well_formed(self):
        import datetime as dt
        import xml.dom.minidom as md
        from odoo_accountant.statements.xlsx_writer import build_statement_xlsx
        rows = [{"date": dt.date(2026, 9, 29), "payment_ref": "a & <b>", "amount": -5.5, "currency": "SAR"}]
        a, b = build_statement_xlsx(rows), build_statement_xlsx(rows)
        self.assertEqual(a, b)
        with zipfile.ZipFile(io.BytesIO(a)) as z:
            self.assertIsNone(z.testzip())
            for n in z.namelist():
                md.parseString(z.read(n))
        self.assertEqual(readers.read_xlsx(a, type("P", (), {"sheet": None, "encoding": None, "delimiter": None})()).rows[1][1], "a & <b>")


class IntegrationTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.fake = FakeOdoo(seed_statement_tables())
        self.emp = AccountingEmployee(self.cfg, client=self.fake)

    def cmd(self, name, **params):
        return self.emp.handle(CommandEnvelope(name, params, ChannelContext(channel="cli", actor_id="tester")))

    @needs_xlrd
    def test_normalize_never_touches_odoo_or_source_and_audits_hashes_only(self):
        src = self.put("s.xls", FIXTURE.read_bytes())
        r = self.cmd("normalize_statement_file", source_path=str(src))
        self.assertTrue(r.ok, r.message); self.assertEqual(r.kind, "report")
        self.assertEqual((self.fake.reads, self.fake.mutations), ([], []))
        audit = (self.rt.dir / "audit.jsonl").read_text()
        self.assertIn("statement_normalize", audit); self.assertIn(r.data["output"]["sha256"], audit)
        self.assertNotIn("ACME SENDER", audit); self.assertNotIn("SYN000001", audit)
        blocked = self.cmd("normalize_statement_file", content_base64=b64("Date,Description,Amount\n01/02/2026,a,1\n03/04/2026,b,2\n"))
        self.assertFalse(blocked.ok); self.assertIn("يحتاج توضيحًا", blocked.message)

    @needs_xlrd
    def test_preview_accepts_normalized_output_without_profile_and_stays_gated(self):
        src = self.put("s.xls", FIXTURE.read_bytes())
        out = self.cmd("normalize_statement_file", source_path=str(src)).data["output"]["path"]
        pv = self.cmd("statement_import_preview", source_path=out, **TARGET, closing_balance=None).data
        self.assertTrue(pv["ready_to_propose"], pv["blockers"])
        self.assertEqual(pv["profile"]["source"], "normalized_output")
        self.assertEqual(pv["provenance"]["source_sha256"], sha(FIXTURE.read_bytes()))
        self.assertEqual((pv["import_plan"]["to_import"], pv["import_plan"]["total_amount"]), (2, 13041.0))
        self.assertIn("xls", pv["formats"]["supported"])
        prop = self.cmd("propose_statement_import", source_path=out, **TARGET)
        self.assertTrue(prop.ok, prop.message)
        self.assertEqual(prop.data["approval"]["status"], "pending"); self.assertEqual(prop.data["approval"]["risk"], "FINANCIAL_FINAL")
        self.assertEqual(self.fake.mutations, [])          # nothing uploaded/created without approval
        self.assertNotIn("approval_code", json.dumps(prop.data))

    @needs_xlrd
    def test_registry_blocks_reimport_of_same_bank_file_even_after_normalization(self):
        src = self.put("s.xls", FIXTURE.read_bytes())
        out = self.cmd("normalize_statement_file", source_path=str(src)).data["output"]["path"]
        ImportRegistry(self.rt.dir).mark(sha(FIXTURE.read_bytes()), "imported")
        pv = self.cmd("statement_import_preview", source_path=out, **TARGET).data
        self.assertFalse(pv["ready_to_propose"]); self.assertTrue(any("SHA-256" in b for b in pv["blockers"]))

    @needs_xlrd
    def test_raw_xls_needs_explicit_profile_but_works_with_one(self):
        src = self.put("s.xls", FIXTURE.read_bytes())
        auto = self.cmd("statement_import_preview", source_path=str(src), **TARGET).data
        self.assertFalse(auto["ready_to_propose"]); self.assertTrue(any("profile صريح" in b for b in auto["blockers"]))
        prof = profile_std(format="xls", header_row=7, columns={"date": "Date", "debit": "Debit", "credit": "Credit", "payment_ref": "Description", "balance": "Balance"}, date_format="%d/%m/%y")
        ok = self.cmd("statement_import_preview", source_path=str(src), profile=prof, **TARGET).data
        self.assertTrue(ok["ready_to_propose"], ok["blockers"]); self.assertEqual(ok["import_plan"]["to_import"], 2)

    @needs_xlrd
    def test_mcp_tool_registered_strict_and_callable(self):
        self.assertEqual(len(TOOLS), 14)
        desc, schema = TOOLS["normalize_statement_file"]
        self.assertFalse(schema["additionalProperties"]); self.assertIn("source_path", schema["properties"])
        self.assertIn("source_path", TOOLS["statement_import_preview"][1]["properties"])
        self.assertIn("xls", TOOLS["statement_import_preview"][1]["properties"]["format"]["enum"])
        src = self.put("s.xls", FIXTURE.read_bytes())
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "normalize_statement_file", "arguments": {"source_path": str(src)}}}
        out = io.StringIO()
        McpServer(self.emp).serve(io.StringIO(json.dumps(req) + "\n"), out)
        res = json.loads(json.loads(out.getvalue())["result"]["content"][0]["text"])
        self.assertTrue(res["ok"]); self.assertEqual(res["data"]["totals"]["sum"], 13041.0)

    @needs_xlrd
    def test_cli_statement_normalize(self):
        src = self.put("s.xls", FIXTURE.read_bytes())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.run(["statement-normalize", "--file", str(src)], employee=self.emp, config=self.cfg)
        d = json.loads(buf.getvalue())
        self.assertEqual(code, 0); self.assertTrue(Path(d["data"]["output"]["path"]).exists())


class XlrdDependencyTests(unittest.TestCase):
    def test_pinned_optional_dependency_declared(self):
        text = (Path(__file__).parents[1] / "pyproject.toml").read_text()
        self.assertIn('xls = ["xlrd==2.0.1"]', text)
        self.assertIn("dependencies = []", text)

    def test_clear_error_when_xlrd_missing(self):
        import sys
        saved = sys.modules.pop("xlrd", None)
        sys.modules["xlrd"] = None          # makes `import xlrd` raise ImportError
        try:
            self.assertNotIn("xls", readers.supported_formats())
            with self.assertRaises(StatementError) as cm:
                readers.read_xls(FIXTURE.read_bytes(), None)
            self.assertEqual(cm.exception.code, "xls_unavailable"); self.assertIn("xlrd==2.0.1", str(cm.exception))
        finally:
            sys.modules.pop("xlrd", None)
            if saved is not None:
                sys.modules["xlrd"] = saved


if __name__ == "__main__":
    unittest.main()
