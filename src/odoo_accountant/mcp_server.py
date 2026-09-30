"""Minimal stdio MCP server (JSON-RPC 2.0, newline-delimited). Standard library only.

Exposes 6 report tools + 5 approval-flow tools. There is deliberately NO generic
"call any Odoo method" tool.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from .config import Config
from .models import ChannelContext, CommandEnvelope, to_jsonable
from .service import AccountingEmployee

PROTOCOL = "2024-11-05"

_DATE = {"type": "string", "description": "YYYY-MM-DD"}
_OBJ = lambda props, req=(): {"type": "object", "properties": props, "required": list(req), "additionalProperties": False}  # noqa: E731

TOOLS: dict = {
    "accounting_snapshot": ("ملخص محاسبي مجمّع للفترة (قراءة فقط).", _OBJ({"date_from": _DATE, "date_to": _DATE, "company_id": {"type": "integer"}})),
    "overdue_followup": ("أعمار المتأخرات وأكبر الشركاء (قراءة فقط).", _OBJ({"side": {"enum": ["out", "in"]}, "min_days": {"type": "integer"}, "limit": {"type": "integer"}, "company_id": {"type": "integer"}, "as_of": _DATE})),
    "bank_match_suggest": ("اقتراح مطابقة أسطر الكشف البنكي بلا تنفيذ (قراءة فقط).", _OBJ({"journal_id": {"type": "integer"}, "date_from": _DATE, "date_to": _DATE, "limit": {"type": "integer"}})),
    "partner_data_quality": ("نواقص بيانات الشركاء (قراءة فقط).", _OBJ({"date_from": _DATE, "date_to": _DATE, "company_id": {"type": "integer"}})),
    "vendor_bill_review": ("مراجعة فواتير الموردين (قراءة فقط).", _OBJ({"move_ids": {"type": "array", "items": {"type": "integer"}}, "date_from": _DATE, "date_to": _DATE, "limit": {"type": "integer"}, "company_id": {"type": "integer"}})),
    "period_close_check": ("قائمة جاهزية إقفال شهر (قراءة فقط).", _OBJ({"month": {"type": "string", "description": "YYYY-MM"}, "company_id": {"type": "integer"}})),
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
                result: Any = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}}, "serverInfo": {"name": "odoo-accountant", "version": "0.1.0"}}
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
