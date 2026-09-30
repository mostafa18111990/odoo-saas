"""AccountingEmployee: channel-independent command handling."""
from __future__ import annotations

from typing import Any

from .approvals import ApprovalStore, public_view
from .audit import AuditLog
from .channels.base import ChannelAdapter, LocalChannel
from .client import OdooClient
from .config import Config
from .errors import ApprovalError, OdooAccountantError, PolicyError, ValidationError
from .executor import Executor, derive_idempotency_key, get_handler
from .models import (
    ActionPlan,
    ChannelContext,
    CommandEnvelope,
    ResponseEnvelope,
    compute_payload_hash,
    to_jsonable,
)
from .policy import Policy
from .workflows import WORKFLOWS

REPORT_COMMANDS = set(WORKFLOWS)
APPROVAL_COMMANDS = {"propose_action", "approve_action", "reject_action", "execute_approved_action", "list_pending_approvals", "get_approval"}


class AccountingEmployee:
    def __init__(self, config: Config, client=None, store: ApprovalStore | None = None, audit: AuditLog | None = None,
                 channel: ChannelAdapter | None = None):
        self.config = config
        self.client = client if client is not None else OdooClient(config)
        self.store = store or ApprovalStore(config)
        self.audit = audit or AuditLog(config.runtime_dir, secrets=[config.token or ""])
        self.policy = Policy(config)
        self.channel = channel or LocalChannel()
        self.executor = Executor(config, self.client, self.store, self.audit, self.policy)

    # ------------------------------------------------------------------
    def handle(self, cmd: CommandEnvelope) -> ResponseEnvelope:
        ctx = cmd.context
        self.audit.append("command", request_id=ctx.request_id, channel=ctx.channel, actor=ctx.actor_id, action=cmd.command, params=cmd.params)
        try:
            if cmd.command in REPORT_COMMANDS:
                return self._report(cmd)
            if cmd.command == "propose_action":
                return self._propose(cmd)
            if cmd.command == "approve_action":
                return self._approve(cmd)
            if cmd.command == "reject_action":
                return self._reject(cmd)
            if cmd.command == "execute_approved_action":
                return self._execute(cmd)
            if cmd.command == "list_pending_approvals":
                items = self.store.list_pending()
                return ResponseEnvelope(ctx.request_id, True, "list", f"{len(items)} موافقة معلّقة/جاهزة", {"approvals": items})
            if cmd.command == "get_approval":
                return ResponseEnvelope(ctx.request_id, True, "approval", "تفاصيل الموافقة", {"approval": public_view(self.store.get(cmd.params["approval_id"]))})
            return self._error(ctx, f"أمر غير معروف: {cmd.command}")
        except (OdooAccountantError, KeyError, TypeError) as exc:
            msg = f"مفتاح ناقص: {exc}" if isinstance(exc, KeyError) else str(exc)
            self.audit.append("command_error", request_id=ctx.request_id, channel=ctx.channel, actor=ctx.actor_id, action=cmd.command, result=f"{type(exc).__name__}: {msg[:300]}")
            return self._error(ctx, msg)

    def _error(self, ctx, msg) -> ResponseEnvelope:
        return ResponseEnvelope(ctx.request_id, False, "error", f"تعذّر التنفيذ: {msg}", {}, error=msg)

    # ------------------------------------------------------------------
    def _report(self, cmd) -> ResponseEnvelope:
        report = WORKFLOWS[cmd.command](self.client, **cmd.params)
        h = report["header"]
        msg = f"{report['title']} — الفترة {h['period']['from']} → {h['period']['to']} (قراءة فقط، دون أي تغيير)"
        return ResponseEnvelope(cmd.context.request_id, True, "report", msg, report)

    def _propose(self, cmd) -> ResponseEnvelope:
        ctx = cmd.context
        action = cmd.params.get("action")
        params = dict(cmd.params.get("params") or {})
        params.pop("expected", None)  # system-filled only
        req = self.policy.requirement(action)
        if not req.allowed:
            raise PolicyError(req.reason or "action not allowed")
        handler = get_handler(action)
        handler.validate(params)
        prev = handler.preview(self.client, params)
        if prev["blockers"]:
            self.audit.append("propose_blocked", request_id=ctx.request_id, channel=ctx.channel, actor=ctx.actor_id, action=action, result=prev["blockers"])
            return ResponseEnvelope(ctx.request_id, False, "plan", "لا يمكن تحضير العملية: " + "؛ ".join(prev["blockers"]), {"blockers": prev["blockers"], "preview": prev}, error="blocked")
        params["expected"] = prev["expected"]
        key = cmd.params.get("idempotency_key") or derive_idempotency_key(action, params)
        done = self.executor.idem.lookup(key)
        if done and done.get("status") in ("completed", "in_progress", "failed_review"):
            return ResponseEnvelope(ctx.request_id, False, "plan", f"العملية نُفذت/قيد التنفيذ سابقًا بنفس المفتاح ({done['status']})", {"idempotency_key": key, "prior": done}, error="duplicate")
        plan = ActionPlan(action=action, params=params, idempotency_key=key, risk=req.risk, summary=prev["summary"],
                          payload_hash=compute_payload_hash(action, params, key), targets=prev["targets"],
                          warnings=prev["warnings"], preview=prev["details"])
        rec, code = self.store.propose(plan, ctx, req)
        hint = self.channel.deliver_approval_code(ctx, rec["approval_id"], code)
        self.channel.request_approval(ctx, public_view(rec))
        self.audit.append("propose", request_id=ctx.request_id, channel=ctx.channel, actor=ctx.actor_id, action=action, approval_id=rec["approval_id"], targets=plan.targets, payload_hash=plan.payload_hash, risk=plan.risk.value)
        data = {"approval": public_view(rec), "details": prev["details"], "code_delivery": hint}
        msg = (f"خطة جاهزة ({plan.risk.value}): {plan.summary}\n"
               f"رقم الموافقة {rec['approval_id']}، بصمة الحمولة {plan.payload_hash}. لا تنفيذ قبل موافقتك الصريحة.")
        return ResponseEnvelope(ctx.request_id, True, "plan", msg, data)

    def _approve(self, cmd) -> ResponseEnvelope:
        p, ctx = cmd.params, cmd.context
        rec = self.store.approve(p["approval_id"], p.get("code", ""), ctx, p.get("payload_hash"))
        self.audit.append("approve", request_id=ctx.request_id, channel=ctx.channel, actor=ctx.actor_id, approval_id=rec["approval_id"], action=rec["action"], payload_hash=rec["payload_hash"])
        return ResponseEnvelope(ctx.request_id, True, "approval", "تمت الموافقة؛ التنفيذ يحتاج execute_approved_action بنفس الكود.", {"approval": public_view(rec)})

    def _reject(self, cmd) -> ResponseEnvelope:
        p, ctx = cmd.params, cmd.context
        rec = self.store.reject(p["approval_id"], ctx, p.get("reason", ""))
        self.audit.append("reject", request_id=ctx.request_id, channel=ctx.channel, actor=ctx.actor_id, approval_id=rec["approval_id"], action=rec["action"], result=p.get("reason", ""))
        return ResponseEnvelope(ctx.request_id, True, "approval", "تم رفض الطلب.", {"approval": public_view(rec)})

    def _execute(self, cmd) -> ResponseEnvelope:
        p, ctx = cmd.params, cmd.context
        res = self.executor.execute(p["approval_id"], p.get("code", ""), ctx, dry_run=bool(p.get("dry_run", False)), payload_hash=p.get("payload_hash"))
        return ResponseEnvelope(ctx.request_id, res.ok, "result", res.message, to_jsonable(res), error=None if res.ok else res.status)
