"""Mandatory pre-approval check: will the imported description actually be DISPLAYED on the reconciliation screen?

Evidence this encodes (verified read-only on Odoo 19):
  * the reconciliation screens (`*.bank_rec_widget` kanban/list/form views) display `payment_ref` (string "Label");
  * the importer matches a column header to a field by technical name first, then by label, so
    "Payment Reference" -> `payment_reference` (a related field of the journal ENTRY that the screen does not show)
    while "Label" (or "payment_ref") -> `payment_ref`;
  * `transaction_details` is read-only and not importable.
Fail closed: if the evidence cannot be read, the preview is blocked (status "unverified").
"""
from __future__ import annotations

import re

from ..errors import OdooClientError, OdooError

DISPLAY_FIELD = "payment_ref"
MODEL = "account.bank.statement.line"
_FIELD_RE = re.compile(r'<field[^>]*\bname="payment_ref"')


def resolve_header(header: str, fields: dict):
    """Replica of base_import header matching: technical name first, then the (translated) label."""
    h = str(header).strip().lower()
    for name, meta in fields.items():
        if name.lower() == h and not (meta.get("readonly") and not meta.get("related")):
            return name
    for name, meta in fields.items():
        if str(meta.get("string", "")).lower() == h and not (meta.get("readonly") and not meta.get("related")):
            return name
    return None


def check_description_visibility(client, lines=None, headers=()) -> dict:
    """lines: dicts/objects with payment_ref text to be imported; headers: column titles of a manual-import file."""
    out: dict = {"check": "description-visibility", "display_field": DISPLAY_FIELD, "status": "ok", "problems": [], "warnings": [],
                 "evidence": {}, "header_resolution": {}}
    try:
        fields = client.fields_get(MODEL, ["string", "type", "store", "readonly", "related"])
        views = client.search_read("ir.ui.view", [["model", "=", MODEL], ["active", "=", True]], ["name", "type", "arch_db"], limit=50)
    except (OdooError, OdooClientError) as exc:
        out["status"] = "unverified"
        out["problems"].append(f"تعذّر التحقق من الحقل الذي تعرضه شاشة التسوية ({type(exc).__name__}). لا موافقة قبل إثبات ظهور الوصف.")
        return out

    pr = fields.get(DISPLAY_FIELD)
    if not pr or not pr.get("store") or pr.get("readonly"):
        out["problems"].append(f"الحقل {DISPLAY_FIELD} غير موجود أو غير قابل للكتابة في هذا الإصدار من Odoo.")
    rec_views = [v for v in views if "bank_rec_widget" in (v.get("name") or "")]
    shown = [{"view_id": v["id"], "type": v["type"]} for v in rec_views if _FIELD_RE.search(v.get("arch_db") or "")]
    out["evidence"] = {"reconciliation_views": len(rec_views), "views_showing_payment_ref": shown,
                       "payment_ref_string": (pr or {}).get("string"), "payment_reference_string": (fields.get("payment_reference") or {}).get("string")}
    if not rec_views:
        out["status"] = "unverified"
        out["problems"].append("لم أجد شاشات التسوية (bank_rec_widget) لإثبات الحقل المعروض.")
    elif not shown:
        out["problems"].append(f"شاشات التسوية لا تعرض الحقل {DISPLAY_FIELD}: الوصف لن يظهر.")

    for h in headers:
        target = resolve_header(h, fields)
        out["header_resolution"][h] = target
        if target == "payment_reference":
            out["warnings"].append(f"العنوان «{h}» يُربط في معالج الاستيراد بـ payment_reference (حقل القيد) ولا يظهر في شاشة التسوية (الاستيراد اليدوي بهذا العنوان يعرض الوصف فارغًا)؛ استعمل «Label» (payment_ref). مسار الاقتراح في هذه الأداة يكتب payment_ref مباشرة ولا يتأثر.")
    if lines is not None:
        empty = sum(1 for l in lines if not str((l.get("payment_ref") if isinstance(l, dict) else getattr(l, "payment_ref", "")) or "").strip())
        out["lines_checked"], out["lines_without_description"] = len(lines), empty
        if empty:
            out["problems"].append(f"{empty} حركة بلا وصف (payment_ref فارغ): ستظهر فارغة في شاشة التسوية.")
    if out["problems"] and out["status"] == "ok":
        out["status"] = "blocked"
    return out
