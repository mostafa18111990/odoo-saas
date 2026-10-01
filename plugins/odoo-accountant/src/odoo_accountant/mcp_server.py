"""Minimal stdio MCP server (JSON-RPC 2.0, newline-delimited). Standard library only.

Exposes 6 report tools + 5 approval-flow tools. There is deliberately NO generic
"call any Odoo method" tool.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .config import Config
from .models import ChannelContext, CommandEnvelope, to_jsonable
from .service import AccountingEmployee

PROTOCOL = "2024-11-05"

_DATE = {"type": "string", "description": "YYYY-MM-DD"}
_OBJ = lambda props, req=(): {"type": "object", "properties": props, "required": list(req), "additionalProperties": False}  # noqa: E731

_STMT_PROPS = {
    "content_base64": {"type": "string", "description": "محتوى الملف base64 (CSV أو XLSX أو XLS، حد 5MB)"},
    "source_path": {"type": "string", "description": "مسار ملف محلي داخل مجلد مسموح (inputs/ أو uploads أو ODOO_ACCOUNTANT_INPUT_DIRS) — بديل عن content_base64؛ يُقرأ فقط ولا يُعدَّل"},
    "rows": {"type": "array", "items": {"type": "object"}, "description": "بديل عن الملف: صفوف جاهزة {date(YYYY-MM-DD), amount, payment_ref, partner_name?, balance?, currency?} (حد 5000)"},
    "filename": {"type": "string"},
    "format": {"type": "string", "enum": ["csv", "xlsx", "xls", "ofx", "qfx", "camt053"], "description": "المدعوم فعليًا: csv وxlsx وxls؛ ofx/qfx/camt053 مخطَّط لها وغير مدعومة بعد"},
    "profile": {"oneOf": [{"type": "string"}, {"type": "object"}], "description": "اسم profile محفوظ أو كائن profile صريح"},
    "company_id": {"type": "integer"}, "journal_id": {"type": "integer"}, "bank_account_id": {"type": "integer"},
    "currency": {"type": "string", "description": "مثل SAR"},
    "opening_balance": {"type": "number"}, "closing_balance": {"type": "number"},
    "include_possible_duplicates": {"type": "array", "items": {"type": "integer"}, "description": "فهارس حركات «تكرار محتمل» يقرر المستخدم استيرادها"},
    "allow_reimport_file": {"type": "boolean"},
}

TOOLS: dict = {
    "accounting_snapshot": ("ملخص محاسبي مجمّع للفترة (قراءة فقط).", _OBJ({"date_from": _DATE, "date_to": _DATE, "company_id": {"type": "integer"}})),
    "overdue_followup": ("أعمار المتأخرات وأكبر الشركاء (قراءة فقط).", _OBJ({"side": {"enum": ["out", "in"]}, "min_days": {"type": "integer"}, "limit": {"type": "integer"}, "company_id": {"type": "integer"}, "as_of": _DATE})),
    "bank_match_suggest": ("اقتراح مطابقة أسطر الكشف البنكي بلا تنفيذ (قراءة فقط).", _OBJ({"journal_id": {"type": "integer"}, "date_from": _DATE, "date_to": _DATE, "limit": {"type": "integer"}})),
    "partner_data_quality": ("نواقص بيانات الشركاء (قراءة فقط).", _OBJ({"date_from": _DATE, "date_to": _DATE, "company_id": {"type": "integer"}})),
    "vendor_bill_review": ("مراجعة فواتير الموردين (قراءة فقط).", _OBJ({"move_ids": {"type": "array", "items": {"type": "integer"}}, "date_from": _DATE, "date_to": _DATE, "limit": {"type": "integer"}, "company_id": {"type": "integer"}})),
    "period_close_check": ("قائمة جاهزية إقفال شهر (قراءة فقط).", _OBJ({"month": {"type": "string", "description": "YYYY-MM"}, "company_id": {"type": "integer"}})),
    "normalize_statement_file": ("تطبيع كشف بنكي (CSV/XLSX/XLS القديم) إلى ملف XLSX قياسي جديد: ورقة Bank Transactions بأعمدة Date وLabel وAmount (وCurrency اختياريًا؛ Label هو ما يربطه Odoo بالحقل payment_ref المعروض في شاشة التسوية، وليس Payment Reference)، الوارد موجب والصادر سالب، تواريخ حقيقية yyyy-mm-dd، مع تقرير تحقق (عدد، مجموع، رصيد، checksums). لا يلمس Odoo ولا يعدّل المصدر؛ يكتب ملفًا محليًا جديدًا فقط ويتوقف عند الغموض.", _OBJ({"source_path": {"type": "string", "description": "مسار ملف محلي داخل مجلد مسموح (قراءة فقط)"}, "content_base64": {"type": "string"}, "filename": {"type": "string"}, "format": {"type": "string", "enum": ["csv", "xlsx", "xls"]}, "profile": {"oneOf": [{"type": "string"}, {"type": "object"}]}, "currency": {"type": "string"}, "include_currency": {"type": "boolean"}, "sheet": {"oneOf": [{"type": "string"}, {"type": "integer"}]}, "opening_balance": {"type": "number"}, "closing_balance": {"type": "number"}, "decimal_places": {"type": "integer"}})),
    "statement_import_preview": ("معاينة استيراد كشف حساب بنكي بدون أي كتابة: اكتشاف الأعمدة، تنسيق التواريخ والأرقام، الأرصدة، التكرارات (exact/possible)، والتحقق من الشركة واليومية والحساب البنكي والعملة. يقبل source_path (مرفق/ملف محلي مسموح) أو content_base64 (CSV/XLSX/XLS) أو rows جاهزة، ويشغّل دائمًا تحقق ظهور الوصف في شاشة التسوية (description_visibility) وهو إلزامي قبل أي اقتراح.", _OBJ({**_STMT_PROPS}) ),
    "propose_statement_import": ("اقتراح استيراد الحركات الجديدة فقط إلى account.bank.statement.line (يقبل نفس مدخلات المعاينة، وبعد نجاح description_visibility فقط؛ proposal فقط؛ يحتاج موافقة مستقلة بالكود وبصمة الحمولة). التسوية مرحلة منفصلة.", _OBJ({**_STMT_PROPS, "idempotency_key": {"type": "string"}}) ),
    "propose_action": ("تحضير عملية كتابة كخطة + طلب موافقة. لا ينفذ شيئًا في Odoo.", _OBJ({"action": {"type": "string"}, "params": {"type": "object"}, "idempotency_key": {"type": "string"}}, ["action", "params"])),
    "approve_action": ("تسجيل موافقة المستخدم على خطة (يتطلب الكود الذي يعطيه المستخدم وبصمة الحمولة).", _OBJ({"approval_id": {"type": "string"}, "code": {"type": "string"}, "payload_hash": {"type": "string"}}, ["approval_id", "code"])),
    "reject_action": ("رفض خطة معلّقة.", _OBJ({"approval_id": {"type": "string"}, "reason": {"type": "string"}}, ["approval_id"])),
    "execute_approved_action": ("تنفيذ خطة وافق عليها المستخدم (مرة واحدة). dry_run=true للفحص فقط.", _OBJ({"approval_id": {"type": "string"}, "code": {"type": "string"}, "payload_hash": {"type": "string"}, "dry_run": {"type": "boolean"}}, ["approval_id", "code"])),
    "list_pending_approvals": ("قائمة الموافقات المعلّقة/المعتمدة غير المنفذة.", _OBJ({})),
}


class McpServer:
    def __init__(self, employee: AccountingEmployee | None = None):
        self._employee = employee

    @property
    def employee(self) -> AccountingEmployee:
        if self._employee is None:
            self._employee = AccountingEmployee(Config.from_env())
        return self._employee

    def tools_list(self) -> list:
        return [{"name": n, "description": d, "inputSchema": s} for n, (d, s) in TOOLS.items()]

    def call_tool(self, name: str, args: dict) -> dict:
        if name not in TOOLS:
            return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}
        ctx = ChannelContext(channel="claude_code", actor_id="local")
        resp = self.employee.handle(CommandEnvelope(name, dict(args or {}), ctx))
        return {"isError": not resp.ok, "content": [{"type": "text", "text": json.dumps(to_jsonable(resp), ensure_ascii=False, indent=1, default=str)}]}

    def handle_message(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                result: Any = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}}, "serverInfo": {"name": "odoo-accountant", "version": __version__}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools_list()}
            elif method == "tools/call":
                p = msg.get("params", {})
                result = self.call_tool(p.get("name", ""), p.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": type(exc).__name__}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            out = self.handle_message(msg)
            if out is not None:
                stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
                stdout.flush()


def main() -> None:
    McpServer().serve()


if __name__ == "__main__":
    main()
