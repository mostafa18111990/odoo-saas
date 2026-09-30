"""Per-bank mapping profiles: explicit, validated, saved as JSON (no secrets)."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..errors import StatementError
from ..storage import atomic_write

PROFILES_DIR = Path(__file__).resolve().parents[3] / "statement_profiles"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
_COLUMN_KEYS = {"date", "amount", "debit", "credit", "payment_ref", "partner_name", "balance", "currency"}
_DATE_FORMATS_OK = re.compile(r"^(%[dmYyHMSbBj]|[-/.\s:,])+$")


@dataclass
class MappingProfile:
    name: str
    bank: str = ""
    format: str = "csv"  # csv | xlsx | xls (xlsx/xls profiles are interchangeable: both are spreadsheets)
    encoding: str | None = None
    delimiter: str | None = None
    sheet: str | int | None = None
    header_row: int = 1  # 1-based row holding column titles
    skip_footer_rows: int = 0
    columns: dict = field(default_factory=dict)  # role -> header title | "#N" (1-based index) | [titles] for payment_ref
    date_format: str | None = None  # strptime; required for text dates (XLSX real dates exempt)
    decimal_separator: str = "."
    thousands_separator: str = ""
    invert_amount: bool = False
    drcr_suffix: bool = False
    default_currency: str | None = None
    notes: str = ""

    def validate(self) -> "MappingProfile":
        def bad(code, msg):
            raise StatementError(code, "profile غير صالح: " + msg)
        if not _NAME.match(self.name or ""):
            bad("profile_name", "الاسم يجب أن يكون a-z و0-9 و- و_ (2..40 حرفًا).")
        if self.format not in ("csv", "xlsx", "xls"):
            bad("profile_format", "format يجب أن يكون csv أو xlsx أو xls.")
        extra = set(self.columns) - _COLUMN_KEYS
        if extra:
            bad("profile_columns", "أدوار أعمدة غير معروفة: " + ", ".join(sorted(extra)))
        c = self.columns
        if "date" not in c:
            bad("profile_columns", "يجب تحديد عمود date.")
        if "amount" in c and ("debit" in c or "credit" in c):
            bad("profile_columns", "حدّد amount أو (debit وcredit) وليس كليهما.")
        if "amount" not in c and not ("debit" in c and "credit" in c):
            bad("profile_columns", "يجب تحديد amount أو debit وcredit معًا.")
        if "payment_ref" not in c:
            bad("profile_columns", "يجب تحديد عمود/أعمدة payment_ref (البيان).")
        if self.decimal_separator not in (".", ","):
            bad("profile_number", "decimal_separator يجب أن يكون . أو ,")
        if self.thousands_separator not in ("", ",", ".", " ", "'"):
            bad("profile_number", "thousands_separator غير مدعوم.")
        if self.thousands_separator == self.decimal_separator:
            bad("profile_number", "الفاصل العشري وفاصل الآلاف متطابقان.")
        if self.date_format is not None and not _DATE_FORMATS_OK.match(self.date_format):
            bad("profile_date", "date_format غير صالح (مثال: %d/%m/%Y).")
        if not isinstance(self.header_row, int) or self.header_row < 1:
            bad("profile_header", "header_row يجب أن يكون عددًا صحيحًا ≥ 1.")
        if not isinstance(self.skip_footer_rows, int) or self.skip_footer_rows < 0:
            bad("profile_footer", "skip_footer_rows يجب أن يكون ≥ 0.")
        if self.default_currency and not re.match(r"^[A-Z]{3}$", self.default_currency):
            bad("profile_currency", "default_currency يجب أن يكون رمزًا من 3 أحرف كبيرة.")
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MappingProfile":
        if not isinstance(d, dict):
            raise StatementError("profile_invalid", "profile يجب أن يكون كائنًا (object).")
        known = set(cls.__dataclass_fields__)
        extra = set(d) - known
        if extra:
            raise StatementError("profile_invalid", "profile غير صالح: مفاتيح غير معروفة: " + ", ".join(sorted(extra)))
        return cls(**d).validate()


def save_profile(profile: MappingProfile, directory: Path | None = None, overwrite: bool = False) -> Path:
    profile.validate()
    d = Path(directory or PROFILES_DIR)
    path = d / f"{profile.name}.json"
    if path.exists() and not overwrite:
        raise StatementError("profile_exists", f"الـ profile «{profile.name}» موجود. استخدم overwrite للاستبدال الصريح.")
    atomic_write(path, json.dumps(profile.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return path


def load_profile(name: str, directory: Path | None = None) -> MappingProfile:
    if not _NAME.match(name or ""):
        raise StatementError("profile_name", "اسم profile غير صالح.")
    path = Path(directory or PROFILES_DIR) / f"{name}.json"
    if not path.exists():
        raise StatementError("profile_missing", f"الـ profile «{name}» غير موجود. المتاح: " + (", ".join(list_profiles(directory)) or "لا شيء"))
    return MappingProfile.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_profiles(directory: Path | None = None) -> list:
    d = Path(directory or PROFILES_DIR)
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def resolve_profile(spec, directory: Path | None = None) -> MappingProfile | None:
    if spec in (None, "", {}):
        return None
    if isinstance(spec, str):
        return load_profile(spec, directory)
    if isinstance(spec, dict):
        return MappingProfile.from_dict(spec)
    raise StatementError("profile_invalid", "profile يجب أن يكون اسمًا محفوظًا أو كائنًا.")


def formats_compatible(profile_format: str, file_format: str) -> bool:
    sheets = {"xlsx", "xls"}
    return profile_format == file_format or (profile_format in sheets and file_format in sheets)
