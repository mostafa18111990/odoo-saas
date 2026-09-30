"""Safe access to local source files: only allow-listed directories, regular files, size-capped, read-only."""
from __future__ import annotations

from pathlib import Path

from ..errors import StatementError
from .limits import MAX_BYTES


def resolve_source_path(path: str, config) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise StatementError("path_invalid", "source_path يجب أن يكون مسار ملف نصيًا.")
    try:
        real = Path(path).expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        raise StatementError("path_missing", "الملف غير موجود أو لا يمكن الوصول إليه.") from None
    if not real.is_file():
        raise StatementError("path_not_file", "المسار ليس ملفًا عاديًا.")
    runtime = Path(config.runtime_dir).resolve()
    if real == runtime or runtime in real.parents or real.name.startswith(".env") or real.suffix in (".key", ".pem"):
        raise StatementError("path_denied", "هذا المسار محجوب (ملفات التشغيل والأسرار).")
    roots = []
    for r in config.input_dirs:
        try:
            roots.append(Path(r).expanduser().resolve())
        except OSError:
            continue
    if not any(real == r or r in real.parents for r in roots):
        raise StatementError("path_outside", "الملف خارج المجلدات المسموح بالقراءة منها (inputs/ أو uploads أو ODOO_ACCOUNTANT_INPUT_DIRS).")
    if real.stat().st_size > MAX_BYTES:
        raise StatementError("file_too_big", f"حجم الملف يتجاوز الحد المسموح ({MAX_BYTES // (1024 * 1024)} ميغابايت).")
    if real.stat().st_size == 0:
        raise StatementError("empty_file", "الملف فارغ.")
    return real


def read_source_bytes(real: Path) -> bytes:
    with open(real, "rb") as fh:          # read-only; the source is never written, renamed or deleted
        data = fh.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise StatementError("file_too_big", f"حجم الملف يتجاوز الحد المسموح ({MAX_BYTES // (1024 * 1024)} ميغابايت).")
    return data
