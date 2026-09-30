"""Synthetic statement files only (no real bank data)."""
from __future__ import annotations

import base64
import io
import zipfile

CSV_STD = """Date,Description,Amount,Balance
15/09/2026,Transfer from ACME,"1,000.00","11,000.00"
16/09/2026,Fuel station,-250.50,"10,749.50"
17/09/2026,Transfer from BETA,"2,000.00","12,749.50"
"""


def b64(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode()


def profile_std(**over) -> dict:
    p = {"name": "test-bank", "bank": "TEST", "format": "csv", "header_row": 1,
         "columns": {"date": "Date", "amount": "Amount", "payment_ref": "Description", "balance": "Balance"},
         "date_format": "%d/%m/%Y", "decimal_separator": ".", "thousands_separator": ","}
    p.update(over)
    return p


def build_xlsx(rows, *, date_cols=(), sheets=None, date1904=False, inline=False, extra_sheet=False) -> bytes:
    """rows: list[list]; date_cols: indexes whose numeric values are Excel date serials."""
    from xml.sax.saxutils import escape

    shared: list = []

    def sidx(s):
        if s not in shared:
            shared.append(s)
        return shared.index(s)

    def col(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    def sheet_xml(rs):
        out = ['<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
        for r, row in enumerate(rs, 1):
            out.append(f'<row r="{r}">')
            for c, v in enumerate(row):
                ref = f"{col(c)}{r}"
                if v is None:
                    continue
                if isinstance(v, str):
                    if inline:
                        out.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(v)}</t></is></c>')
                    else:
                        out.append(f'<c r="{ref}" t="s"><v>{sidx(v)}</v></c>')
                elif c in date_cols and r > 1:
                    out.append(f'<c r="{ref}" s="1"><v>{v}</v></c>')
                else:
                    out.append(f'<c r="{ref}"><v>{v}</v></c>')
            out.append("</row>")
        out.append("</sheetData></worksheet>")
        return "".join(out)

    named = sheets or {"Statement": rows}
    sheet_files = {}
    for i, (name, rs) in enumerate(named.items(), 1):
        sheet_files[f"xl/worksheets/sheet{i}.xml"] = sheet_xml(rs)
    wb_sheets = "".join(f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(named, 1))
    wb = ('<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          + ('<workbookPr date1904="1"/>' if date1904 else "") + f"<sheets>{wb_sheets}</sheets></workbook>")
    rels = "".join(f'<Relationship Id="rId{i}" Type="x" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(named) + 1))
    rels = f'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>'
    styles = ('<?xml version="1.0"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
    sst = '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + "".join(f"<si><t>{escape(s)}</t></si>" for s in shared) + "</sst>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/sharedStrings.xml", sst)
        for n, x in sheet_files.items():
            z.writestr(n, x)
    return buf.getvalue()


def excel_serial(y, m, d) -> int:
    import datetime as dt
    return (dt.date(y, m, d) - dt.date(1899, 12, 30)).days


XLSX_ROWS = [
    ["Date", "Description", "Amount", "Balance"],
    [excel_serial(2026, 9, 15), "Transfer from ACME", 1000.0, 11000.0],
    [excel_serial(2026, 9, 16), "Fuel station", -250.5, 10749.5],
]
