"""Explicit-target verification: company, journal, bank account and currency must all be given and consistent."""
from __future__ import annotations


def _m2o(v):
    return v[0] if isinstance(v, (list, tuple)) and v else v


def _name(v):
    return v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else None


def mask(acc: str | None) -> str | None:
    if not acc:
        return None
    acc = str(acc).replace(" ", "")
    return "****" + acc[-4:] if len(acc) > 4 else "****"


def check_target(client, company_id: int, journal_id: int, bank_account_id: int, currency: str):
    """-> (info, blockers, decimal_places). Reads only."""
    blockers: list = []
    info: dict = {"company_id": company_id, "journal_id": journal_id, "bank_account_id": bank_account_id, "currency": currency}
    comp = client.read("res.company", [company_id], ["name", "currency_id"])
    jr = client.read("account.journal", [journal_id], ["name", "code", "type", "company_id", "currency_id", "bank_account_id", "active"])
    bank = client.read("res.partner.bank", [bank_account_id], ["acc_number", "active"])
    cur = client.search_read("res.currency", [["name", "=", currency], ["active", "=", True]], ["name", "decimal_places"], limit=2)
    dp = 2
    if not comp:
        blockers.append(f"الشركة {company_id} غير موجودة.")
    else:
        info["company"] = comp[0]["name"]
    if not jr:
        blockers.append(f"اليومية {journal_id} غير موجودة.")
    else:
        j = jr[0]
        info.update({"journal": j["name"], "journal_code": j["code"], "journal_type": j["type"]})
        if not j["active"]:
            blockers.append("اليومية مؤرشفة.")
        if j["type"] not in ("bank", "cash"):
            blockers.append(f"اليومية من نوع «{j['type']}»؛ المطلوب bank أو cash.")
        if comp and _m2o(j["company_id"]) != company_id:
            blockers.append(f"اليومية تتبع الشركة «{_name(j['company_id'])}» لا الشركة المحددة ({company_id}).")
        if not j["bank_account_id"]:
            blockers.append("اليومية بلا حساب بنكي مسجَّل؛ لا أخمّن الحساب.")
        elif _m2o(j["bank_account_id"]) != bank_account_id:
            blockers.append(f"الحساب البنكي {bank_account_id} لا يطابق حساب اليومية ({_m2o(j['bank_account_id'])}).")
        eff = _name(j["currency_id"]) if j["currency_id"] else (_name(comp[0]["currency_id"]) if comp else None)
        info["journal_currency"] = eff
        if eff and eff != currency:
            blockers.append(f"عملة اليومية {eff} لا تطابق عملة الكشف {currency}.")
    if not bank:
        blockers.append(f"الحساب البنكي {bank_account_id} غير موجود.")
    else:
        info["bank_account"] = mask(bank[0]["acc_number"])
        if not bank[0]["active"]:
            blockers.append("الحساب البنكي مؤرشف.")
    if not cur:
        blockers.append(f"العملة {currency} غير موجودة أو غير مفعلة.")
    else:
        dp = int(cur[0]["decimal_places"])
        info["decimal_places"] = dp
    return info, blockers, dp
