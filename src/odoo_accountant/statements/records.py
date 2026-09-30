from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawTable:
    rows: list
    meta: dict = field(default_factory=dict)  # encoding, delimiter, sheet, format, warnings


@dataclass
class NormalizedLine:
    index: int  # 1-based position among data rows
    date: str  # ISO
    amount: float
    payment_ref: str
    partner_name: str | None = None
    balance: float | None = None
    currency: str | None = None
    source_row: int = 0


def issue(row: int | None, code: str, message_ar: str, severity: str = "error", field_name: str | None = None) -> dict:
    return {"row": row, "field": field_name, "code": code, "severity": severity, "message": message_ar}
