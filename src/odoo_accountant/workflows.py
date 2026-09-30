"""The six read-only workflows. They only use the client's read API."""
from __future__ import annotations

import datetime as _dt
from typing import Any

from .errors import OdooError, ValidationError

INV = ["out_invoice", "in_invoice"]
COUNT_SUM = ["__count", "amount_total:sum"]


def _clean(rows: list) -> list:
    out = []
    for r in rows or []:
        row = {}
        for k, v in r.items():
            if k in ("__extra_domain", "__domain"):
                continue
            row[k] = {"id": v[0], "name": v[1]} if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int) else v
        out.append(row)
    return out


def _d(value: Any, default: _dt.date) -> _dt.date:
    if not value:
        return default
    try:
        return _dt.date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError(f"Invalid date '{value}', expected YYYY-MM-DD") from None


def _company(company_id) -> list:
    return [["company_id", "=", company_id]] if company_id else []


def _header(client, date_from, date_to, date_field, company_id, journals=None, currencies=None) -> dict:
    dom = [["id", "=", company_id]] if company_id else []
    companies = _clean(client.search_read("res.company", dom, ["name"], limit=5))
    return {
        "period": {"from": str(date_from), "to": str(date_to), "date_field": date_field},
        "companies": [{"id": c["id"], "name": c.get("name")} for c in companies],
        "branches_note": "الفروع تظهر عبر اليوميات (journals)",
        "journals": journals or [],
        "currencies": currencies or [],
        "source": "JSON-2 read-only",
        "mutations": 0,
    }


def _pick(rows: list, key: str) -> list:
    seen, out = set(), []
    for r in rows:
        v = r.get(key)
        if isinstance(v, dict) and v["id"] not in seen:
            seen.add(v["id"]); out.append(v)
    return out


def accounting_snapshot(client, date_from=None, date_to=None, company_id=None) -> dict:
    today = _dt.date.today()
    f, t = _d(date_from, today - _dt.timedelta(days=90)), _d(date_to, today)
    base = [["date", ">=", str(f)], ["date", "<=", str(t)]] + _company(company_id)
    inv = base + [["move_type", "in", INV]]
    by_type = _clean(client.formatted_read_group("account.move", base, ["move_type", "state"], COUNT_SUM))
    by_journal = _clean(client.formatted_read_group("account.move", base, ["journal_id", "move_type"], COUNT_SUM))
    by_ccy = _clean(client.formatted_read_group("account.move", inv, ["currency_id"], COUNT_SUM))
    pay_state = _clean(client.formatted_read_group("account.move", inv, ["move_type", "payment_state", "state"], COUNT_SUM + ["amount_residual:sum"]))
    by_month = _clean(client.formatted_read_group("account.move", inv, ["invoice_date:month"], COUNT_SUM))
    overdue = _clean(client.formatted_read_group("account.move", [["move_type", "in", INV], ["state", "=", "posted"], ["payment_state", "in", ["not_paid", "partial"]], ["invoice_date_due", "<", str(today)]] + _company(company_id), ["move_type"], ["__count", "amount_residual:sum"]))
    payments = _clean(client.formatted_read_group("account.payment", [["date", ">=", str(f)], ["date", "<=", str(t)]] + _company(company_id), ["payment_type", "state"], ["__count", "amount:sum"]))
    bank = _clean(client.formatted_read_group("account.bank.statement.line", [["date", ">=", str(f)], ["date", "<=", str(t)]], ["is_reconciled"], ["__count", "amount:sum"]))
    drafts = client.search_count("account.move", base + [["state", "=", "draft"]])
    return {
        "report": "accounting-snapshot",
        "title": "الملخص المحاسبي",
        "header": _header(client, f, t, "date", company_id, _pick(by_journal, "journal_id"), _pick(by_ccy, "currency_id")),
        "moves_by_type_state": by_type,
        "moves_by_journal": by_journal,
        "invoices_payment_state": pay_state,
        "invoices_by_month": by_month,
        "overdue_all_time": overdue,
        "payments": payments,
        "bank_statement_lines": bank,
        "draft_moves": drafts,
        "notes": ["account.payment فارغ لا يعني عدم وجود تحصيل: التحصيل قد يمر عبر أسطر كشف الحساب البنكي."],
    }


def overdue_followup(client, side="out", min_days=0, limit=20, company_id=None, as_of=None) -> dict:
    if side not in ("out", "in"):
        raise ValidationError("side must be 'out' (customers) or 'in' (vendors)")
    today = _d(as_of, _dt.date.today())
    mt = "out_invoice" if side == "out" else "in_invoice"
    cutoff = today - _dt.timedelta(days=int(min_days))
    base = [["move_type", "=", mt], ["state", "=", "posted"], ["payment_state", "in", ["not_paid", "partial"]]] + _company(company_id)
    agg = ["__count", "amount_residual:sum"]
    buckets = {}
    for label, lo, hi in (("0-30", 0, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("90+", 91, None)):
        dom = base + [["invoice_date_due", "<=", str(today - _dt.timedelta(days=lo))]]
        if hi is not None:
            dom.append(["invoice_date_due", ">=", str(today - _dt.timedelta(days=hi))])
        rows = client.formatted_read_group("account.move", dom + [["invoice_date_due", "<", str(today)]], ["currency_id"], agg)
        buckets[label] = _clean(rows)
    top = _clean(client.formatted_read_group("account.move", base + [["invoice_date_due", "<=", str(cutoff)], ["invoice_date_due", "<", str(today)]], ["partner_id"], agg, order="amount_residual:sum desc", limit=int(limit)))
    acct = "asset_receivable" if side == "out" else "liability_payable"
    sign = ["amount_residual", "<", 0] if side == "out" else ["amount_residual", ">", 0]
    credits = _clean(client.formatted_read_group("account.move.line", [["account_id.account_type", "=", acct], ["reconciled", "=", False], ["parent_state", "=", "posted"], sign], ["partner_id"], ["__count", "amount_residual:sum"], order="__count desc", limit=int(limit)))
    credit_ids = {r["partner_id"]["id"] for r in credits if isinstance(r.get("partner_id"), dict)}
    for r in top:
        r["has_unallocated_credits"] = isinstance(r.get("partner_id"), dict) and r["partner_id"]["id"] in credit_ids
    ccy = _pick([r for b in buckets.values() for r in b], "currency_id")
    return {
        "report": "overdue-followup",
        "title": "المتأخرات والتحصيل" if side == "out" else "مستحقات الموردين المتأخرة",
        "header": _header(client, today, today, "invoice_date_due (as of)", company_id, currencies=ccy),
        "aging_buckets": buckets,
        "top_partners": top,
        "unallocated_credits_or_payments": credits,
        "suggested_message_template": "السادة {partner}، نود التذكير بوجود فواتير مستحقة بإجمالي {amount} {currency}. نرجو السداد أو إفادتنا بموعده. (مسودة — لا تُرسل دون موافقة)",
        "notes": ["الشركاء الذين لديهم سندات/دفعات غير مسواة (has_unallocated_credits) قد لا يكونون متأخرين فعليًا: راجع المطابقة أولًا."],
    }


def bank_match_suggest(client, journal_id=None, date_from=None, date_to=None, limit=30) -> dict:
    today = _dt.date.today()
    f, t = _d(date_from, today - _dt.timedelta(days=90)), _d(date_to, today)
    dom = [["is_reconciled", "=", False], ["date", ">=", str(f)], ["date", "<=", str(t)]] + ([["journal_id", "=", int(journal_id)]] if journal_id else [])
    by_j = _clean(client.formatted_read_group("account.bank.statement.line", dom, ["journal_id"], ["__count", "amount:sum"]))
    lines = client.search_read("account.bank.statement.line", dom, ["date", "amount", "payment_ref", "partner_id", "journal_id", "currency_id"], limit=int(limit), order="date desc")
    out = []
    for ln in lines:
        amt = ln["amount"]
        mt = "out_invoice" if amt > 0 else "in_invoice"
        cdom = [["move_type", "=", mt], ["state", "=", "posted"], ["payment_state", "in", ["not_paid", "partial"]], ["amount_residual", "=", abs(amt)]]
        if ln.get("currency_id"):
            cdom.append(["currency_id", "=", ln["currency_id"][0]])
        partner = ln["partner_id"][0] if ln.get("partner_id") else None
        if partner:
            cdom.append(["partner_id", "=", partner])
        cands = client.search_read("account.move", cdom, ["name", "partner_id", "invoice_date_due", "amount_residual", "ref"], limit=5)
        ref_text = (ln.get("payment_ref") or "")
        for c in cands:
            c["ref_in_payment_ref"] = bool(c["name"] and c["name"] in ref_text) or bool(c.get("ref") and c["ref"] in ref_text)
        if not cands:
            status, reason = "no_candidate", "لا فاتورة مفتوحة بنفس المبلغ"
        elif not partner:
            status, reason = "ambiguous", "السطر بلا شريك: لا مطابقة تلقائية"
        elif len(cands) > 1:
            status, reason = "ambiguous", "أكثر من مرشح"
        else:
            status, reason = "single_candidate", "مرشح وحيد بنفس الشريك والمبلغ والعملة"
        out.append({
            "statement_line_id": ln["id"], "date": ln["date"], "amount": amt, "journal": ln["journal_id"][1] if ln.get("journal_id") else None,
            "partner_id": partner, "status": status, "reason": reason,
            "candidates": [{"move_id": c["id"], "name": c["name"], "amount_residual": c["amount_residual"], "ref_in_payment_ref": c["ref_in_payment_ref"]} for c in cands],
        })
    blank_dom = [["is_reconciled", "=", False], ["payment_ref", "=", False], ["date", ">=", str(f)], ["date", "<=", str(t)]] + ([["journal_id", "=", int(journal_id)]] if journal_id else [])
    blank_total = client.search_count("account.bank.statement.line", blank_dom)
    blank_rows = client.search_read("account.bank.statement.line", blank_dom, ["date", "amount", "payment_reference", "journal_id"], limit=50, order="date desc")
    norm = lambda x: " ".join(str(x or "").split())
    repairable = [r for r in blank_rows if norm(r.get("payment_reference"))]
    lines_without_label = {
        "count": blank_total,
        "why_it_matters": "payment_ref (Label) فارغ ⇒ يظهر السطر بلا وصف في شاشة التسوية. السبب الشائع: استيراد يدوي بعنوان «Payment Reference» فوصل النص إلى payment_reference (مخفي).",
        "repairable": len(repairable),
        "candidates": [{"statement_line_id": r["id"], "date": r["date"], "amount": r["amount"], "journal": r["journal_id"][1] if r.get("journal_id") else None, "text_preview": norm(r["payment_reference"])[:80]} for r in repairable],
        "proposal": ({"action": "fill_statement_line_label", "params": {"lines": [{"statement_line_id": r["id"], "payment_ref": norm(r["payment_reference"])} for r in repairable]}} if repairable else None),
        "note": "الإصلاح تعديل لسطور موجودة: propose_action بهذا المحتوى ثم موافقة صريحة؛ لا ينفَّذ تلقائيًا ولا يستبدل تسمية موجودة.",
    }
    return {
        "report": "bank-match-suggest",
        "title": "اقتراحات المطابقة البنكية",
        "header": _header(client, f, t, "date", None, _pick(by_j, "journal_id")),
        "unreconciled_by_journal": by_j,
        "lines_without_label": lines_without_label,
        "suggestions": out,
        "counts": {s: sum(1 for x in out if x["status"] == s) for s in ("single_candidate", "ambiguous", "no_candidate")},
        "notes": ["لا تنفيذ: أي مطابقة تمر عبر propose_action ثم موافقة صريحة."],
    }


def partner_data_quality(client, date_from=None, date_to=None, company_id=None) -> dict:
    today = _dt.date.today()
    f, t = _d(date_from, today - _dt.timedelta(days=90)), _d(date_to, today)
    base = [["date", ">=", str(f)], ["date", "<=", str(t)]] + _company(company_id)
    inv = base + [["move_type", "in", INV], ["state", "!=", "cancel"]]
    res = {
        "invoices_total": _clean(client.formatted_read_group("account.move", inv, ["move_type"], ["__count"])),
        "invoices_partner_without_vat": _clean(client.formatted_read_group("account.move", inv + [["partner_id.vat", "=", False]], ["move_type"], ["__count"])),
        "invoices_partner_without_country": _clean(client.formatted_read_group("account.move", inv + [["partner_id.country_id", "=", False]], ["move_type"], ["__count"])),
        "invoices_without_ref": _clean(client.formatted_read_group("account.move", inv + [["ref", "=", False]], ["move_type"], ["__count"])),
    }
    sl = [["date", ">=", str(f)], ["date", "<=", str(t)]]
    res["statement_lines_total"] = client.search_count("account.bank.statement.line", sl)
    res["statement_lines_without_partner"] = client.search_count("account.bank.statement.line", sl + [["partner_id", "=", False]])
    res["suppliers_total"] = client.search_count("res.partner", [["supplier_rank", ">", 0]])
    res["suppliers_without_vat"] = client.search_count("res.partner", [["supplier_rank", ">", 0], ["vat", "=", False]])
    top = _clean(client.formatted_read_group("account.move", inv + [["partner_id.vat", "=", False]], ["partner_id"], ["__count", "amount_total:sum"], order="__count desc", limit=10))
    return {
        "report": "partner-data-quality",
        "title": "جودة بيانات الشركاء",
        "header": _header(client, f, t, "date", company_id),
        **res,
        "top_partners_missing_vat": top,
        "notes": ["عميل فرد قد يكون بلا رقم ضريبي بشكل طبيعي: لا تصنّفه مخالفة دون مراجعة.", "لا تُجلب بيانات اتصال شخصية."],
    }


def vendor_bill_review(client, move_ids=None, date_from=None, date_to=None, limit=20, company_id=None) -> dict:
    today = _dt.date.today()
    f, t = _d(date_from, today - _dt.timedelta(days=90)), _d(date_to, today)
    dom = [["move_type", "=", "in_invoice"], ["state", "!=", "cancel"]] + _company(company_id)
    dom += [["id", "in", [int(i) for i in move_ids]]] if move_ids else [["date", ">=", str(f)], ["date", "<=", str(t)]]
    bills = client.search_read("account.move", dom, ["name", "partner_id", "ref", "invoice_date", "invoice_date_due", "state", "payment_state", "amount_total", "currency_id", "journal_id"], limit=int(limit), order="date desc")
    ids = [b["id"] for b in bills]
    lines = client.search_read("account.move.line", [["move_id", "in", ids], ["display_type", "=", "product"]], ["move_id", "account_id", "tax_ids", "product_id", "price_subtotal"]) if ids else []
    by_move: dict = {}
    for l in lines:
        by_move.setdefault(l["move_id"][0], []).append(l)
    pids = sorted({b["partner_id"][0] for b in bills if b.get("partner_id")})
    no_vat = {r["id"] for r in client.search_read("res.partner", [["id", "in", pids], ["vat", "=", False]], ["id"])} if pids else set()
    findings = []
    for b in bills:
        issues = []
        if not b.get("ref"): issues.append("missing_ref")
        if not b.get("partner_id"): issues.append("missing_partner")
        elif b["partner_id"][0] in no_vat: issues.append("partner_without_vat")
        ls = by_move.get(b["id"], [])
        if not ls: issues.append("no_product_lines")
        if any(not l["product_id"] for l in ls): issues.append("line_without_product")
        if any(not l["tax_ids"] for l in ls): issues.append("line_without_tax")
        if b.get("ref") and b.get("partner_id") and client.search_count("account.move", [["move_type", "=", "in_invoice"], ["partner_id", "=", b["partner_id"][0]], ["ref", "=", b["ref"]], ["id", "!=", b["id"]], ["state", "!=", "cancel"]]):
            issues.append("possible_duplicate")
        findings.append({"move_id": b["id"], "name": b["name"], "partner": b["partner_id"][1] if b.get("partner_id") else None, "state": b["state"], "payment_state": b["payment_state"], "amount_total": b["amount_total"], "currency": b["currency_id"][1] if b.get("currency_id") else None, "journal": b["journal_id"][1] if b.get("journal_id") else None, "issues": issues, "verdict": "review" if issues else "ok"})
    return {
        "report": "vendor-bill-review",
        "title": "مراجعة فواتير الموردين",
        "header": _header(client, f, t, "date", company_id, [{"name": j} for j in sorted({x["journal"] for x in findings if x["journal"]})], [{"name": c} for c in sorted({x["currency"] for x in findings if x["currency"]})]),
        "reviewed": len(findings),
        "needs_review": sum(1 for x in findings if x["issues"]),
        "bills": findings,
        "notes": ["لا يُخمَّن حساب أو ضريبة؛ التصحيح يمر عبر propose_action بعد قرار المستخدم."],
    }


def period_close_check(client, month=None, company_id=None) -> dict:
    today = _dt.date.today()
    if month:
        try:
            first = _dt.date.fromisoformat(month + "-01")
        except ValueError:
            raise ValidationError("month must be YYYY-MM") from None
    else:
        first = today.replace(day=1)
    nxt = (first.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
    last = nxt - _dt.timedelta(days=1)
    base = [["date", ">=", str(first)], ["date", "<=", str(last)]] + _company(company_id)
    drafts = _clean(client.formatted_read_group("account.move", base + [["state", "=", "draft"]], ["journal_id"], COUNT_SUM))
    draft_inv = client.search_count("account.move", base + [["state", "=", "draft"], ["move_type", "in", INV]])
    unrec = _clean(client.formatted_read_group("account.bank.statement.line", [["date", "<=", str(last)], ["is_reconciled", "=", False]], ["journal_id"], ["__count", "amount:sum"]))
    credits = _clean(client.formatted_read_group("account.move.line", [["account_id.account_type", "=", "asset_receivable"], ["reconciled", "=", False], ["parent_state", "=", "posted"], ["amount_residual", "<", 0], ["date", "<=", str(last)]], ["account_id"], ["__count", "amount_residual:sum"]))
    no_partner = client.search_count("account.move", base + [["move_type", "in", INV], ["partner_id", "=", False]])
    no_due = client.search_count("account.move", base + [["move_type", "in", INV], ["state", "=", "posted"], ["invoice_date_due", "=", False]])
    tot = _clean(client.formatted_read_group("account.move", base + [["state", "=", "posted"], ["move_type", "in", INV]], ["move_type"], ["__count", "amount_untaxed:sum", "amount_tax:sum", "amount_total:sum"]))
    locks, lock_note = {}, None
    try:
        rows = client.search_read("res.company", [["id", "=", company_id]] if company_id else [], ["name", "fiscalyear_lock_date", "tax_lock_date", "sale_lock_date", "purchase_lock_date"], limit=5)
        locks = {r["name"]: {k: v for k, v in r.items() if k.endswith("lock_date")} for r in rows}
    except OdooError as exc:
        lock_note = f"تعذّر قراءة تواريخ القفل: {exc.name}"
    n_drafts = sum(r.get("__count", 0) for r in drafts)
    n_unrec = sum(r.get("__count", 0) for r in unrec)
    checks = [
        {"check": "لا قيود مسودة في الفترة", "ok": n_drafts == 0, "count": n_drafts},
        {"check": "لا أسطر بنكية غير مطابقة حتى نهاية الفترة", "ok": n_unrec == 0, "count": n_unrec},
        {"check": "لا فواتير بلا شريك", "ok": no_partner == 0, "count": no_partner},
        {"check": "لا فواتير مرحّلة بلا تاريخ استحقاق", "ok": no_due == 0, "count": no_due},
    ]
    return {
        "report": "period-close-check",
        "title": f"جاهزية إقفال {first:%Y-%m}",
        "header": _header(client, first, last, "date", company_id),
        "draft_moves_by_journal": drafts,
        "draft_invoices": draft_inv,
        "unreconciled_statement_lines": unrec,
        "unallocated_customer_credits": credits,
        "sales_purchases_totals_posted": tot,
        "lock_dates": locks,
        "lock_note": lock_note,
        "checklist": checks,
        "ready": all(c["ok"] for c in checks),
        "notes": ["القفل قرار المستخدم وحده عبر propose_action(set_period_lock)."],
    }


WORKFLOWS = {
    "accounting_snapshot": accounting_snapshot,
    "overdue_followup": overdue_followup,
    "bank_match_suggest": bank_match_suggest,
    "partner_data_quality": partner_data_quality,
    "vendor_bill_review": vendor_bill_review,
    "period_close_check": period_close_check,
}
