"""Local CLI. Mutating commands are dry-run unless --execute is passed."""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from .approvals import ApprovalStore
from .audit import AuditLog
from .config import Config
from .errors import OdooAccountantError
from .models import ChannelContext, CommandEnvelope, to_jsonable
from .service import AccountingEmployee


def _ctx() -> ChannelContext:
    return ChannelContext(channel="cli", actor_id=getpass.getuser())


def _print(obj) -> None:
    print(json.dumps(to_jsonable(obj), ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="odoo_accountant.cli", description="Odoo AI accountant (local)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def rng(sp):
        sp.add_argument("--from", dest="date_from"); sp.add_argument("--to", dest="date_to")
    s = sub.add_parser("snapshot"); rng(s); s.add_argument("--company-id", type=int)
    s = sub.add_parser("overdue"); s.add_argument("--side", choices=["out", "in"], default="out"); s.add_argument("--min-days", type=int, default=0); s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("bank-match"); rng(s); s.add_argument("--journal-id", type=int); s.add_argument("--limit", type=int, default=30)
    s = sub.add_parser("partner-quality"); rng(s)
    s = sub.add_parser("vendor-bills"); rng(s); s.add_argument("--move-id", type=int, action="append", dest="move_ids"); s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("period-close"); s.add_argument("--month")

    s = sub.add_parser("propose-action"); s.add_argument("--file", required=True); s.add_argument("--print-code", action="store_true", help="print the one-time code to this (human) terminal")
    for name in ("approve", "reject", "execute"):
        s = sub.add_parser(name); s.add_argument("approval_id"); s.add_argument("--code", default="")
        s.add_argument("--payload-hash"); s.add_argument("--reason", default="")
        s.add_argument("--execute", action="store_true", help="really apply (default is dry-run)")
    sub.add_parser("list-pending")
    s = sub.add_parser("show"); s.add_argument("approval_id")
    s = sub.add_parser("show-code"); s.add_argument("approval_id")
    sub.add_parser("verify-audit")
    return p


def run(argv: list[str], employee: AccountingEmployee | None = None, config: Config | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = config or Config.from_env()
    ctx = _ctx()

    if args.cmd == "show-code":
        try:
            print(ApprovalStore(config).read_code_file(args.approval_id))
            return 0
        except OdooAccountantError as exc:
            print(f"error: {exc}", file=sys.stderr); return 1
    if args.cmd == "verify-audit":
        ok, n = AuditLog(config.runtime_dir).verify_chain()
        _print({"ok": ok, "records": n}); return 0 if ok else 2

    emp = employee or AccountingEmployee(config)
    cmd_params: dict = {}
    name = ""
    if args.cmd == "snapshot": name, cmd_params = "accounting_snapshot", {"date_from": args.date_from, "date_to": args.date_to, "company_id": args.company_id}
    elif args.cmd == "overdue": name, cmd_params = "overdue_followup", {"side": args.side, "min_days": args.min_days, "limit": args.limit}
    elif args.cmd == "bank-match": name, cmd_params = "bank_match_suggest", {"journal_id": args.journal_id, "date_from": args.date_from, "date_to": args.date_to, "limit": args.limit}
    elif args.cmd == "partner-quality": name, cmd_params = "partner_data_quality", {"date_from": args.date_from, "date_to": args.date_to}
    elif args.cmd == "vendor-bills": name, cmd_params = "vendor_bill_review", {"move_ids": args.move_ids, "date_from": args.date_from, "date_to": args.date_to, "limit": args.limit}
    elif args.cmd == "period-close": name, cmd_params = "period_close_check", {"month": args.month}
    elif args.cmd == "propose-action":
        with open(args.file, encoding="utf-8") as fh:
            name, cmd_params = "propose_action", json.load(fh)
    elif args.cmd == "list-pending": name = "list_pending_approvals"
    elif args.cmd == "show": name, cmd_params = "get_approval", {"approval_id": args.approval_id}
    elif args.cmd in ("approve", "reject", "execute"):
        base = {"approval_id": args.approval_id}
        if args.cmd == "approve":
            if not args.execute:
                rec = ApprovalStore(config).get(args.approval_id)
                _print({"dry_run": True, "would_approve": rec["approval_id"], "status": rec["status"], "payload_hash": rec["payload_hash"], "hint": "add --execute --code CODE --payload-hash HASH"}); return 0
            name, cmd_params = "approve_action", {**base, "code": args.code, "payload_hash": args.payload_hash}
        elif args.cmd == "reject":
            if not args.execute:
                _print({"dry_run": True, "would_reject": args.approval_id, "hint": "add --execute"}); return 0
            name, cmd_params = "reject_action", {**base, "reason": args.reason}
        else:
            name, cmd_params = "execute_approved_action", {**base, "code": args.code, "payload_hash": args.payload_hash, "dry_run": not args.execute}
    cmd_params = {k: v for k, v in cmd_params.items() if v is not None} if name != "propose_action" else cmd_params
    resp = emp.handle(CommandEnvelope(name, cmd_params, ctx))
    if args.cmd == "propose-action" and args.print_code and resp.ok:
        resp.data["approval_code"] = ApprovalStore(config).read_code_file(resp.data["approval"]["approval_id"])
    _print(resp)
    return 0 if resp.ok else 1


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
