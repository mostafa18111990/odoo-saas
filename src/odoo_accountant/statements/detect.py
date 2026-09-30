"""Suggest a mapping profile from a file. Suggestions only — never auto-imported."""
from __future__ import annotations

import datetime as _dt
import re

from .normalize import norm_header

_SYN = {
    "date": ["date", "transaction date", "booking date", "posting date", "txn date", "تاريخ", "التاريخ", "تاريخ العملية", "تاريخ الحركة"],
    "value_date": ["value date", "تاريخ القيمة", "تاريخ الاستحقاق"],
    "amount": ["amount", "المبلغ", "قيمة", "القيمة", "amount sar"],
    "debit": ["debit", "withdrawal", "withdrawals", "مدين", "سحب", "المسحوبات"],
    "credit": ["credit", "deposit", "deposits", "دائن", "ايداع", "إيداع", "الايداعات", "الإيداعات"],
    "payment_ref": ["description", "details", "narrative", "reference", "memo", "البيان", "الوصف", "التفاصيل", "المرجع", "ملاحظات"],
    "balance": ["balance", "running balance", "الرصيد"],
    "currency": ["currency", "ccy", "العملة"],
    "partner_name": ["beneficiary", "payee", "counterparty", "المستفيد"],
}
_SYN_N = {k: {norm_header(x) for x in v} for k, v in _SYN.items()}
_DATE_CANDIDATES = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%y", "%d-%b-%Y"]


def _row_score(row):
    return sum(1 for c in row if any(norm_header(c) in s for s in _SYN_N.values()))


def detect(table, max_scan=30) -> dict:
    rows = table.rows
    scored = [(i, _row_score(r)) for i, r in enumerate(rows[:max_scan])]
    best = max(scored, key=lambda x: x[1], default=(0, 0))
    amb: list = []
    if best[1] < 2:
        return {"complete": False, "header_row": None, "suggested_profile": None, "ambiguities": ["تعذّر اكتشاف صف العناوين؛ حدّد profile صراحةً."], "confidence": "none"}
    hr = best[0] + 1
    headers = rows[best[0]]
    hn = [norm_header(h) for h in headers]
    cols: dict = {}

    def pick(role):
        hits = [headers[i] for i, h in enumerate(hn) if h in _SYN_N[role]]
        return [h for h in hits if h not in (None, "")]

    d = pick("date")
    if len(d) == 1:
        cols["date"] = d[0]
    elif len(d) > 1:
        amb.append("أكثر من عمود تاريخ: " + "، ".join(d) + " — اختر واحدًا.")
    elif pick("value_date"):
        amb.append("يوجد تاريخ قيمة فقط؛ أكّد أنه عمود التاريخ المطلوب.")
    a, de, cr = pick("amount"), pick("debit"), pick("credit")
    if len(a) == 1 and not (de and cr):
        cols["amount"] = a[0]
    elif len(de) == 1 and len(cr) == 1 and not a:
        cols["debit"], cols["credit"] = de[0], cr[0]
    else:
        amb.append("تعذّر تحديد أعمدة المبلغ بلا لبس (amount أو debit/credit).")
    refs = pick("payment_ref")
    if refs:
        cols["payment_ref"] = refs if len(refs) > 1 else refs[0]
    else:
        amb.append("لا يوجد عمود بيان/وصف واضح.")
    for role in ("balance", "currency", "partner_name"):
        h = pick(role)
        if len(h) == 1:
            cols[role] = h[0]

    def col_values(title):
        i = next((i for i, h in enumerate(headers) if h == title), None)
        return [] if i is None else [r[i] for r in rows[hr:hr + 500] if i < len(r) and r[i] not in (None, "")]

    date_format, decimal, thousands = None, None, ""
    if "date" in cols:
        vals = col_values(cols["date"])
        if vals and all(isinstance(v, (_dt.date, _dt.datetime)) for v in vals):
            date_format = "(تواريخ Excel حقيقية)"
        else:
            ok = []
            for f in _DATE_CANDIDATES:
                try:
                    for v in vals:
                        _dt.datetime.strptime(str(v).strip(), f)
                    ok.append(f)
                except ValueError:
                    pass
            if len(ok) == 1:
                date_format = ok[0]
            elif len(ok) > 1:
                amb.append("تنسيق التاريخ غامض (" + " أو ".join(ok) + "): حدّد date_format.")
            else:
                amb.append("تنسيق التاريخ غير معروف: حدّد date_format.")
    numeric_titles = [cols[k] for k in ("amount", "debit", "credit", "balance") if k in cols]
    samples = [str(v) for t in numeric_titles for v in col_values(t) if isinstance(v, str)]
    if samples:
        dot = any(re.search(r"\d\.\d{1,2}$", s) for s in samples)
        com = any(re.search(r"\d,\d{1,2}$", s) for s in samples)
        if dot and not com:
            decimal, thousands = ".", ("," if any("," in s for s in samples) else "")
        elif com and not dot:
            decimal, thousands = ",", ("." if any("." in s for s in samples) else "")
        else:
            amb.append("الفاصل العشري غامض: حدّد decimal_separator وthousands_separator.")
    else:
        decimal = "."
    profile = {
        "name": "auto-detected", "format": table.meta.get("format", "csv"), "header_row": hr, "columns": cols,
        "date_format": date_format if date_format and not date_format.startswith("(") else None,
        "decimal_separator": decimal or ".", "thousands_separator": thousands,
    }
    if table.meta.get("delimiter"):
        profile["delimiter"] = table.meta["delimiter"]
    if table.meta.get("encoding"):
        profile["encoding"] = table.meta["encoding"]
    complete = not amb and all(k in cols for k in ("date", "payment_ref")) and ("amount" in cols or "debit" in cols)
    return {"complete": complete, "header_row": hr, "suggested_profile": profile, "ambiguities": amb,
            "confidence": "high" if complete else "low",
            "note": "اقتراح فقط؛ يجب اعتماده كـ profile صريح قبل أي proposal."}
