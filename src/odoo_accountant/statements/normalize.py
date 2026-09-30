"""Turn a RawTable (or pre-normalized rows) into validated NormalizedLine objects."""
from __future__ import annotations

import datetime as _dt
import re
import unicodedata

from ..errors import StatementError
from .limits import MAX_REF_LEN, MAX_ROWS
from .records import NormalizedLine, RawTable, issue

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def norm_header(s) -> str:
    s = unicodedata.normalize("NFKC", str(s or "")).strip().lower()
    s = re.sub(r"[ً-ْـ]", "", s)
    s = s.translate({ord("أ"): "ا", ord("إ"): "ا", ord("آ"): "ا", ord("ى"): "ي"})
    return re.sub(r"\s+", " ", s)


def parse_number(raw, decimal=".", thousands="", drcr=False):
    """Return float or None (empty). Raises ValueError with a short code on bad input."""
    if raw is None or isinstance(raw, bool):
        if raw is None:
            return None
        raise ValueError("not_a_number")
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).translate(_ARABIC_DIGITS).replace("٫", ".").replace("٬", ",").replace(" ", " ").replace(" ", " ").strip()
    if not s:
        return None
    neg = False
    if drcr:
        m = re.search(r"\s*(CR|DR|دائن|مدين)\s*$", s, re.I)
        if m:
            neg = m.group(1).upper() in ("DR", "مدين")
            s = s[: m.start()]
    s = re.sub(r"^[A-Z]{3}\s*|\s*[A-Z]{3}$", "", s.strip())
    s = s.replace(" ", "")
    if s.startswith("(") and s.endswith(")"):
        neg, s = (not neg) if neg else True, s[1:-1]
    if s.endswith("-"):
        neg, s = True, s[:-1]
    if s.startswith("-"):
        if neg and not drcr:
            raise ValueError("double_negative")
        neg, s = True, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if not s:
        raise ValueError("not_a_number")
    if thousands == " ":
        thousands = ""
    parts = s.split(decimal)
    if len(parts) > 2:
        raise ValueError("multiple_decimal")
    ip = parts[0]
    fp = parts[1] if len(parts) == 2 else ""
    if thousands:
        t = re.escape(thousands)
        if not re.match(rf"^(\d{{1,3}}({t}\d{{3}})+|\d+)$", ip):
            raise ValueError("bad_thousands")
        ip = ip.replace(thousands, "")
    if not re.match(r"^\d+$", ip) or (fp and not re.match(r"^\d+$", fp)):
        raise ValueError("not_a_number")
    val = float(f"{ip}.{fp or '0'}")
    return -val if neg else val


def parse_date(v, fmt):
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    s = str(v or "").translate(_ARABIC_DIGITS).strip()
    if not s:
        return None
    if not fmt:
        raise ValueError("no_date_format")
    return _dt.datetime.strptime(s, fmt).date()


def clean_text(s, limit=MAX_REF_LEN):
    s = _CTRL.sub(" ", unicodedata.normalize("NFKC", str(s or "")))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit], len(s) > limit


def _resolve(headers_norm, spec, label):
    if isinstance(spec, int):
        return spec - 1
    spec = str(spec)
    if spec.startswith("#") and spec[1:].isdigit():
        return int(spec[1:]) - 1
    n = norm_header(spec)
    hits = [i for i, h in enumerate(headers_norm) if h == n]
    if not hits:
        raise StatementError("column_missing", f"العمود «{spec}» ({label}) غير موجود في الملف. الأعمدة المتاحة: " + "، ".join(h for h in headers_norm if h))
    if len(hits) > 1:
        raise StatementError("column_ambiguous", f"العمود «{spec}» ({label}) مكرر في الملف؛ استخدم #رقم العمود.")
    return hits[0]


def _cell(row, idx):
    return row[idx] if 0 <= idx < len(row) else None


def normalize_table(table: RawTable, profile, statement_currency: str | None = None):
    """-> (lines, issues, info). Never guesses: every choice comes from the profile."""
    issues: list = []
    rows = table.rows
    hr = profile.header_row
    if len(rows) < hr:
        raise StatementError("no_header", "الملف أقصر من موضع صف العناوين في الـ profile.")
    headers = [norm_header(h) for h in rows[hr - 1]]
    data = rows[hr:]
    if profile.skip_footer_rows:
        data = data[: -profile.skip_footer_rows] if profile.skip_footer_rows < len(data) else []
    cols = profile.columns
    idx = {
        "date": _resolve(headers, cols["date"], "date"),
        "amount": _resolve(headers, cols["amount"], "amount") if "amount" in cols else None,
        "debit": _resolve(headers, cols["debit"], "debit") if "debit" in cols else None,
        "credit": _resolve(headers, cols["credit"], "credit") if "credit" in cols else None,
        "balance": _resolve(headers, cols["balance"], "balance") if "balance" in cols else None,
        "partner_name": _resolve(headers, cols["partner_name"], "partner_name") if "partner_name" in cols else None,
        "currency": _resolve(headers, cols["currency"], "currency") if "currency" in cols else None,
    }
    ref_spec = cols["payment_ref"]
    ref_idx = [_resolve(headers, s, "payment_ref") for s in (ref_spec if isinstance(ref_spec, list) else [ref_spec])]

    lines: list = []
    n = 0
    for offset, row in enumerate(data):
        if not any(c not in (None, "") for c in row):
            continue
        n += 1
        if n > MAX_ROWS:
            raise StatementError("too_many_rows", f"عدد الحركات يتجاوز الحد المسموح ({MAX_ROWS}).")
        src = hr + 1 + offset
        ok = True
        try:
            d = parse_date(_cell(row, idx["date"]), profile.date_format)
            if d is None:
                raise ValueError("empty")
        except ValueError as exc:
            msg = "تنسيق التاريخ غير محدد في الـ profile" if str(exc) == "no_date_format" else f"تاريخ غير صالح «{_cell(row, idx['date'])}» (المتوقع {profile.date_format})"
            issues.append(issue(src, "bad_date", msg, field_name="date")); ok = False; d = None
        amount = None
        try:
            if idx["amount"] is not None:
                amount = parse_number(_cell(row, idx["amount"]), profile.decimal_separator, profile.thousands_separator, profile.drcr_suffix)
                if amount is None:
                    raise ValueError("empty")
            else:
                deb = parse_number(_cell(row, idx["debit"]), profile.decimal_separator, profile.thousands_separator)
                cre = parse_number(_cell(row, idx["credit"]), profile.decimal_separator, profile.thousands_separator)
                if (deb or 0) and (cre or 0):
                    raise ValueError("both_debit_credit")
                if deb is None and cre is None:
                    raise ValueError("empty")
                amount = (cre or 0.0) - abs(deb or 0.0)
        except ValueError as exc:
            why = {"both_debit_credit": "مدين ودائن معًا في نفس الصف", "empty": "مبلغ فارغ", "bad_thousands": "فواصل الآلاف غير صحيحة", "multiple_decimal": "أكثر من فاصل عشري"}.get(str(exc), "مبلغ غير صالح")
            issues.append(issue(src, "bad_amount", f"{why} «{_cell(row, idx['amount'] if idx['amount'] is not None else idx['debit'])}»", field_name="amount")); ok = False
        if amount is not None and profile.invert_amount:
            amount = -amount
        if amount is not None and abs(amount) < 1e-9:
            issues.append(issue(src, "zero_amount", "مبلغ صفر: لا يُستورد", field_name="amount")); ok = False
        ref_parts, truncated = [], False
        for ri in ref_idx:
            t, tr = clean_text(_cell(row, ri))
            truncated = truncated or tr
            if t:
                ref_parts.append(t)
        ref, tr2 = clean_text(" | ".join(ref_parts))
        if not ref:
            issues.append(issue(src, "empty_ref", "البيان (payment_ref) فارغ", field_name="payment_ref")); ok = False
        if truncated or tr2:
            issues.append(issue(src, "ref_truncated", f"البيان اقتُطع إلى {MAX_REF_LEN} حرفًا", "warning", "payment_ref"))
        bal = None
        if idx["balance"] is not None:
            try:
                bal = parse_number(_cell(row, idx["balance"]), profile.decimal_separator, profile.thousands_separator, profile.drcr_suffix)
            except ValueError:
                issues.append(issue(src, "bad_balance", "رصيد غير صالح", "error", "balance")); ok = False
        cur = None
        if idx["currency"] is not None:
            cur = clean_text(_cell(row, idx["currency"]), 10)[0].upper() or None
        if cur and statement_currency and cur != statement_currency:
            issues.append(issue(src, "currency_mismatch", f"عملة الصف {cur} تخالف عملة الكشف {statement_currency}", field_name="currency")); ok = False
        pname = clean_text(_cell(row, idx["partner_name"]), 200)[0] or None if idx["partner_name"] is not None else None
        if ok:
            lines.append(NormalizedLine(len(lines) + 1, d.isoformat(), round(amount, 6), ref, pname, bal, cur, src))
    info = {"rows_total": n, "rows_parsed": len(lines), "rows_rejected": n - len(lines)}
    return lines, issues, info


def normalize_rows(rows: list, statement_currency: str | None = None):
    """Pre-normalized input (e.g. from a Telegram file handler): ISO dates, '.' decimals."""
    if not isinstance(rows, list) or not rows:
        raise StatementError("rows_empty", "لا توجد صفوف في rows.")
    if len(rows) > MAX_ROWS:
        raise StatementError("too_many_rows", f"عدد الصفوف ({len(rows)}) يتجاوز الحد المسموح ({MAX_ROWS}).")
    issues, lines = [], []
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            issues.append(issue(i, "row_not_object", "الصف ليس كائنًا")); continue
        extra = set(r) - {"date", "amount", "payment_ref", "partner_name", "balance", "currency"}
        ok = True
        if extra:
            issues.append(issue(i, "unknown_keys", "مفاتيح غير معروفة: " + ", ".join(sorted(extra)))); ok = False
        try:
            d = _dt.date.fromisoformat(str(r.get("date")))
        except ValueError:
            issues.append(issue(i, "bad_date", f"تاريخ غير صالح «{r.get('date')}» (المتوقع YYYY-MM-DD)", field_name="date")); ok = False; d = None
        try:
            amount = parse_number(r.get("amount"), ".", "")
            if amount is None:
                raise ValueError("empty")
            if abs(amount) < 1e-9:
                issues.append(issue(i, "zero_amount", "مبلغ صفر: لا يُستورد", field_name="amount")); ok = False
        except ValueError:
            issues.append(issue(i, "bad_amount", f"مبلغ غير صالح «{r.get('amount')}»", field_name="amount")); ok = False; amount = None
        ref, _ = clean_text(r.get("payment_ref"))
        if not ref:
            issues.append(issue(i, "empty_ref", "البيان (payment_ref) فارغ", field_name="payment_ref")); ok = False
        bal = None
        if r.get("balance") not in (None, ""):
            try:
                bal = parse_number(r["balance"], ".", "")
            except ValueError:
                issues.append(issue(i, "bad_balance", "رصيد غير صالح", field_name="balance")); ok = False
        cur = (str(r["currency"]).upper() if r.get("currency") else None)
        if cur and statement_currency and cur != statement_currency:
            issues.append(issue(i, "currency_mismatch", f"عملة الصف {cur} تخالف عملة الكشف {statement_currency}", field_name="currency")); ok = False
        if ok:
            lines.append(NormalizedLine(len(lines) + 1, d.isoformat(), round(amount, 6), ref, clean_text(r.get("partner_name"), 200)[0] or None, bal, cur, i))
    return lines, issues, {"rows_total": len(rows), "rows_parsed": len(lines), "rows_rejected": len(rows) - len(lines)}
