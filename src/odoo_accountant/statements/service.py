"""Statement import: read-only preview and the parameters for the import proposal."""
from __future__ import annotations

import base64
import binascii
import re

from ..errors import StatementError
from .dedupe import ImportRegistry, assign_fingerprints, classify_against_existing, file_sha256, rows_sha256, unique_import_id
from .detect import detect
from .limits import EXISTING_LINES_CAP, DUP_WINDOW_DAYS, MAX_BASE64_CHARS, MAX_BYTES, MAX_ISSUES_SHOWN, MAX_ROWS
from .limits import OUTPUT_SHEET
from .normalize import normalize_rows, normalize_table
from .normalizer import verify_normalized_output
from .inputs import read_source_bytes, resolve_source_path
from .profiles import MappingProfile, formats_compatible, resolve_profile
from .readers import PLANNED, detect_format, read_table, supported_formats
from .target import check_target
from .validate import check_consistency

ARG_KEYS = {"source_path", "content_base64", "rows", "filename", "format", "profile", "company_id", "journal_id", "bank_account_id",
            "currency", "opening_balance", "closing_balance", "include_possible_duplicates", "allow_reimport_file", "idempotency_key"}
TARGET_KEYS = ("company_id", "journal_id", "bank_account_id", "currency")
_TARGET_AR = {"company_id": "الشركة (company_id)", "journal_id": "اليومية (journal_id)", "bank_account_id": "الحساب البنكي (bank_account_id)", "currency": "العملة (currency)"}


def _decode_b64(s) -> bytes:
    if not isinstance(s, str) or not s:
        raise StatementError("content_invalid", "content_base64 يجب أن يكون نصًا غير فارغ.")
    s = re.sub(r"^data:[^;]*;base64,", "", s.strip())
    if len(s) > MAX_BASE64_CHARS:
        raise StatementError("file_too_big", f"حجم الملف يتجاوز الحد المسموح ({MAX_BYTES // (1024 * 1024)} ميغابايت).")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", s), validate=True)
    except (binascii.Error, ValueError):
        raise StatementError("base64_invalid", "المحتوى ليس base64 صالحًا.") from None
    if len(data) > MAX_BYTES:
        raise StatementError("file_too_big", f"حجم الملف يتجاوز الحد المسموح ({MAX_BYTES // (1024 * 1024)} ميغابايت).")
    if not data:
        raise StatementError("empty_file", "الملف فارغ.")
    return data


def _num(v, name):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise StatementError("balance_invalid", f"{name} يجب أن يكون رقمًا.")
    return float(v)


def _int_or_none(v, name):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise StatementError("target_invalid", f"{_TARGET_AR.get(name, name)} يجب أن يكون عددًا صحيحًا موجبًا.")
    return v


def run_preview(client, config, args: dict, profiles_dir=None):
    """Read-only. Returns (report, plan). `plan['params']` is set only when ready_to_propose."""
    extra = set(args) - ARG_KEYS
    if extra:
        raise StatementError("args_unknown", "معاملات غير معروفة: " + ", ".join(sorted(extra)))
    has_p = args.get("source_path") is not None
    has_c, has_r = args.get("content_base64") is not None, args.get("rows") is not None
    if (has_p + has_c + has_r) != 1:
        raise StatementError("input_required", "مطلوب مدخل واحد بالضبط: source_path (ملف محلي مسموح) أو content_base64 (ملف) أو rows (صفوف جاهزة).")
    company_id, journal_id, bank_id = (_int_or_none(args.get(k), k) for k in ("company_id", "journal_id", "bank_account_id"))
    currency = args.get("currency")
    if currency is not None and not re.match(r"^[A-Z]{3}$", str(currency)):
        raise StatementError("target_invalid", "العملة يجب أن تكون رمزًا من 3 أحرف كبيرة مثل SAR.")
    opening, closing = _num(args.get("opening_balance"), "opening_balance"), _num(args.get("closing_balance"), "closing_balance")
    include = args.get("include_possible_duplicates") or []
    if not isinstance(include, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in include):
        raise StatementError("include_invalid", "include_possible_duplicates يجب أن تكون قائمة أرقام صفوف.")
    profile = resolve_profile(args.get("profile"), profiles_dir)

    blockers: list = []
    warnings: list = []
    report: dict = {"report": "statement-import-preview", "title": "معاينة استيراد كشف حساب بنكي (بدون كتابة)", "mutations": 0}

    # ---- input -> lines ------------------------------------------------
    detection = None
    provenance = None
    if has_c or has_p:
        if has_p:
            real = resolve_source_path(args["source_path"], config)
            data, fname = read_source_bytes(real), (args.get("filename") or real.name)
        else:
            data, fname = _decode_b64(args["content_base64"]), args.get("filename")
        fmt = detect_format(fname, data, args.get("format"))
        sha = file_sha256(data)
        file_info = {"name": (fname or "")[:255], "format": fmt, "size_bytes": len(data), "sha256": sha}
        table = read_table(data, fmt, profile)
        file_info.update({k: v for k, v in table.meta.items() if k in ("encoding", "delimiter", "sheet")})
        work_profile, profile_source = profile, ("saved_or_inline" if profile else None)
        std = verify_normalized_output(data) if (profile is None and fmt == "xlsx") else None
        if std is not None:     # output of normalize_statement_file (or an identical standard layout): explicit by construction
            cols = {"date": "Date", "amount": "Amount", "payment_ref": "Payment Reference"}
            if std.get("_with_currency"):
                cols["currency"] = "Currency"
            work_profile = MappingProfile.from_dict({"name": "odoo-normalized", "bank": "normalized", "format": "xlsx", "sheet": OUTPUT_SHEET, "columns": cols})
            profile_source = "normalized_output"
            provenance = {k: v for k, v in std.items() if not k.startswith("_")}
        elif profile is None:
            detection = detect(table)
            if detection["suggested_profile"] and detection["complete"]:
                sp = dict(detection["suggested_profile"])
                try:
                    work_profile = MappingProfile.from_dict(sp)
                    profile_source = "auto_detected"
                except StatementError:
                    work_profile = None
            blockers.append("لا يوجد profile صريح؛ اعتمد الـ profile المقترح (احفظه باسم بنك) ثم أعد المعاينة.")
        elif not formats_compatible(profile.format, fmt):
            blockers.append(f"الـ profile معرَّف لصيغة {profile.format} والملف {fmt}.")
        if work_profile is None:
            lines, issues, info = [], [], {"rows_total": 0, "rows_parsed": 0, "rows_rejected": 0}
        else:
            lines, issues, info = normalize_table(table, work_profile, currency)
    else:
        rows = args["rows"]
        sha = rows_sha256(rows) if isinstance(rows, list) else ""
        file_info = {"name": (args.get("filename") or "rows")[:255], "format": "normalized_rows", "sha256": sha}
        lines, issues, info = normalize_rows(rows, currency)
        work_profile, profile_source = None, "normalized_rows"

    # ---- target --------------------------------------------------------
    target_vals = {"company_id": company_id, "journal_id": journal_id, "bank_account_id": bank_id, "currency": currency}
    missing = [k for k, v in target_vals.items() if not v]
    target_info, dp = {}, 2
    if missing:
        blockers.append("بيانات الهدف ناقصة (لا تخمين): " + "، ".join(_TARGET_AR[k] for k in missing))
    else:
        target_info, tblock, dp = check_target(client, company_id, journal_id, bank_id, currency)
        blockers.extend(tblock)

    # decimals check
    if lines:
        bad = [l for l in lines if abs(round(l.amount, dp) - l.amount) > 1e-6]
        for l in bad[:MAX_ISSUES_SHOWN]:
            issues.append({"row": l.source_row, "field": "amount", "code": "too_many_decimals", "severity": "error", "message": f"المبلغ {l.amount} يتجاوز {dp} منازل عشرية للعملة."})
        if bad:
            lines = [l for l in lines if l not in bad]
            info["rows_rejected"] += len(bad); info["rows_parsed"] -= len(bad)

    consistency, cissues = check_consistency(lines, opening, closing, dp)
    issues.extend(cissues)
    errors = [i for i in issues if i["severity"] == "error"]
    if info["rows_rejected"]:
        blockers.append(f"{info['rows_rejected']} صف مرفوض؛ صحّح الملف أو الـ profile (لا استيراد جزئي صامت).")
    for i in errors:
        if i["code"] in ("balance_mismatch", "running_balance", "opening_vs_first_balance"):
            blockers.append(i["message"])
    if not lines and not blockers:
        blockers.append("لا توجد حركات صالحة.")

    # ---- duplicates -----------------------------------------------------
    duplicates = {"exact": [], "possible": [], "counts": {}}
    registry = ImportRegistry(config.runtime_dir)
    file_seen = registry.status(sha) if sha else None
    if not file_seen and provenance and provenance.get("source_sha256"):
        file_seen = registry.status(provenance["source_sha256"])      # same bank file normalized differently
    if file_seen:
        msg = f"بصمة الملف SHA-256 سبق تسجيلها ({file_seen.get('status')})."
        if file_seen.get("status") in ("imported", "import_unverified") and not args.get("allow_reimport_file"):
            blockers.append(msg + " لإعادة الفحص على مستوى الحركات فقط مرّر allow_reimport_file=true.")
        else:
            warnings.append(msg)
    to_import, fps = [], []
    if lines and not missing and not [b for b in blockers if b.startswith(("اليومية", "الشركة", "الحساب", "العملة"))]:
        fps = assign_fingerprints(lines, journal_id, currency, dp)
        dmin = min(l.date for l in lines); dmax = max(l.date for l in lines)
        import datetime as _dt
        lo = (_dt.date.fromisoformat(dmin) - _dt.timedelta(days=DUP_WINDOW_DAYS)).isoformat()
        hi = (_dt.date.fromisoformat(dmax) + _dt.timedelta(days=DUP_WINDOW_DAYS)).isoformat()
        existing = client.search_read("account.bank.statement.line", [["journal_id", "=", journal_id], ["date", ">=", lo], ["date", "<=", hi]],
                                      ["date", "amount", "payment_ref", "unique_import_id"], limit=EXISTING_LINES_CAP + 1)
        if len(existing) > EXISTING_LINES_CAP:
            blockers.append("نطاق التواريخ يحوي حركات كثيرة جدًا في Odoo لفحص التكرار بأمان؛ قسّم الكشف.")
        else:
            cls = classify_against_existing(lines, fps, existing, dp)
            by_idx = {l.index: l for l in lines}
            for c in cls:
                l = by_idx[c["index"]]
                row = {"row": l.source_row, "index": c["index"], "date": l.date, "amount": l.amount, "payment_ref": l.payment_ref[:80], **{k: v for k, v in c.items() if k not in ("index", "status")}}
                if c["status"] == "exact":
                    duplicates["exact"].append(row)
                elif c["status"] == "possible":
                    duplicates["possible"].append(row)
            poss_idx = {r["index"] for r in duplicates["possible"]}
            exact_idx = {r["index"] for r in duplicates["exact"]}
            bad_inc = [i for i in include if i not in poss_idx]
            if bad_inc:
                blockers.append("include_possible_duplicates تحوي فهارس ليست «تكرارًا محتملًا»: " + str(bad_inc))
            for l, fp in zip(lines, fps):
                if l.index in exact_idx or (l.index in poss_idx and l.index not in include):
                    continue
                to_import.append((l, fp))
            report["existing_window_count"] = len(existing)
    duplicates["counts"] = {"exact": len(duplicates["exact"]), "possible": len(duplicates["possible"])}
    duplicates["exact"], duplicates["possible"] = duplicates["exact"][:MAX_ISSUES_SHOWN], duplicates["possible"][:MAX_ISSUES_SHOWN]
    if lines and fps and not to_import and not blockers:
        blockers.append("لا توجد حركات جديدة للاستيراد (كلها مكررة أو مستبعدة).")

    explicit_profile = profile is not None or profile_source in ("normalized_rows", "normalized_output")
    ready = not blockers and explicit_profile and bool(to_import) and not missing
    params = None
    if ready:
        params = {
            "company_id": company_id, "journal_id": journal_id, "bank_account_id": bank_id, "currency": currency,
            "file_sha256": sha, "source_filename": file_info["name"], "profile_name": (profile.name if profile else "normalized_rows"),
            "opening_balance": opening, "closing_balance": closing,
            "lines": [dict({"date": l.date, "amount": round(l.amount, dp), "payment_ref": l.payment_ref, "fp": fp},
                           **({"partner_name": l.partner_name} if l.partner_name else {})) for l, fp in to_import],
        }
    report.update({
        "ready_to_propose": ready,
        "blockers": blockers,
        "warnings": warnings,
        "file": file_info,
        "profile": {"name": profile.name if profile else None, "source": profile_source, "mapping": (profile.to_dict() if profile else None)},
        "detection": detection,
        "parsing": {**info, "issues_total": len(issues), "issues": issues[:MAX_ISSUES_SHOWN]},
        "consistency": consistency,
        "target": target_info,
        "duplicates": duplicates,
        "import_plan": {"to_import": len(to_import), "total_amount": round(sum(l.amount for l, _ in to_import), dp),
                        "excluded_exact": duplicates["counts"]["exact"],
                        "excluded_possible": max(0, duplicates["counts"]["possible"] - len([i for i in include if i in {r['index'] for r in duplicates['possible']}])),
                        "included_possible": len([i for i in include if i in {r['index'] for r in duplicates['possible']}]),
                        "creates": "account.bank.statement.line (قيود كشف بنكي مرحّلة)", "reconciliation": "لا تسوية ولا ترحيل إضافي؛ التسوية مرحلة منفصلة بموافقة مستقلة."},
        "sample": [{"date": l.date, "amount": l.amount, "payment_ref": l.payment_ref[:80]} for l in lines[:5]],
        "limits": {"max_bytes": MAX_BYTES, "max_rows": MAX_ROWS},
        "formats": {"supported": supported_formats(), "planned_not_supported": sorted(PLANNED)},
        "provenance": provenance,
    })
    return report, {"params": params, "ready": ready, "sha": sha}
