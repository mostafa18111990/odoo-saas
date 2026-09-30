"""Standard statement XLSX writer (stdlib only, deterministic bytes, no formulas ever).

Every text is stored as a shared string (never a formula), so cells that start with
'=', '+', '-' or '@' stay inert text. Dates are real date cells formatted yyyy-mm-dd.
"""
from __future__ import annotations

import datetime as _dt
import io
import re
import zipfile
from xml.sax.saxutils import escape

from ..errors import StatementError
from .limits import NORMALIZER_VERSION, OUTPUT_COLUMNS, OUTPUT_SHEET

_FIXED_TIME = (1980, 1, 1, 0, 0, 0)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _col(i: int) -> str:
    return "ABCDEFGH"[i]


def build_statement_xlsx(rows: list, *, include_currency: bool = False, decimal_places: int = 2, properties: dict | None = None) -> bytes:
    """rows: [{'date': date, 'payment_ref': str, 'amount': float, 'currency': str|None}]"""
    headers = list(OUTPUT_COLUMNS) + (["Currency"] if include_currency else [])
    if include_currency and any(not r.get("currency") for r in rows):
        raise StatementError("currency_missing", "عمود Currency مطلوب لكن عملة بعض الحركات غير معروفة.")
    shared: list = []
    index: dict = {}

    def sidx(text: str) -> int:
        text = _CTRL.sub(" ", text)
        if text not in index:
            index[text] = len(shared)
            shared.append(text)
        return index[text]

    n = len(rows) + 1
    last_col = _col(len(headers) - 1)
    sheet = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             f'<dimension ref="A1:{last_col}{n}"/>',
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
             '<cols><col min="1" max="1" width="13" customWidth="1"/><col min="2" max="2" width="110" customWidth="1"/>'
             '<col min="3" max="3" width="15" customWidth="1"/>' + ('<col min="4" max="4" width="10" customWidth="1"/>' if include_currency else "") + '</cols>',
             "<sheetData>"]
    sheet.append('<row r="1">' + "".join(f'<c r="{_col(i)}1" s="1" t="s"><v>{sidx(h)}</v></c>' for i, h in enumerate(headers)) + "</row>")
    base = _dt.date(1899, 12, 30)
    for i, r in enumerate(rows, 2):
        d: _dt.date = r["date"]
        cells = [f'<c r="A{i}" s="2"><v>{(d - base).days}</v></c>',
                 f'<c r="B{i}" s="4" t="s"><v>{sidx(r["payment_ref"])}</v></c>',
                 f'<c r="C{i}" s="3"><v>{r["amount"]:.{decimal_places}f}</v></c>']
        if include_currency:
            cells.append(f'<c r="D{i}" t="s"><v>{sidx(r["currency"])}</v></c>')
        sheet.append(f'<row r="{i}">' + "".join(cells) + "</row>")
    sheet.append("</sheetData></worksheet>")

    amount_fmt = '<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' if decimal_places == 2 else \
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    custom_fmt = '<numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/>' + ('' if decimal_places == 2 else f'<numFmt numFmtId="165" formatCode="{"0." + "0" * decimal_places if decimal_places else "0"}"/>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              f'<numFmts count="{1 if decimal_places == 2 else 2}">{custom_fmt}</numFmts>'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
              '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              + amount_fmt +
              '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top"/></xf>'
              '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    sst = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           f'count="{len(shared)}" uniqueCount="{len(shared)}">' + "".join(f'<si><t xml:space="preserve">{escape(t)}</t></si>' for t in shared) + "</sst>")
    props = {"normalizer_version": NORMALIZER_VERSION, **(properties or {})}
    custom = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
              'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
              + "".join(f'<property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="{i}" name="{escape(k)}"><vt:lpwstr>{escape(str(v))}</vt:lpwstr></property>'
                        for i, (k, v) in enumerate(sorted(props.items()), 2)) + "</Properties>")
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{OUTPUT_SHEET}" sheetId="1" r:id="rId1"/></sheets></workbook>')
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    wbrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              f'<Relationship Id="rId1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{R}/styles" Target="styles.xml"/>'
              f'<Relationship Id="rId3" Type="{R}/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/>'
            f'<Relationship Id="rId2" Type="{R}/custom-properties" Target="docProps/custom.xml"/></Relationships>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
          '<Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml"/></Types>')
    parts = [("[Content_Types].xml", ct), ("_rels/.rels", rels), ("docProps/custom.xml", custom), ("xl/workbook.xml", wb),
             ("xl/_rels/workbook.xml.rels", wbrels), ("xl/styles.xml", styles), ("xl/sharedStrings.xml", sst), ("xl/worksheets/sheet1.xml", "".join(sheet))]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts:               # fixed order + fixed timestamps => identical input gives identical bytes
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, text)
    return buf.getvalue()


def read_custom_properties(data: bytes) -> dict:
    """Custom document properties written by build_statement_xlsx (provenance); {} when absent."""
    import xml.etree.ElementTree as ET
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "docProps/custom.xml" not in z.namelist():
                return {}
            raw = z.read("docProps/custom.xml")
    except zipfile.BadZipFile:
        return {}
    if b"<!doctype" in raw[:2048].lower() or b"<!entity" in raw[:2048].lower():
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    out = {}
    for prop in root:
        val = next(iter(prop), None)
        if val is not None and prop.get("name"):
            out[prop.get("name")] = val.text or ""
    return out
