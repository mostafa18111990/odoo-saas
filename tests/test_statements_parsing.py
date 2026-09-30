import datetime as dt
import tempfile
import unittest
import zipfile
import io
from pathlib import Path

from odoo_accountant.errors import StatementError
from odoo_accountant.statements import dedupe, detect as detect_mod, readers, validate
from odoo_accountant.statements.normalize import normalize_rows, normalize_table, parse_number
from odoo_accountant.statements.profiles import MappingProfile, list_profiles, load_profile, resolve_profile, save_profile
from odoo_accountant.statements.records import NormalizedLine
from tests.statement_fixtures import CSV_STD, XLSX_ROWS, build_xlsx, excel_serial, profile_std


def prof(**kw):
    return MappingProfile.from_dict(profile_std(**kw))


def parse_csv(text, profile=None, cur="SAR", enc="utf-8"):
    p = profile or prof()
    t = readers.read_csv(text.encode(enc), p)
    return normalize_table(t, p, cur)


class NumberTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_number("1,234.50", ".", ","), 1234.5)
        self.assertEqual(parse_number("1.234,56", ",", "."), 1234.56)
        self.assertEqual(parse_number("(99.10)"), -99.1)
        self.assertEqual(parse_number("500-"), -500.0)
        self.assertEqual(parse_number("+7"), 7.0)
        self.assertEqual(parse_number("١٬٢٣٤٫٥٠", ".", ","), 1234.5)
        self.assertEqual(parse_number("SAR 12.5"), 12.5)
        self.assertEqual(parse_number("10.00 DR", drcr=True), -10.0)
        self.assertEqual(parse_number("10.00 CR", drcr=True), 10.0)
        self.assertEqual(parse_number("1 234,5", ",", " "), 1234.5)
        self.assertIsNone(parse_number(""))

    def test_rejects_garbage(self):
        for bad, dec, th in [("1.2.3", ".", ""), ("12,34,5", ".", ","), ("abc", ".", ""), ("1,2345.00", ".", ","), ("--5", ".", "")]:
            with self.assertRaises(ValueError, msg=bad):
                parse_number(bad, dec, th)


class CsvNormalizeTests(unittest.TestCase):
    def test_standard(self):
        lines, issues, info = parse_csv(CSV_STD)
        self.assertEqual(info, {"rows_total": 3, "rows_parsed": 3, "rows_rejected": 0})
        self.assertEqual([l.amount for l in lines], [1000.0, -250.5, 2000.0])
        self.assertEqual(lines[0].date, "2026-09-15")
        self.assertEqual(lines[2].balance, 12749.5)
        self.assertEqual(issues, [])

    def test_bom_preamble_footer_and_blank_rows(self):
        text = "﻿Bank export\nAccount: x\nDate,Description,Amount,Balance\n15/09/2026,A,10.00,10.00\n\n16/09/2026,B,5.00,15.00\nTOTAL,,15.00,\n"
        p = prof(header_row=3, skip_footer_rows=1)
        lines, issues, info = parse_csv(text, p)
        self.assertEqual(len(lines), 2)
        self.assertEqual(issues, [])

    def test_semicolon_and_comma_decimal(self):
        text = "Datum;Text;Betrag\n15.09.2026;Gehalt;1.234,56\n16.09.2026;Miete;-800,00\n"
        p = prof(columns={"date": "Datum", "amount": "Betrag", "payment_ref": "Text"}, date_format="%d.%m.%Y", decimal_separator=",", thousands_separator=".")
        lines, issues, _ = parse_csv(text, p)
        self.assertEqual([l.amount for l in lines], [1234.56, -800.0])

    def test_debit_credit_columns_and_invert(self):
        text = "Date,Narrative,Debit,Credit\n15/09/2026,Pay,100.00,\n16/09/2026,Receive,,250.00\n17/09/2026,Both,5.00,5.00\n"
        p = prof(columns={"date": "Date", "debit": "Debit", "credit": "Credit", "payment_ref": "Narrative"})
        lines, issues, info = parse_csv(text, p)
        self.assertEqual([l.amount for l in lines], [-100.0, 250.0])
        self.assertEqual(info["rows_rejected"], 1)
        self.assertIn("مدين ودائن", issues[0]["message"])
        inv, _, _ = parse_csv("Date,Description,Amount\n15/09/2026,X,10.00\n", prof(columns={"date": "Date", "amount": "Amount", "payment_ref": "Description"}, invert_amount=True))
        self.assertEqual(inv[0].amount, -10.0)

    def test_arabic_headers_cp1256_and_arabic_digits(self):
        p = prof(columns={"date": "التاريخ", "amount": "المبلغ", "payment_ref": "البيان"}, encoding="cp1256")
        lines, issues, _ = parse_csv('التاريخ,البيان,المبلغ\n15/09/2026,تحويل وارد,"1,000.00"\n', p, enc="cp1256")
        self.assertEqual((lines[0].date, lines[0].amount, lines[0].payment_ref), ("2026-09-15", 1000.0, "تحويل وارد"))
        self.assertEqual(readers.read_csv('التاريخ,البيان\n15/09/2026,تحويل\n'.encode("cp1256"), None).meta["encoding"], "cp1256")
        lines, _, _ = parse_csv('التاريخ,البيان,المبلغ\n١٥/٠٩/٢٠٢٦,تحويل وارد,"١٬٠٠٠٫٠٠"\n', p.__class__.from_dict({**p.to_dict(), "encoding": None}))
        self.assertEqual((lines[0].date, lines[0].amount), ("2026-09-15", 1000.0))

    def test_multi_column_reference_and_control_chars(self):
        text = "Date,Desc,Ref,Amount\n15/09/2026,Line\x07one,R-77,10.00\n"
        p = prof(columns={"date": "Date", "amount": "Amount", "payment_ref": ["Desc", "Ref"]})
        lines, _, _ = parse_csv(text, p)
        self.assertEqual(lines[0].payment_ref, "Line one | R-77")

    def test_row_errors_are_reported_not_silently_dropped(self):
        text = "Date,Description,Amount\n31/02/2026,A,10.00\n15/09/2026,,10.00\n15/09/2026,B,0.00\n15/09/2026,C,1.2.3\n15/09/2026,D,5.00\n"
        lines, issues, info = parse_csv(text, prof(columns={"date": "Date", "amount": "Amount", "payment_ref": "Description"}))
        self.assertEqual((info["rows_parsed"], info["rows_rejected"]), (1, 4))
        self.assertEqual({i["code"] for i in issues}, {"bad_date", "empty_ref", "zero_amount", "bad_amount"})
        self.assertTrue(all(i["row"] for i in issues))

    def test_currency_column_mismatch(self):
        text = "Date,Description,Amount,Ccy\n15/09/2026,A,10.00,USD\n"
        p = prof(columns={"date": "Date", "amount": "Amount", "payment_ref": "Description", "currency": "Ccy"})
        _, issues, info = parse_csv(text, p, cur="SAR")
        self.assertEqual(info["rows_rejected"], 1)
        self.assertEqual(issues[0]["code"], "currency_mismatch")

    def test_missing_and_duplicate_columns(self):
        with self.assertRaises(StatementError) as cm:
            parse_csv("Date,Desc,Amt\n15/09/2026,A,1\n")
        self.assertIn("غير موجود", str(cm.exception)); self.assertIn("الأعمدة المتاحة", str(cm.exception))
        with self.assertRaises(StatementError) as cm:
            parse_csv("Date,Description,Amount,Amount\n15/09/2026,A,1,2\n")
        self.assertEqual(cm.exception.code, "column_ambiguous")

    def test_text_date_needs_explicit_format(self):
        p = prof(date_format=None)
        _, issues, info = parse_csv(CSV_STD, p)
        self.assertEqual(info["rows_parsed"], 0)
        self.assertIn("تنسيق التاريخ غير محدد", issues[0]["message"])

    def test_delimiter_problems(self):
        with self.assertRaises(StatementError) as cm:
            readers.read_csv(b"just one column\nvalue\n", None)
        self.assertEqual(cm.exception.code, "delimiter_unknown")
        with self.assertRaises(StatementError) as cm:
            readers.read_csv(b"a,b;c\n1,2;3\n", None)
        self.assertEqual(cm.exception.code, "delimiter_ambiguous")
        with self.assertRaises(StatementError):
            readers.read_csv(b"", None)

    def test_row_limit(self):
        from odoo_accountant.statements import limits
        old = limits.MAX_ROWS
        try:
            import odoo_accountant.statements.normalize as nm
            nm.MAX_ROWS = 2
            with self.assertRaises(StatementError) as cm:
                parse_csv(CSV_STD)
            self.assertEqual(cm.exception.code, "too_many_rows")
        finally:
            nm.MAX_ROWS = old


class NormalizedRowsTests(unittest.TestCase):
    def test_rows_ok_and_errors(self):
        lines, issues, info = normalize_rows([
            {"date": "2026-09-15", "amount": 10, "payment_ref": "A"},
            {"date": "2026-09-16", "amount": "-5.25", "payment_ref": "B", "balance": "4.75"},
            {"date": "bad", "amount": 1, "payment_ref": "C"},
            {"date": "2026-09-17", "amount": 0, "payment_ref": "D"},
            {"date": "2026-09-17", "amount": 1, "payment_ref": "E", "evil": 1},
            "not-an-object",
        ], "SAR")
        self.assertEqual(info["rows_parsed"], 2)
        self.assertEqual(lines[1].amount, -5.25)
        self.assertEqual(info["rows_rejected"], 4)
        with self.assertRaises(StatementError):
            normalize_rows([], "SAR")
        with self.assertRaises(StatementError) as cm:
            normalize_rows([{"date": "2026-01-01", "amount": 1, "payment_ref": "x"}] * 5001, "SAR")
        self.assertEqual(cm.exception.code, "too_many_rows")


class XlsxTests(unittest.TestCase):
    def prof(self, **kw):
        return prof(format="xlsx", date_format=None, **kw)

    def test_shared_strings_and_dates(self):
        data = build_xlsx(XLSX_ROWS, date_cols=(0,))
        t = readers.read_xlsx(data, self.prof())
        lines, issues, info = normalize_table(t, self.prof(), "SAR")
        self.assertEqual(info["rows_parsed"], 2)
        self.assertEqual(lines[0].date, "2026-09-15")
        self.assertEqual(lines[1].amount, -250.5)
        self.assertEqual(t.meta["sheet"], "Statement")

    def test_inline_strings(self):
        t = readers.read_xlsx(build_xlsx(XLSX_ROWS, date_cols=(0,), inline=True), self.prof())
        self.assertEqual(t.rows[0][1], "Description")

    def test_1904_date_system(self):
        rows = [["Date", "Description", "Amount"], [excel_serial(2026, 9, 15) - 1462, "X", 5]]
        p = prof(format="xlsx", date_format=None, columns={"date": "Date", "amount": "Amount", "payment_ref": "Description"})
        lines, _, _ = normalize_table(readers.read_xlsx(build_xlsx(rows, date_cols=(0,), date1904=True), p), p, "SAR")
        self.assertEqual(lines[0].date, "2026-09-15")

    def test_sheet_selection_and_ambiguity(self):
        data = build_xlsx(None, sheets={"Summary": [["x"]], "Txns": XLSX_ROWS}, date_cols=(0,))
        with self.assertRaises(StatementError) as cm:
            readers.read_xlsx(data, self.prof())
        self.assertEqual(cm.exception.code, "xlsx_sheet_ambiguous")
        self.assertEqual(readers.read_xlsx(data, self.prof(sheet="Txns")).meta["sheet"], "Txns")
        self.assertEqual(readers.read_xlsx(data, self.prof(sheet=2)).meta["sheet"], "Txns")
        with self.assertRaises(StatementError):
            readers.read_xlsx(data, self.prof(sheet="Nope"))

    def test_corrupt_and_unsafe(self):
        with self.assertRaises(StatementError) as cm:
            readers.read_xlsx(b"PK not really a zip", self.prof())
        self.assertEqual(cm.exception.code, "xlsx_corrupt")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/workbook.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><workbook/>')
        with self.assertRaises(StatementError) as cm:
            readers.read_xlsx(buf.getvalue(), self.prof())
        self.assertEqual(cm.exception.code, "xlsx_unsafe")


class FormatRegistryTests(unittest.TestCase):
    def test_detection_and_planned_formats_not_claimed(self):
        self.assertEqual(readers.detect_format("a.csv", b"x", None), "csv")
        self.assertEqual(readers.detect_format("a.XLSX", b"x", None), "xlsx")
        self.assertEqual(readers.detect_format(None, b"PK\x03\x04", None), "xlsx")
        self.assertEqual(readers.detect_format("a.ofx", b"x", None), "ofx")
        self.assertEqual(readers.detect_format(None, b"<Document xmlns='urn:iso:camt.053.001.02'>", None), "camt053")
        for fmt in ("ofx", "qfx", "camt053"):
            with self.assertRaises(StatementError) as cm:
                readers.read_table(b"x", fmt, None)
            self.assertIn("غير مدعومة بعد", str(cm.exception))
        with self.assertRaises(StatementError):
            readers.detect_format("a.pdf", b"x", "pdf")
        self.assertEqual(set(readers.SUPPORTED), {"csv", "xlsx"})


class ProfileTests(unittest.TestCase):
    def test_roundtrip_and_guards(self):
        with tempfile.TemporaryDirectory() as d:
            p = prof()
            path = save_profile(p, d)
            self.assertTrue(path.exists())
            self.assertEqual(load_profile("test-bank", d).to_dict(), p.to_dict())
            self.assertEqual(list_profiles(d), ["test-bank"])
            with self.assertRaises(StatementError) as cm:
                save_profile(p, d)
            self.assertEqual(cm.exception.code, "profile_exists")
            save_profile(p, d, overwrite=True)
            with self.assertRaises(StatementError):
                load_profile("../etc/passwd", d)
            with self.assertRaises(StatementError):
                load_profile("missing", d)
            self.assertEqual(resolve_profile("test-bank", d).name, "test-bank")
            self.assertIsNone(resolve_profile(None, d))

    def test_validation(self):
        for bad in [
            profile_std(name="Bad Name"), profile_std(format="pdf"), profile_std(columns={"amount": "A", "payment_ref": "D"}),
            profile_std(columns={"date": "D", "amount": "A", "debit": "X", "payment_ref": "P"}),
            profile_std(columns={"date": "D", "debit": "X", "payment_ref": "P"}), profile_std(columns={"date": "D", "amount": "A"}),
            profile_std(decimal_separator=";"), profile_std(thousands_separator="."), profile_std(date_format="%d/%m/%Y; rm"),
            profile_std(header_row=0), profile_std(default_currency="sar"), profile_std(surprise=1), profile_std(columns={"date": "D", "amount": "A", "payment_ref": "P", "zzz": "Z"}),
        ]:
            with self.assertRaises(StatementError, msg=str(bad)):
                MappingProfile.from_dict(bad)
        with self.assertRaises(StatementError):
            resolve_profile(123)


class DetectTests(unittest.TestCase):
    def table(self, text):
        return readers.read_csv(text.encode(), None)

    def test_detects_unambiguous(self):
        d = detect_mod.detect(self.table(CSV_STD))
        self.assertTrue(d["complete"], d)
        sp = d["suggested_profile"]
        self.assertEqual(sp["date_format"], "%d/%m/%Y")
        self.assertEqual((sp["decimal_separator"], sp["thousands_separator"]), (".", ","))
        self.assertEqual(sp["columns"]["amount"], "Amount")

    def test_ambiguous_date_and_no_header(self):
        d = detect_mod.detect(self.table("Date,Description,Amount\n01/02/2026,A,10.00\n03/04/2026,B,5.00\n"))
        self.assertFalse(d["complete"])
        self.assertTrue(any("غامض" in a for a in d["ambiguities"]))
        d2 = detect_mod.detect(self.table("foo,bar\n1,2\n"))
        self.assertFalse(d2["complete"]); self.assertIsNone(d2["suggested_profile"])

    def test_arabic_headers_preamble_and_debit_credit(self):
        text = "كشف حساب\nالتاريخ,البيان,مدين,دائن,الرصيد\n15/09/2026,دفع,100.00,,900.00\n16/09/2026,إيداع,,50.00,950.00\n"
        d = detect_mod.detect(self.table(text))
        self.assertEqual(d["header_row"], 2)
        self.assertEqual(d["suggested_profile"]["columns"]["debit"], "مدين")
        self.assertTrue(d["complete"], d)

    def test_ambiguous_decimal_and_multiple_dates(self):
        d = detect_mod.detect(self.table('Date,Value Date,Description,Amount\n15/09/2026,16/09/2026,A,"1,234"\n'))
        self.assertFalse(d["complete"])


def L(i, date, amount, ref, bal=None, row=None):
    return NormalizedLine(i, date, amount, ref, None, bal, None, row or i + 1)


class ValidateTests(unittest.TestCase):
    def test_balances(self):
        lines = [L(1, "2026-09-15", 100.0, "a", 1100.0), L(2, "2026-09-16", -40.0, "b", 1060.0)]
        ok, iss = validate.check_consistency(lines, 1000.0, 1060.0, today=dt.date(2026, 9, 29))
        self.assertEqual(ok["status"], "ok"); self.assertEqual(ok["running_balance"], "ok (ascending)"); self.assertEqual(iss, [])
        bad, iss = validate.check_consistency(lines, 1000.0, 1061.0, today=dt.date(2026, 9, 29))
        self.assertEqual(bad["status"], "mismatch"); self.assertIn("balance_mismatch", {i["code"] for i in iss})
        self.assertEqual(validate.check_consistency(lines, 1000.0, None)[0]["derived_closing"], 1060.0)
        self.assertEqual(validate.check_consistency(lines, None, 1060.0)[0]["derived_opening"], 1000.0)
        self.assertEqual(validate.check_consistency(lines, None, None)[0]["status"], "not_checked")

    def test_running_balance_orders(self):
        desc = [L(1, "2026-09-16", -40.0, "b", 1060.0), L(2, "2026-09-15", 100.0, "a", 1100.0)]
        self.assertEqual(validate.check_consistency(desc, None, None)[0]["running_balance"], "ok (descending / newest first)")
        broken = [L(1, "2026-09-15", 100.0, "a", 1100.0), L(2, "2026-09-16", -40.0, "b", 999.0)]
        out, iss = validate.check_consistency(broken, None, None)
        self.assertEqual(out["running_balance"], "inconsistent"); self.assertIn("running_balance", {i["code"] for i in iss})
        out, iss = validate.check_consistency([L(1, "2026-09-15", 100.0, "a", 1100.0)], 900.0, None)
        self.assertIn("opening_vs_first_balance", {i["code"] for i in iss})

    def test_dates_and_repeats(self):
        lines = [L(1, "2099-01-01", 5.0, "x"), L(2, "2099-01-01", 5.0, "X"), L(3, "2001-01-01", 1.0, "old")]
        _, iss = validate.check_consistency(lines, None, None, today=dt.date(2026, 9, 29))
        self.assertEqual({i["code"] for i in iss}, {"future_date", "very_old_date", "repeated_rows_in_file"})
        self.assertTrue(all(i["severity"] == "warning" for i in iss))


class DedupeTests(unittest.TestCase):
    def test_fingerprints_are_occurrence_aware_and_verifiable(self):
        lines = [L(1, "2026-09-15", 10.0, "Same  Ref"), L(2, "2026-09-15", 10.0, "same ref"), L(3, "2026-09-16", 10.0, "same ref")]
        fps = dedupe.assign_fingerprints(lines, 14, "SAR")
        self.assertEqual(len(set(fps)), 3)
        self.assertEqual(fps, dedupe.assign_fingerprints(lines, 14, "SAR"))
        self.assertNotEqual(fps[0], dedupe.assign_fingerprints(lines, 15, "SAR")[0])
        ln = {"date": "2026-09-15", "amount": 10.0, "payment_ref": "x", "fp": dedupe.line_fp(14, "2026-09-15", 10.0, "SAR", "x", 3)}
        self.assertTrue(dedupe.verify_fp(14, ln, "SAR"))
        ln["amount"] = 11.0
        self.assertFalse(dedupe.verify_fp(14, ln, "SAR"))
        self.assertTrue(dedupe.unique_import_id(fps[0]).startswith("OA-"))
        self.assertEqual(len(dedupe.file_sha256(b"x")), 64)

    def test_classification(self):
        lines = [L(1, "2026-09-15", 10.0, "Alpha"), L(2, "2026-09-15", 10.0, "Alpha"), L(3, "2026-09-15", 20.0, "Beta"),
                 L(4, "2026-09-16", 30.0, "Gamma"), L(5, "2026-09-20", 40.0, "Delta")]
        fps = dedupe.assign_fingerprints(lines, 14, "SAR")
        existing = [
            {"id": 1, "date": "2026-09-15", "amount": 10.0, "payment_ref": "alpha", "unique_import_id": False},      # exact for line 1 only
            {"id": 2, "date": "2026-09-15", "amount": 20.0, "payment_ref": "Other text", "unique_import_id": False},   # possible for line 3
            {"id": 3, "date": "2026-09-18", "amount": 30.0, "payment_ref": "GAMMA", "unique_import_id": False},        # possible (nearby date)
            {"id": 4, "date": "2026-09-01", "amount": 40.0, "payment_ref": "Delta", "unique_import_id": False},        # far -> new
        ]
        st = {c["index"]: c for c in dedupe.classify_against_existing(lines, fps, existing)}
        self.assertEqual(st[1]["status"], "exact")
        self.assertEqual(st[2]["status"], "new")            # two genuine rows, one existing -> second is new
        self.assertEqual((st[3]["status"], st[3]["reason"]), ("possible", "same_date_amount_different_ref"))
        self.assertEqual((st[4]["status"], st[4]["reason"]), ("possible", "same_amount_ref_nearby_date"))
        self.assertEqual(st[5]["status"], "new")

    def test_exact_by_import_id(self):
        lines = [L(1, "2026-09-15", 10.0, "Alpha")]
        fps = dedupe.assign_fingerprints(lines, 14, "SAR")
        ex = [{"id": 7, "date": "2026-09-15", "amount": 10.0, "payment_ref": "changed label", "unique_import_id": dedupe.unique_import_id(fps[0])}]
        c = dedupe.classify_against_existing(lines, fps, ex)[0]
        self.assertEqual((c["status"], c["reason"]), ("exact", "same_import_id"))

    def test_registry(self):
        with tempfile.TemporaryDirectory() as d:
            r = dedupe.ImportRegistry(Path(d))
            self.assertIsNone(r.status("abc"))
            r.mark("abc", "proposed", approval_id="apr_1")
            r.mark("abc", "imported")
            self.assertEqual(r.status("abc")["status"], "imported")
            self.assertEqual(r.status("abc")["approval_id"], "apr_1")


if __name__ == "__main__":
    unittest.main()
