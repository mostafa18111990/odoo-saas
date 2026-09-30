"""Format readers. Supported for real: CSV and XLSX (stdlib only).

OFX/QFX/CAMT.053 are *planned*: the registry has slots and a clear Arabic
"not supported yet" error, but no untested parser is shipped.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import re
import xml.etree.ElementTree as ET
import zipfile

from ..errors import StatementError
from .limits import MAX_ROWS, MAX_XLSX_UNCOMPRESSED
from .records import RawTable

SUPPORTED = ("csv", "xlsx", "xls")
PLANNED = {"ofx": "OFX", "qfx": "QFX", "camt053": "CAMT.053"}
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
XLS_REQUIREMENT = "xlrd==2.0.1"


def xls_available() -> bool:
    try:
        import xlrd  # noqa: F401
        return True
    except ImportError:
        return False


def supported_formats() -> list:
    """Formats that can really be read right now (xls needs the pinned optional xlrd)."""
    return ["csv", "xlsx"] + (["xls"] if xls_available() else [])


def _is_encrypted_ole(data: bytes) -> bool:
    return data[:8] == OLE_MAGIC and (b"E\x00n\x00c\x00r\x00y\x00p\x00t\x00e\x00d\x00P\x00a\x00c\x00k\x00a\x00g\x00e\x00" in data
                                      or b"E\x00n\x00c\x00r\x00y\x00p\x00t\x00i\x00o\x00n\x00I\x00n\x00f\x00o\x00" in data)


def _has_vba_ole(data: bytes) -> bool:
    return data[:8] == OLE_MAGIC and (b"_\x00V\x00B\x00A\x00_\x00P\x00R\x00O\x00J\x00E\x00C\x00T\x00" in data)


def detect_format(filename: str | None, data: bytes, hint: str | None) -> str:
    if _is_encrypted_ole(data):
        raise StatementError("encrypted", "الملف مشفَّر أو محميّ بكلمة مرور؛ لا يمكن قراءته. أزل التشفير ثم أعد المحاولة.")
    h = (hint or "").lower().strip().lstrip(".")
    if h == "xlsm" or (filename or "").lower().endswith((".xlsm", ".xlsb", ".xltm")):
        raise StatementError("macros", "ملفات Excel ذات الماكرو (xlsm/xlsb) مرفوضة. احفظ الكشف كـ XLSX أو CSV بلا ماكرو.")
    if h in ("camt.053", "camt_053", "camt"):
        h = "camt053"
    if h:
        if h in SUPPORTED or h in PLANNED:
            return h
        raise StatementError("format_unknown", f"الصيغة «{hint}» غير معروفة. المدعوم فعليًا: CSV وXLSX وXLS.")
    name = (filename or "").lower()
    for ext in ("xlsx", "xls", "csv", "ofx", "qfx"):
        if name.endswith("." + ext):
            return ext
    if data[:2] == b"PK":
        return "xlsx"
    if data[:8] == OLE_MAGIC:
        return "xls"
    head = data[:2048].lstrip().lower()
    if head.startswith(b"ofxheader") or b"<ofx>" in head:
        return "ofx"
    if b"camt.053" in head:
        return "camt053"
    return "csv"


def read_table(data: bytes, fmt: str, profile) -> RawTable:
    if fmt in PLANNED:
        raise StatementError("format_planned", f"صيغة {PLANNED[fmt]} مخطَّط لها لكنها غير مدعومة بعد. المدعوم فعليًا: CSV وXLSX وXLS.")
    if fmt == "csv":
        return read_csv(data, profile)
    if fmt == "xlsx":
        return read_xlsx(data, profile)
    if fmt == "xls":
        return read_xls(data, profile)
    raise StatementError("format_unknown", f"صيغة غير مدعومة: {fmt}")


# --------------------------------------------------------------------- CSV
def _decode(data: bytes, encoding: str | None) -> tuple[str, str]:
    tries = [encoding] if encoding else ["utf-8-sig", "cp1256"]
    for enc in tries:
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    raise StatementError("decode_failed", "تعذّر فك ترميز الملف. حدّد الترميز في الـ profile (مثل utf-8 أو cp1256).")


def _sniff_delimiter(text: str) -> str:
    lines = [l for l in text.splitlines() if l.strip()][:20]
    if not lines:
        raise StatementError("empty_file", "الملف فارغ.")
    best, best_score = None, (0, 0)
    tie = False
    for cand in (",", ";", "\t", "|"):
        counts = [len(next(csv.reader([l], delimiter=cand))) for l in lines]
        modal = max(set(counts), key=counts.count)
        if modal < 2:
            continue
        score = (counts.count(modal), modal)
        if score > best_score:
            best, best_score, tie = cand, score, False
        elif score == best_score:
            tie = True
    if best is None:
        raise StatementError("delimiter_unknown", "تعذّر اكتشاف الفاصل بين الأعمدة. حدّد delimiter في الـ profile.")
    if tie:
        raise StatementError("delimiter_ambiguous", "الفاصل غير واضح (أكثر من احتمال متساوٍ). حدّد delimiter في الـ profile.")
    return best


def read_csv(data: bytes, profile) -> RawTable:
    text, enc = _decode(data, getattr(profile, "encoding", None) if profile else None)
    if text.startswith("﻿"):
        text = text[1:]
    delim = (getattr(profile, "delimiter", None) if profile else None) or _sniff_delimiter(text)
    rows = []
    for row in csv.reader(io.StringIO(text), delimiter=delim):
        rows.append([c.strip() for c in row])
        if len(rows) > MAX_ROWS + 200:
            raise StatementError("too_many_rows", f"عدد الصفوف يتجاوز الحد المسموح ({MAX_ROWS}).")
    return RawTable(rows, {"format": "csv", "encoding": enc, "delimiter": delim})


# -------------------------------------------------------------------- XLSX
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_BUILTIN_DATE_IDS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))


def _xml(data: bytes):
    low = data[:4096].lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise StatementError("xlsx_unsafe", "ملف XLSX يحتوي تعريفات XML غير آمنة ورُفض.")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise StatementError("xlsx_corrupt", "ملف XLSX تالف أو غير صالح.") from None


def _is_date_format(code: str) -> bool:
    c = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code).lower()
    return bool(re.search(r"[dy]", c))


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref.upper()).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(data: bytes, profile) -> RawTable:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise StatementError("xlsx_corrupt", "ملف XLSX تالف أو ليس ملف Excel صالحًا.") from None
    if sum(i.file_size for i in zf.infolist()) > MAX_XLSX_UNCOMPRESSED:
        raise StatementError("xlsx_too_big", "حجم محتوى XLSX بعد فك الضغط يتجاوز الحد المسموح.")
    names = set(zf.namelist())
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise StatementError("macros", "الملف يحتوي وحدات ماكرو (vbaProject) ومرفوض لأسباب أمنية. احفظه بلا ماكرو.")
    if any(n.startswith("xl/encrypted") or n == "EncryptedPackage" for n in names):
        raise StatementError("encrypted", "الملف مشفَّر؛ لا يمكن قراءته.")
    if "xl/workbook.xml" not in names:
        raise StatementError("xlsx_corrupt", "ملف XLSX لا يحتوي مصنفًا (workbook).")
    wb = _xml(zf.read("xl/workbook.xml"))
    date1904 = False
    pr = wb.find(_NS + "workbookPr")
    if pr is not None and pr.get("date1904") in ("1", "true"):
        date1904 = True
    sheets = [(s.get("name"), s.get(_RNS + "id")) for s in wb.find(_NS + "sheets") if s.get("state") in (None, "visible")]
    if not sheets:
        raise StatementError("xlsx_no_sheet", "لا توجد أوراق ظاهرة في المصنف.")
    want = getattr(profile, "sheet", None) if profile else None
    if want is None:
        if len(sheets) > 1:
            raise StatementError("xlsx_sheet_ambiguous", "المصنف يحتوي أكثر من ورقة: حدّد sheet (اسمًا أو رقمًا) في الـ profile. الأوراق: " + "، ".join(n for n, _ in sheets))
        name, rid = sheets[0]
    else:
        pick = None
        if isinstance(want, int) or (isinstance(want, str) and want.isdigit()):
            i = int(want) - 1
            pick = sheets[i] if 0 <= i < len(sheets) else None
        else:
            pick = next((s for s in sheets if s[0] == want), None)
        if not pick:
            raise StatementError("xlsx_sheet_missing", f"الورقة «{want}» غير موجودة. المتاح: " + "، ".join(n for n, _ in sheets))
        name, rid = pick
    rels = _xml(zf.read("xl/_rels/workbook.xml.rels")) if "xl/_rels/workbook.xml.rels" in names else None
    target = None
    if rels is not None:
        for r in rels.findall(_PKG_REL + "Relationship"):
            if r.get("Id") == rid:
                target = r.get("Target")
    if not target:
        raise StatementError("xlsx_corrupt", "تعذّر تحديد ملف الورقة داخل XLSX.")
    path = target.lstrip("/") if target.startswith("/") else "xl/" + target
    if path not in names:
        raise StatementError("xlsx_corrupt", "ملف الورقة مفقود داخل XLSX.")

    shared: list = []
    if "xl/sharedStrings.xml" in names:
        for si in _xml(zf.read("xl/sharedStrings.xml")).findall(_NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(_NS + "t")))
    date_xf: set = set()
    if "xl/styles.xml" in names:
        st = _xml(zf.read("xl/styles.xml"))
        custom = {}
        nf = st.find(_NS + "numFmts")
        if nf is not None:
            custom = {int(n.get("numFmtId")): n.get("formatCode", "") for n in nf}
        xfs = st.find(_NS + "cellXfs")
        if xfs is not None:
            for i, xf in enumerate(xfs):
                fid = int(xf.get("numFmtId", "0"))
                if fid in _BUILTIN_DATE_IDS or (fid in custom and _is_date_format(custom[fid])):
                    date_xf.add(i)

    sheet = _xml(zf.read(path))
    formula_cells = sum(1 for _ in sheet.iter(_NS + "f"))
    base = _dt.date(1904, 1, 1) if date1904 else _dt.date(1899, 12, 30)
    out: dict = {}
    for row in sheet.iter(_NS + "row"):
        rnum = int(row.get("r", "0")) or (max(out) + 1 if out else 1)
        cells: dict = {}
        for c in row.findall(_NS + "c"):
            ref = c.get("r")
            col = _col_index(ref) if ref else (max(cells) + 1 if cells else 0)
            t, v = c.get("t"), c.find(_NS + "v")
            val = None
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(_NS + "t"))
            elif t in ("str", "e") and v is not None:
                val = v.text
            elif t == "b" and v is not None:
                val = bool(int(v.text))
            elif v is not None and v.text not in (None, ""):
                num = float(v.text)
                if int(c.get("s", "0")) in date_xf:
                    val = base + _dt.timedelta(days=int(num))
                else:
                    val = int(num) if num.is_integer() and abs(num) < 1e15 else num
            cells[col] = val.strip() if isinstance(val, str) else val
        if cells:
            width = max(cells) + 1
            out[rnum] = [cells.get(i) for i in range(width)]
        if len(out) > MAX_ROWS + 200:
            raise StatementError("too_many_rows", f"عدد الصفوف يتجاوز الحد المسموح ({MAX_ROWS}).")
    last = max(out) if out else 0
    rows = [out.get(i, []) for i in range(1, last + 1)]
    return RawTable(rows, {"format": "xlsx", "sheet": name, "formula_cells": formula_cells})


def _pick_sheet(names: list, want):
    """names: visible sheet names in order -> index. Ambiguity is an error, never a guess."""
    if want is None:
        if len(names) > 1:
            raise StatementError("xlsx_sheet_ambiguous", "المصنف يحتوي أكثر من ورقة: حدّد sheet (اسمًا أو رقمًا). الأوراق: " + "، ".join(names))
        return 0
    if isinstance(want, int) or (isinstance(want, str) and want.isdigit()):
        i = int(want) - 1
        if 0 <= i < len(names):
            return i
    elif want in names:
        return names.index(want)
    raise StatementError("xlsx_sheet_missing", f"الورقة «{want}» غير موجودة. المتاح: " + "، ".join(names))


def read_xls(data: bytes, profile) -> RawTable:
    """Legacy BIFF (.xls, OLE2) via the pinned optional `xlrd`. Formulas are never evaluated (cached values only)."""
    try:
        import xlrd
    except ImportError:
        raise StatementError("xls_unavailable", f"قراءة XLS تتطلب الحزمة {XLS_REQUIREMENT} (ثبّتها: pip install \"{XLS_REQUIREMENT}\"). أو احفظ الكشف كـ XLSX/CSV.") from None
    if data[:8] != OLE_MAGIC:
        raise StatementError("xls_corrupt", "الملف ليس بصيغة XLS صالحة (OLE2).")
    if _is_encrypted_ole(data):
        raise StatementError("encrypted", "الملف مشفَّر أو محميّ بكلمة مرور؛ لا يمكن قراءته.")
    if _has_vba_ole(data):
        raise StatementError("macros", "الملف يحتوي وحدات ماكرو (VBA) ومرفوض لأسباب أمنية. احفظه بلا ماكرو.")
    try:
        wb = xlrd.open_workbook(file_contents=data, logfile=io.StringIO(), on_demand=False)
    except xlrd.XLRDError as exc:
        if "encrypt" in str(exc).lower() or "password" in str(exc).lower():
            raise StatementError("encrypted", "الملف مشفَّر أو محميّ بكلمة مرور؛ لا يمكن قراءته.") from None
        raise StatementError("xls_corrupt", "ملف XLS تالف أو غير مدعوم.") from None
    except Exception:  # noqa: BLE001 - xlrd can raise struct/index errors on damaged files
        raise StatementError("xls_corrupt", "ملف XLS تالف أو غير مكتمل.") from None
    visible = [i for i in range(wb.nsheets) if wb.sheet_by_index(i).visibility == 0]
    if not visible:
        raise StatementError("xlsx_no_sheet", "لا توجد أوراق ظاهرة في المصنف.")
    names = [wb.sheet_by_index(i).name for i in visible]
    sh = wb.sheet_by_index(visible[_pick_sheet(names, getattr(profile, "sheet", None) if profile else None)])
    if sh.nrows > MAX_ROWS + 200:
        raise StatementError("too_many_rows", f"عدد الصفوف يتجاوز الحد المسموح ({MAX_ROWS}).")
    rows, errors = [], 0
    for r in range(sh.nrows):
        row = []
        for c in range(sh.ncols):
            t, v = sh.cell_type(r, c), sh.cell_value(r, c)
            if t in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                val = None
            elif t == xlrd.XL_CELL_TEXT:
                val = v.strip() or None
            elif t == xlrd.XL_CELL_NUMBER:
                val = int(v) if float(v).is_integer() and abs(v) < 1e15 else float(v)
            elif t == xlrd.XL_CELL_DATE:
                try:
                    val = xlrd.xldate_as_datetime(v, wb.datemode).date()
                except (xlrd.XLDateError, ValueError, OverflowError):
                    val, errors = None, errors + 1
            elif t == xlrd.XL_CELL_BOOLEAN:
                val = bool(v)
            else:  # error cells
                val, errors = None, errors + 1
            row.append(val)
        rows.append(row)
    return RawTable(rows, {"format": "xls", "sheet": sh.name, "error_cells": errors})
