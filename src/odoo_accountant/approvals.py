"""Approval gate: propose -> approve -> consume, bound to an exact payload hash."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Callable

from .config import Config
from .errors import ApprovalError
from .models import (
    _INTERNAL_KEY,
    ActionPlan,
    ApprovalStatus,
    ChannelContext,
    ExecutionAuthorization,
    canonical_json,
    compute_payload_hash,
    to_jsonable,
)
from .policy import Requirement
from .storage import atomic_write, ensure_dir, file_lock

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_CODE_FAILURES = 5


def _hash_code(salt: str, code: str) -> str:
    return hashlib.sha256((salt + code.strip().upper()).encode()).hexdigest()


class ApprovalStore:
    def __init__(self, config: Config, clock: Callable[[], float] = time.time):
        self.config = config
        self.clock = clock
        self.dir = Path(config.runtime_dir) / "approvals"
        self.codes_dir = Path(config.runtime_dir) / "pending_codes"
        self.key_path = Path(config.runtime_dir) / "secret.key"

    # ---- storage ----------------------------------------------------
    def _key(self) -> bytes:
        if not self.key_path.exists():
            ensure_dir(self.key_path.parent)
            atomic_write(self.key_path, secrets.token_hex(32))
        return self.key_path.read_text().strip().encode()

    def _sign(self, record: dict) -> str:
        body = {k: v for k, v in record.items() if k != "sig"}
        return hmac.new(self._key(), canonical_json(body).encode(), hashlib.sha256).hexdigest()

    def _path(self, approval_id: str) -> Path:
        if not approval_id.startswith("apr_") or not approval_id[4:].isalnum():
            raise ApprovalError("Invalid approval id")
        return self.dir / f"{approval_id}.json"

    def _save(self, record: dict) -> None:
        record["sig"] = self._sign(record)
        atomic_write(self._path(record["approval_id"]), json.dumps(record, ensure_ascii=False, indent=1))

    def _load(self, approval_id: str) -> dict:
        path = self._path(approval_id)
        if not path.exists():
            raise ApprovalError("Approval not found")
        record = json.loads(path.read_text(encoding="utf-8"))
        if not hmac.compare_digest(record.get("sig", ""), self._sign(record)):
            raise ApprovalError("Approval record failed integrity check (tampered)")
        expected = compute_payload_hash(record["action"], record["params"], record["idempotency_key"])
        if not hmac.compare_digest(expected, record["payload_hash"]):
            raise ApprovalError("Payload hash mismatch (payload changed after proposal)")
        if record["status"] in (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value):
            if self.clock() > record["expires_at"]:
                record["status"] = ApprovalStatus.EXPIRED.value
                self._save(record)
                self._drop_code(approval_id)
        return record

    def _drop_code(self, approval_id: str) -> None:
        try:
            (self.codes_dir / f"{approval_id}.code").unlink()
        except OSError:
            pass

    # ---- lifecycle --------------------------------------------------
    def propose(self, plan: ActionPlan, ctx: ChannelContext, req: Requirement) -> tuple[dict, str]:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        salt = secrets.token_hex(8)
        now = self.clock()
        record = {
            "approval_id": "apr_" + secrets.token_hex(6),
            "action": plan.action,
            "params": plan.params,
            "idempotency_key": plan.idempotency_key,
            "payload_hash": plan.payload_hash,
            "risk": plan.risk.value,
            "summary": plan.summary,
            "targets": to_jsonable(plan.targets),
            "warnings": list(plan.warnings),
            "explicit_payload_hash": req.explicit_payload_hash,
            "status": ApprovalStatus.PENDING.value,
            "code_salt": salt,
            "code_hash": _hash_code(salt, code),
            "failures": 0,
            "created_at": now,
            "expires_at": now + req.ttl_seconds,
            "proposer": {"channel": ctx.channel, "actor": ctx.actor_id},
            "approver": None,
            "approved_at": None,
            "consumed_at": None,
        }
        with file_lock(self.config.runtime_dir):
            self._save(record)
            ensure_dir(self.codes_dir)
            atomic_write(self.codes_dir / f"{record['approval_id']}.code", code)
        return record, code

    def get(self, approval_id: str) -> dict:
        with file_lock(self.config.runtime_dir):
            return self._load(approval_id)

    def read_code_file(self, approval_id: str) -> str:
        path = self.codes_dir / f"{approval_id}.code"
        if not path.exists():
            raise ApprovalError("No pending code for this approval (expired, consumed or already delivered)")
        return path.read_text().strip()

    def _check_code(self, record: dict, code: str) -> None:
        ok = hmac.compare_digest(record["code_hash"], _hash_code(record["code_salt"], code or ""))
        if not ok:
            record["failures"] = record.get("failures", 0) + 1
            if record["failures"] >= MAX_CODE_FAILURES:
                record["status"] = ApprovalStatus.REJECTED.value
                self._drop_code(record["approval_id"])
            self._save(record)
            raise ApprovalError("Invalid approval code")

    def approve(self, approval_id: str, code: str, approver: ChannelContext, payload_hash: str | None = None) -> dict:
        if not self.config.is_approver(approver.channel, approver.actor_id):
            raise ApprovalError("Actor is not an allowed approver")
        with file_lock(self.config.runtime_dir):
            rec = self._load(approval_id)
            if rec["status"] != ApprovalStatus.PENDING.value:
                raise ApprovalError(f"Approval is {rec['status']}, cannot approve")
            self._check_code(rec, code)
            if rec["explicit_payload_hash"]:
                if not payload_hash or not hmac.compare_digest(payload_hash.strip().lower(), rec["payload_hash"]):
                    raise ApprovalError("Explicit payload_hash required and must match this proposal exactly")
            elif payload_hash and not hmac.compare_digest(payload_hash.strip().lower(), rec["payload_hash"]):
                raise ApprovalError("payload_hash does not match this proposal")
            rec["status"] = ApprovalStatus.APPROVED.value
            rec["approver"] = {"channel": approver.channel, "actor": approver.actor_id}
            rec["approved_at"] = self.clock()
            self._save(rec)
            return rec

    def reject(self, approval_id: str, rejecter: ChannelContext, reason: str = "") -> dict:
        with file_lock(self.config.runtime_dir):
            rec = self._load(approval_id)
            if rec["status"] not in (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value):
                raise ApprovalError(f"Approval is {rec['status']}, cannot reject")
            rec["status"] = ApprovalStatus.REJECTED.value
            rec["rejected_by"] = {"channel": rejecter.channel, "actor": rejecter.actor_id}
            rec["reject_reason"] = reason[:300]
            self._save(rec)
            self._drop_code(approval_id)
            return rec

    def check_executable(self, approval_id: str, code: str) -> dict:
        """Read-only validation used by dry-run and by execute before consuming."""
        with file_lock(self.config.runtime_dir):
            rec = self._load(approval_id)
            if rec["status"] != ApprovalStatus.APPROVED.value:
                raise ApprovalError(f"Approval is {rec['status']}, not executable")
            self._check_code(rec, code)
            return rec

    def authorize_execution(self, approval_id: str, code: str, payload_hash: str | None = None) -> ExecutionAuthorization:
        """Atomically consume the approval (single use) and mint the authorization."""
        with file_lock(self.config.runtime_dir):
            rec = self._load(approval_id)
            if rec["status"] == ApprovalStatus.CONSUMED.value:
                raise ApprovalError("Approval already used (replay blocked)")
            if rec["status"] != ApprovalStatus.APPROVED.value:
                raise ApprovalError(f"Approval is {rec['status']}, not executable")
            self._check_code(rec, code)
            if payload_hash and not hmac.compare_digest(payload_hash.strip().lower(), rec["payload_hash"]):
                raise ApprovalError("payload_hash does not match the approved payload")
            rec["status"] = ApprovalStatus.CONSUMED.value
            rec["consumed_at"] = self.clock()
            self._save(rec)
            self._drop_code(approval_id)
            return ExecutionAuthorization(approval_id, rec["payload_hash"], rec["action"], _key=_INTERNAL_KEY)

    def list_pending(self) -> list:
        out = []
        if not self.dir.exists():
            return out
        for path in sorted(self.dir.glob("apr_*.json")):
            try:
                rec = self.get(path.stem)
            except ApprovalError:
                out.append({"approval_id": path.stem, "status": "invalid"})
                continue
            if rec["status"] in (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value):
                out.append(public_view(rec))
        return out


def public_view(rec: dict) -> dict:
    """Safe view of a record: never includes params secrets/code material."""
    return {
        "approval_id": rec["approval_id"],
        "action": rec["action"],
        "risk": rec["risk"],
        "status": rec["status"],
        "summary": rec["summary"],
        "targets": rec["targets"],
        "warnings": rec.get("warnings", []),
        "payload_hash": rec["payload_hash"],
        "requires_explicit_payload_hash": rec["explicit_payload_hash"],
        "expires_at": rec["expires_at"],
        "proposer": rec["proposer"],
        "approver": rec.get("approver"),
    }
