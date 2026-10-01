"""Append-only JSONL audit log with hash chaining and automatic redaction."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Iterable

from .models import canonical_json, to_jsonable
from .storage import ensure_dir, file_lock

_SENSITIVE_KEY = re.compile(
    r"(?i)^(code|sig|otp)$|pass|token|secret|authorization|api[-_]?key|bearer|cookie|credential|approval_code"
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/\-]+=*")
REDACTED = "[REDACTED]"


def redact(obj: Any, secrets: Iterable[str] = ()) -> Any:
    secrets = [s for s in secrets if s]
    if isinstance(obj, dict):
        return {
            k: (REDACTED if _SENSITIVE_KEY.search(str(k)) else redact(v, secrets)) for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        out = _BEARER.sub("Bearer " + REDACTED, obj)
        for s in secrets:
            out = out.replace(s, REDACTED)
        return out
    return obj


class AuditLog:
    def __init__(self, runtime_dir: Path, secrets: Iterable[str] = ()):
        self.runtime_dir = Path(runtime_dir)
        self.path = self.runtime_dir / "audit.jsonl"
        self._secrets = list(secrets)

    def _last_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        last = ""
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if not last:
            return "0" * 64
        import json

        return json.loads(last).get("hash", "0" * 64)

    def append(self, event: str, **fields: Any) -> dict:
        record = redact(to_jsonable({"ts": time.time(), "event": event, **fields}), self._secrets)
        ensure_dir(self.runtime_dir)
        with file_lock(self.runtime_dir):
            record["prev"] = self._last_hash()
            record["hash"] = hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(canonical_json(record) + "\n")
        return record

    def read_all(self) -> list:
        import json

        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def verify_chain(self) -> tuple[bool, int]:
        prev = "0" * 64
        rows = self.read_all()
        for i, rec in enumerate(rows):
            body = {k: v for k, v in rec.items() if k != "hash"}
            if rec.get("prev") != prev:
                return False, i
            if hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest() != rec.get("hash"):
                return False, i
            prev = rec["hash"]
        return True, len(rows)
