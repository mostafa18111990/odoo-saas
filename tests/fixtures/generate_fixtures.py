"""One-off generator for tests/fixtures/synthetic_bank_export.xls (development only).

Needs `xlwt` (NOT a project dependency):  pip install --target /tmp/xlwt_dev xlwt==1.3.0
Run:  PYTHONPATH=/tmp/xlwt_dev python3 tests/fixtures/generate_fixtures.py
All data is synthetic; it only mimics the *layout* of a typical Saudi bank export
(stacked Arabic/English titles, preamble with account and currency, dd/mm/yy text dates,
Credit/Debit/Balance columns, multi-line descriptions).
"""
from pathlib import Path

import xlwt

OUT = Path(__file__).with_name("synthetic_bank_export.xls")
DESC = ("حوالة محلية واردة\n                  ACME SENDER CO المرسل\n                  من مصرف تجريبي\n"
        "                  مبلغ التحويل {amount} ريال سعودي\n                  مرجع العملية {ref}\n")

wb = xlwt.Workbook(encoding="utf-8")
ws = wb.add_sheet("Report")
ws.write(0, 0, "Accounts Summary"); ws.write(0, 4, "ملخص الحسابات")
ws.write(2, 0, "Account Number"); ws.write(2, 1, "رقم الحساب"); ws.write(2, 3, "Currency"); ws.write(2, 4, "العملة")
ws.write(3, 0, "0000000000001"); ws.write(3, 3, "SAR")
for c, t in enumerate(["التاريخ", "الوصف", "دائن", "مدين", "الرصيد"]):
    ws.write(5, c, t)
for c, t in enumerate(["Date", "Description", "Credit", "Debit", "Balance"]):
    ws.write(6, c, t)
ws.write(7, 0, "29/09/26"); ws.write(7, 1, DESC.format(amount="7,452.00", ref="SYN000001"))
ws.write(7, 2, 7452.0); ws.write(7, 3, 0.0); ws.write(7, 4, 189461.1)
ws.write(8, 0, "30/09/26"); ws.write(8, 1, DESC.format(amount="5,589.00", ref="SYN000002"))
ws.write(8, 2, 5589.0); ws.write(8, 3, 0.0); ws.write(8, 4, 195050.1)
wb.save(str(OUT))
print("wrote", OUT, OUT.stat().st_size, "bytes")
