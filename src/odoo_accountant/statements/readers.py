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

SUPPORTED = ("csv", "xlsx")
PLANNED = {"ofx": "OFX", "qfx": "QFX", "camt053": "CAMT.053"}


def detect_format(filename: str | None, data: bytes, hint: str | None) -> str:
    h = (hint or "").lower().strip().lstrip(".")
    if h in ("camt.053", "camt_053", "camt"):
        h = "camt053"
    if h:
        if h in SUPPORTED or h in PLANNED:
            return h
        raise StatementError("format_unknown", f"الصيغة «{hint}» غير معروفة. المدعوم فعليًا: CSV وXLSX.")
    name = (filename or "").lower()
    for ext in ("xlsx", "csv", "ofx", "qfx"):
        if name.endswith("." + ext):
            return ext
    if data[:2] == b"PK":
        return "xlsx"
    head = data[:2048].lstrip().lower()
    if head.startswith(b"ofxheader") or b"<ofx>" in head:
        return "ofx"
    if b"camt.053" in head:
        return "camt053"
    return "csv"


def read_table(data: bytes, fmt: str, profile) -> RawTable:
    if fmt in PLANNED:
        raise StatementError("format_planned", f"صيغة {PLANNED[fmt]} مخطَّط لها لكنها غير مدعومة بعد. المدعوم فعليًا: CSV وXLSX.")
    if fmt == "csv":
        return read_csv(data, profile)
    if fmt == "xlsx":
        return read_xlsx(data, profile)
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
    return RawTable(rows, {"format": "xlsx", "sheet": name})
