"""Core data types shared by every layer."""
from __future__ import annotations

import enum
import hashlib
import json
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any


class Risk(str, enum.Enum):
    READ = "READ"
    DRAFT_WRITE = "DRAFT_WRITE"
    FINANCIAL_FINAL = "FINANCIAL_FINAL"
    DESTRUCTIVE = "DESTRUCTIVE"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def compute_payload_hash(action: str, params: dict, idempotency_key: str) -> str:
    body = canonical_json({"action": action, "params": params, "idempotency_key": idempotency_key})
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, enum.Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj) if not f.name.startswith("_")}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    return obj


_INTERNAL_KEY = object()


@dataclass(frozen=True)
class ExecutionAuthorization:
    """Proof that an approval was validly consumed. Only approvals.py can mint one."""

    approval_id: str
    payload_hash: str
    action: str
    _key: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._key is not _INTERNAL_KEY:
            raise PermissionError("ExecutionAuthorization can only be issued by the approval store")


@dataclass
class ChannelContext:
    channel: str = "claude_code"
    actor_id: str = "local"
    actor_name: str = ""
    chat_id: str = ""
    request_id: str = field(default_factory=lambda: "req_" + uuid.uuid4().hex[:12])

    @property
    def actor_key(self) -> str:
        return f"{self.channel}:{self.actor_id}"


@dataclass
class CommandEnvelope:
    command: str
    params: dict = field(default_factory=dict)
    context: ChannelContext = field(default_factory=ChannelContext)


@dataclass
class ActionPlan:
    action: str
    params: dict
    idempotency_key: str
    risk: Risk
    summary: str
    payload_hash: str
    targets: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    preview: dict = field(default_factory=dict)


@dataclass
class ExecutionResult:
    ok: bool
    action: str
    status: str  # executed | executed_unverified | dry_run | failed | blocked | duplicate
    message: str
    data: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)


@dataclass
class ResponseEnvelope:
    request_id: str
    ok: bool
    kind: str  # report | plan | approval | result | list | error
    message: str
    data: dict = field(default_factory=dict)
    error: str | None = None
