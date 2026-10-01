"""Fingerprints and duplicate classification (file-level, line-level, vs Odoo)."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path

from ..storage import atomic_write, file_lock
from .limits import DUP_WINDOW_DAYS

ID_PREFIX = "OA-"


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rows_sha256(rows: list) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def norm_ref(s: str) -> str:
    return " ".join(str(s or "").casefold().split())


def line_fp(journal_id: int, date: str, amount: float, currency: str, payment_ref: str, occurrence: int, dp: int = 2) -> str:
    key = f"{journal_id}|{date}|{amount:.{dp}f}|{currency}|{norm_ref(payment_ref)}|{occurrence}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def assign_fingerprints(lines, journal_id: int, currency: str, dp: int = 2) -> list:
    """Occurrence-aware: two identical genuine rows get distinct fingerprints."""
    counts: dict = {}
    out = []
    for l in lines:
        k = (l.date, f"{l.amount:.{dp}f}", norm_ref(l.payment_ref))
        counts[k] = counts.get(k, 0) + 1
        out.append(line_fp(journal_id, l.date, l.amount, currency, l.payment_ref, counts[k], dp))
    return out


def unique_import_id(fp: str) -> str:
    return ID_PREFIX + fp[:40]


def verify_fp(journal_id: int, line: dict, currency: str, dp: int = 2, max_occ: int = 200) -> bool:
    return any(line_fp(journal_id, line["date"], line["amount"], currency, line["payment_ref"], o, dp) == line["fp"] for o in range(1, max_occ + 1))


def classify_against_existing(lines, fps, existing, dp=2, window_days=DUP_WINDOW_DAYS):
    """Return per-line dicts: status in new | exact | possible (+ reason, matched existing id)."""
    pool = [dict(e, _used=False) for e in existing]
    by_uid = {e.get("unique_import_id"): e for e in pool if e.get("unique_import_id")}
    res = []
    for l, fp in zip(lines, fps):
        nref = norm_ref(l.payment_ref)
        m = by_uid.get(unique_import_id(fp))
        if m and not m["_used"]:
            m["_used"] = True
            res.append({"index": l.index, "status": "exact", "reason": "same_import_id", "existing_id": m["id"]}); continue
        exact = next((e for e in pool if not e["_used"] and e["date"] == l.date and abs(e["amount"] - l.amount) < 0.5 * 10 ** -dp and norm_ref(e.get("payment_ref")) == nref), None)
        if exact:
            exact["_used"] = True
            res.append({"index": l.index, "status": "exact", "reason": "same_date_amount_ref", "existing_id": exact["id"]}); continue
        d0 = _dt.date.fromisoformat(l.date)
        same_da = next((e for e in pool if not e["_used"] and e["date"] == l.date and abs(e["amount"] - l.amount) < 0.5 * 10 ** -dp), None)
        if same_da:
            res.append({"index": l.index, "status": "possible", "reason": "same_date_amount_different_ref", "existing_id": same_da["id"], "existing_ref": (same_da.get("payment_ref") or "")[:80]}); continue
        near = next((e for e in pool if not e["_used"] and abs(e["amount"] - l.amount) < 0.5 * 10 ** -dp and norm_ref(e.get("payment_ref")) == nref and abs((_dt.date.fromisoformat(e["date"]) - d0).days) <= window_days), None)
        if near:
            res.append({"index": l.index, "status": "possible", "reason": "same_amount_ref_nearby_date", "existing_id": near["id"], "existing_date": near["date"]}); continue
        res.append({"index": l.index, "status": "new"})
    return res


class ImportRegistry:
    """Local record of file hashes already proposed/imported (.runtime/statement_imports.json)."""

    def __init__(self, runtime_dir):
        self.dir = Path(runtime_dir)
        self.path = self.dir / "statement_imports.json"

    def _all(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def status(self, sha: str):
        with file_lock(self.dir):
            return self._all().get(sha)

    def mark(self, sha: str, status: str, **extra):
        import time
        with file_lock(self.dir):
            data = self._all()
            data[sha] = {**data.get(sha, {}), "status": status, "ts": time.time(), **extra}
            atomic_write(self.path, json.dumps(data, indent=1))
