"""Consistency checks: balances, running balance, dates, in-file repeats."""
from __future__ import annotations

import datetime as _dt

from .records import issue

TOL = 0.005


def check_consistency(lines, opening, closing, dp=2, today=None):
    issues: list = []
    today = today or _dt.date.today()
    total = round(sum(l.amount for l in lines), dp + 2)
    out = {
        "sum_of_movements": round(total, dp),
        "credits": round(sum(l.amount for l in lines if l.amount > 0), dp),
        "debits": round(sum(l.amount for l in lines if l.amount < 0), dp),
        "opening_balance": opening,
        "closing_balance": closing,
        "status": "not_checked",
    }
    if opening is not None and closing is not None:
        diff = round(opening + total - closing, dp + 2)
        out["difference"] = round(diff, dp)
        if abs(diff) <= TOL:
            out["status"] = "ok"
        else:
            out["status"] = "mismatch"
            issues.append(issue(None, "balance_mismatch", f"الرصيد الافتتاحي {opening} + مجموع الحركات {round(total, dp)} = {round(opening + total, dp)} لا يساوي الرصيد الختامي {closing} (الفرق {round(diff, dp)})."))
    elif opening is not None:
        out["status"], out["derived_closing"] = "derived", round(opening + total, dp)
    elif closing is not None:
        out["status"], out["derived_opening"] = "derived", round(closing - total, dp)
    # running-balance column
    bals = [l.balance for l in lines]
    if lines and all(b is not None for b in bals):
        def chain(seq):
            for i in range(1, len(seq)):
                if abs(seq[i - 1].balance + seq[i].amount - seq[i].balance) > TOL:
                    return i
            return None
        asc = chain(lines) if len(lines) > 1 else None
        ascending = asc is None
        if asc is None:
            out["running_balance"] = "ok (ascending)"
        else:
            desc = chain(list(reversed(lines)))
            if desc is None:
                out["running_balance"] = "ok (descending / newest first)"
            else:
                out["running_balance"] = "inconsistent"
                issues.append(issue(lines[asc].source_row, "running_balance", f"عمود الرصيد غير متسق مع المبالغ عند الصف {lines[asc].source_row}."))
        if ascending and opening is not None and abs(opening + lines[0].amount - lines[0].balance) > TOL:
            issues.append(issue(lines[0].source_row, "opening_vs_first_balance", "الرصيد الافتتاحي لا يتسق مع رصيد أول حركة."))
    dates = [l.date for l in lines]
    if dates:
        out["min_date"], out["max_date"] = min(dates), max(dates)
        if max(dates) > (today + _dt.timedelta(days=1)).isoformat():
            issues.append(issue(None, "future_date", f"يوجد تاريخ مستقبلي ({max(dates)}).", "warning"))
        if min(dates) < (today - _dt.timedelta(days=365 * 5)).isoformat():
            issues.append(issue(None, "very_old_date", f"يوجد تاريخ أقدم من 5 سنوات ({min(dates)}).", "warning"))
    seen: dict = {}
    for l in lines:
        k = (l.date, round(l.amount, dp), " ".join(l.payment_ref.lower().split()))
        seen[k] = seen.get(k, 0) + 1
    repeats = sum(1 for v in seen.values() if v > 1)
    if repeats:
        issues.append(issue(None, "repeated_rows_in_file", f"{repeats} مجموعة صفوف متطابقة (تاريخ/مبلغ/بيان) داخل الملف؛ قد تكون حركات مشروعة متكررة وستُستورد كلها ما لم تكن موجودة في Odoo.", "warning"))
    return out, issues


def balance_chain_report(lines, opening=None, closing=None, dp=2):
    """Explicit running-balance test with differences. Needs a Balance on every line."""
    if not lines or any(l.balance is None for l in lines):
        return {"present": False, "status": "not_available"}

    def breaks(seq):
        out = []
        for i in range(1, len(seq)):
            expected = round(seq[i - 1].balance + seq[i].amount, dp)
            actual = round(seq[i].balance, dp)
            if abs(expected - actual) > TOL:
                out.append({"row": seq[i].source_row, "expected_balance": expected, "actual_balance": actual, "difference": round(actual - expected, dp)})
        return out

    asc, desc = breaks(lines), breaks(list(reversed(lines)))
    order, bad = ("ascending", asc) if len(asc) <= len(desc) else ("descending", desc)
    timeline = lines if order == "ascending" else list(reversed(lines))
    first, last = timeline[0], timeline[-1]
    derived_opening = round(first.balance - first.amount, dp)
    rep = {"present": True, "order": order if len(lines) > 1 else "single", "status": "ok" if not bad else "broken",
           "breaks": bad[:5], "breaks_total": len(bad), "derived_opening": derived_opening, "last_balance": round(last.balance, dp)}
    if opening is not None:
        rep["opening_difference"] = round(derived_opening - opening, dp)
        if abs(rep["opening_difference"]) > TOL:
            rep["status"] = "broken"
    if closing is not None:
        rep["closing_difference"] = round(last.balance - closing, dp)
        if abs(rep["closing_difference"]) > TOL:
            rep["status"] = "broken"
    return rep
