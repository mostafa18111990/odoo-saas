"""normalize_statement_file: any supported bank export -> one standard, inert XLSX.

Local and read-only with respect to Odoo and to the source: the source is only read,
the output is a NEW file in the configured output directory. Ambiguity stops the run
(no guessing); nothing is produced unless every row is accepted.
"""
from __future__ import annotations

import base64
import binascii
import datetime as _dt
import hashlib
import re
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

from ..errors import StatementError
from ..storage import atomic_write_bytes, ensure_dir
from .dedupe import file_sha256
from .detect import detect, detect_account_hint, detect_currency
from .inputs import read_source_bytes, resolve_source_path
from .limits import MAX_BASE64_CHARS, MAX_BYTES, MAX_ISSUES_SHOWN, NORMALIZER_VERSION, OUTPUT_COLUMNS, OUTPUT_SHEET
from .normalize import normalize_table
from .profiles import MappingProfile, formats_compatible, resolve_profile
from .readers import detect_format, read_table, supported_formats, PLANNED
from .validate import balance_chain_report, check_consistency
from .xlsx_writer import build_statement_xlsx, read_custom_properties

ARG_KEYS = {"source_path", "content_base64", "filename", "format", "profile", "currency", "include_currency", "sheet",
            "opening_balance", "closing_balance", "decimal_places"}


def _b64(s) -> bytes:
    if not isinstance(s, str) or not s:
        raise StatementError("content_invalid", "content_base64 يجب أن يكون نصًا غير فارغ.")
    s = re.sub(r"^data:[^;]*;base64,", "", s.strip())
    if len(s) > MAX_BASE64_CHARS:
        raise StatementError("file_too_big", f"حجم الملف يتجاوز الحد المسموح ({MAX_BYTES // (1024 * 1024)} ميغابايت).")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", s), validate=True)
    except (binascii.Error, ValueError):
        raise StatementError("base64_invalid", "المحتوى ليس base64 صالحًا.") from None
    if not data:
        raise StatementError("empty_file", "الملف فارغ.")
    if len(data) > MAX_BYTES:
        raise StatementError("file_too_big", f"حجم الملف يتجاوز الحد المسموح ({MAX_BYTES // (1024 * 1024)} ميغابايت).")
    return data


def safe_output_name(source_name: str | None, dmin: str, dmax: str, digest: str) -> str:
    stem = Path(source_name or "statement").name.rsplit(".", 1)[0]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()[:40]
    return f"normalized_{slug + '_' if slug else ''}{dmin}_{dmax}_{digest[:8]}.xlsx"


def _num(v, name):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise StatementError("balance_invalid", f"{name} يجب أن يكون رقمًا.")
    return float(v)


def normalize_statement_file(config, args: dict, profiles_dir=None) -> dict:
    extra = set(args) - ARG_KEYS
    if extra:
        raise StatementError("args_unknown", "معاملات غير معروفة: " + ", ".join(sorted(extra)))
    if (args.get("source_path") is None) == (args.get("content_base64") is None):
        raise StatementError("input_required", "مطلوب مدخل واحد بالضبط: source_path (ملف محلي مسموح) أو content_base64.")
    opening, closing = _num(args.get("opening_balance"), "opening_balance"), _num(args.get("closing_balance"), "closing_balance")
    dp = args.get("decimal_places", 2)
    if isinstance(dp, bool) or not isinstance(dp, int) or not 0 <= dp <= 4:
        raise StatementError("dp_invalid", "decimal_places يجب أن يكون بين 0 و4.")
    currency = args.get("currency")
    if currency is not None and not re.match(r"^[A-Z]{3}$", str(currency)):
        raise StatementError("currency_invalid", "العملة يجب أن تكون رمزًا من 3 أحرف كبيرة مثل SAR.")

    # ---- input (read-only) ------------------------------------------------
    real = None
    if args.get("source_path") is not None:
        real = resolve_source_path(args["source_path"], config)
        data, name = read_source_bytes(real), args.get("filename") or real.name
    else:
        data, name = _b64(args["content_base64"]), args.get("filename")
    src_sha = file_sha256(data)
    fmt = detect_format(name, data, args.get("format"))
    if fmt in PLANNED:
        raise StatementError("format_planned", f"صيغة {PLANNED[fmt]} مخطَّط لها وغير مدعومة بعد. المدعوم فعليًا: " + "، ".join(s.upper() for s in supported_formats()) + ".")
    profile = resolve_profile(args.get("profile"), profiles_dir)
    reader_opts = profile or SimpleNamespace(sheet=args.get("sheet"), encoding=None, delimiter=None)
    if profile is not None and args.get("sheet") is not None:
        reader_opts = profile
    table = read_table(data, fmt, reader_opts)

    report: dict = {
        "report": "normalize-statement-file", "title": "تطبيع كشف الحساب البنكي (بدون أي تعديل للمصدر وبدون لمس Odoo)",
        "ok": False, "needs_clarification": False, "mutations": 0, "odoo_touched": False,
        "source": {"name": (name or "")[:255], "format": fmt, "size_bytes": len(data), "sha256": src_sha,
                   **{k: v for k, v in table.meta.items() if k in ("sheet", "encoding", "delimiter")}},
        "output": None, "warnings": [], "blockers": [],
    }
    if fmt in ("xlsx", "xls"):
        if table.meta.get("formula_cells"):
            report["warnings"].append(f"يحوي المصدر {table.meta['formula_cells']} خلية معادلة: لم تُنفَّذ أي معادلة وقُرئت القيم المخزَّنة فقط.")
        if table.meta.get("error_cells"):
            report["warnings"].append(f"{table.meta['error_cells']} خلية خطأ/تاريخ غير صالح في المصدر تُركت فارغة.")

    # ---- mapping: explicit profile or unambiguous detection only ----------
    detection = None
    if profile is None:
        detection = detect(table)
        report["detection"] = {k: detection.get(k) for k in ("complete", "header_row", "stacked_header_rows", "ambiguities", "confidence", "suggested_profile")}
        if not detection["complete"]:
            report["needs_clarification"] = True
            report["blockers"].append("تعذّر تحديد الأعمدة بلا غموض؛ لا تخمين. " + " ".join(detection["ambiguities"]) + " حدّد profile صريحًا ثم أعد المحاولة.")
            return _finish(report, real, data, args)
        work = MappingProfile.from_dict({**detection["suggested_profile"], "name": "auto-detected"})
        profile_source = "auto_detected"
    else:
        if not formats_compatible(profile.format, fmt):
            raise StatementError("profile_format", f"الـ profile معرَّف لصيغة {profile.format} والملف {fmt}.")
        work, profile_source = profile, "explicit"
    report["profile"] = {"source": profile_source, "mapping": work.to_dict()}
    if work.date_format and "%y" in work.date_format:
        report["warnings"].append("التاريخ بسنة من رقمين (%y): تُفسَّر 00–68 كـ 2000–2068 و69–99 كـ 1969–1999؛ راجع التواريخ الناتجة.")
    if profile_source == "auto_detected":
        report["warnings"].append("الأعمدة اكتُشفت تلقائيًا (دائن/Credit = وارد موجب، مدين/Debit = صادر سالب، أو عمود Amount كما هو). راجع الاتجاه عبر فحص الرصيد أدناه.")

    # ---- parse -------------------------------------------------------------
    detected_ccy = detect_currency(table)
    statement_ccy = currency or work.default_currency or detected_ccy
    if currency and detected_ccy and currency != detected_ccy:
        report["blockers"].append(f"العملة المطلوبة {currency} تخالف عملة الملف المكتشفة {detected_ccy}.")
    lines, issues, info = normalize_table(table, work, statement_ccy)
    report["currency"] = {"value": statement_ccy, "detected_in_file": detected_ccy, "provided": currency}
    if (acct := detect_account_hint(table)):
        report["source_account_hint"] = acct
    if args.get("include_currency") and not statement_ccy:
        report["blockers"].append("طُلب عمود Currency لكن العملة غير معروفة؛ مرّر currency صراحةً.")
    report["counts"] = {**info, "rows_accepted": len(lines)}
    rejected = [i for i in issues if i["severity"] == "error"]
    report["issues_total"] = len(issues)
    report["issues"] = issues[:MAX_ISSUES_SHOWN]
    if rejected:
        report["blockers"].append(f"{info['rows_rejected']} صف مرفوض؛ لم يُنتَج ملف (لا تحويل جزئي صامت). صحّح المصدر أو الـ profile.")
    if not lines and not rejected:
        report["blockers"].append("لا توجد حركات صالحة في الملف.")

    # ---- validation: sum + balance chain ---------------------------------
    if lines:
        cons, cissues = check_consistency(lines, opening, closing, dp)
        chain = balance_chain_report(lines, opening, closing, dp)
        report["totals"] = {"movements": len(lines), "sum": cons["sum_of_movements"], "credits": cons["credits"], "debits": cons["debits"], "min_date": cons.get("min_date"), "max_date": cons.get("max_date")}
        report["balance"] = {"consistency": {k: v for k, v in cons.items() if k not in ("sum_of_movements", "credits", "debits")}, "chain": chain}
        for i in cissues:
            if i["severity"] == "warning":
                report["warnings"].append(i["message"])
        if cons["status"] == "mismatch":
            report["blockers"].append(next(i["message"] for i in cissues if i["code"] == "balance_mismatch"))
        if chain.get("status") == "broken":
            first = chain["breaks"][0] if chain["breaks"] else None
            why = (f"أول كسر عند الصف {first['row']}: المتوقع {first['expected_balance']} والفعلي {first['actual_balance']} (فرق {first['difference']})." if first else
                   f"فرق الرصيد الافتتاحي/الختامي: {chain.get('opening_difference')} / {chain.get('closing_difference')}.")
            report["blockers"].append("تسلسل الرصيد غير متسق مع الحركات. " + why)
    report["ok"] = not report["blockers"]
    if not report["ok"]:
        return _finish(report, real, data, args)

    # ---- write the standard XLSX ------------------------------------------
    rows = [{"date": _dt.date.fromisoformat(l.date), "payment_ref": l.payment_ref, "amount": l.amount, "currency": statement_ccy} for l in lines]
    props = {"source_sha256": src_sha, "source_format": fmt, "rows": len(rows), "sum": f"{report['totals']['sum']:.{dp}f}",
             "min_date": report["totals"]["min_date"], "max_date": report["totals"]["max_date"]}
    if statement_ccy:
        props["currency"] = statement_ccy
    xlsx = build_statement_xlsx(rows, include_currency=bool(args.get("include_currency")), decimal_places=dp, properties=props)
    out_sha = file_sha256(xlsx)
    out_dir = ensure_dir(Path(config.output_dir))
    fname = safe_output_name(name, report["totals"]["min_date"], report["totals"]["max_date"], out_sha)
    path = out_dir / fname
    if path.exists() and file_sha256(path.read_bytes()) != out_sha:
        raise StatementError("output_conflict", "يوجد ملف بالاسم نفسه بمحتوى مختلف؛ لن يُستبدل.")
    if not path.exists():
        atomic_write_bytes(path, xlsx, 0o600)

    # ---- round-trip verification of what was written ------------------------
    rt_ok, rt_detail = _roundtrip(path.read_bytes(), lines, dp, bool(args.get("include_currency")))
    report["output"] = {"path": str(path), "filename": fname, "size_bytes": len(xlsx), "sha256": out_sha, "sheet": OUTPUT_SHEET,
                        "columns": list(OUTPUT_COLUMNS) + (["Currency"] if args.get("include_currency") else []), "roundtrip_verified": rt_ok, "roundtrip": rt_detail}
    if not rt_ok:
        report["ok"] = False
        report["blockers"].append("فشل التحقق الراجع من الملف الناتج؛ لا تستخدمه. " + rt_detail.get("reason", ""))
    report["next_steps"] = ["راجع الملف والتقرير ثم استعمل statement_import_preview مع الملف الناتج (بدون profile) وأهداف Odoo الصريحة.", "لا شيء رُفع إلى Odoo؛ أي استيراد يحتاج propose_statement_import ثم موافقة صريحة."]
    return _finish(report, real, data, args)


def _roundtrip(xlsx: bytes, lines, dp: int, with_ccy: bool):
    from .readers import read_xlsx
    prof = SimpleNamespace(sheet=OUTPUT_SHEET, encoding=None, delimiter=None)
    try:
        t = read_xlsx(xlsx, prof)
    except StatementError as exc:
        return False, {"reason": str(exc)}
    want_hdr = list(OUTPUT_COLUMNS) + (["Currency"] if with_ccy else [])
    if t.rows[0][:len(want_hdr)] != want_hdr or len(t.rows) != len(lines) + 1:
        return False, {"reason": "العناوين أو عدد الصفوف لا يطابق."}
    with zipfile.ZipFile(io.BytesIO(xlsx)) as z:
        if b"<f>" in z.read("xl/worksheets/sheet1.xml") or b"<f " in z.read("xl/worksheets/sheet1.xml"):
            return False, {"reason": "وُجدت معادلة في الناتج."}
    for l, r in zip(lines, t.rows[1:]):
        if not (isinstance(r[0], _dt.date) and r[0].isoformat() == l.date and r[1] == l.payment_ref and isinstance(r[2], (int, float)) and abs(r[2] - round(l.amount, dp)) < 10 ** -(dp + 1)):
            return False, {"reason": f"اختلاف في الصف {l.source_row}."}
    return True, {"rows": len(lines), "dates_are_date_cells": True, "amounts_numeric": True, "no_formulas": True}


def _finish(report: dict, real, data: bytes, args: dict) -> dict:
    if real is not None:
        report["source"]["unchanged_after_run"] = file_sha256(real.read_bytes()) == report["source"]["sha256"]
    report["source"]["modified"] = False
    return report


def verify_normalized_output(data: bytes) -> dict | None:
    """If `data` is a standard normalized XLSX (by structure) return its provenance properties, else None."""
    from .readers import read_xlsx
    try:
        t = read_xlsx(data, SimpleNamespace(sheet=OUTPUT_SHEET, encoding=None, delimiter=None))
    except StatementError:
        return None
    if not t.rows:
        return None
    hdr = [h for h in t.rows[0] if h is not None]
    if hdr not in (list(OUTPUT_COLUMNS), list(OUTPUT_COLUMNS) + ["Currency"]):
        return None
    if any(not isinstance(r[0], _dt.date) for r in t.rows[1:] if r):
        return None
    return read_custom_properties(data) | {"_with_currency": len(hdr) == 4}
