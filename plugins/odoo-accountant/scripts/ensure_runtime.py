#!/usr/bin/env python3
"""Make sure the pinned runtime requirements of odoo-accountant are present in THIS interpreter.

Used as a Claude Code SessionStart hook (fresh containers start without pip-installed extras) and as a doctor:
    python3 scripts/ensure_runtime.py            # install missing pinned packages (never fails the session)
    python3 scripts/ensure_runtime.py --check    # report only; exit 1 if something required is missing

Only fixed, pinned requirements below are ever installed (no shell, no user-supplied arguments).
"""
import importlib.metadata as md
import json
import os
import subprocess
import sys
from pathlib import Path

PINS = {"xlrd": "2.0.1"}          # legacy .xls statements; keep in sync with pyproject [project.optional-dependencies].xls
ROOT = Path(__file__).resolve().parents[1]


def installed(name: str):
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


def status() -> dict:
    deps = {n: {"pinned": v, "installed": installed(n), "ok": installed(n) == v} for n, v in PINS.items()}
    sys.path.insert(0, str(ROOT / "src"))
    from odoo_accountant.config import Config
    cfg = Config.from_env()
    dirs = {}
    for label, p in (("output_dir", cfg.output_dir), ("runtime_dir", cfg.runtime_dir)):
        probe = Path(p)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        dirs[label] = {"path": str(p), "writable": os.access(probe, os.W_OK)}
    return {
        "python": sys.executable,
        "dependencies": deps,
        "odoo_env": {k: bool(os.environ.get(k)) for k in ("ODOO_URL", "ODOO_DB", "ODOO_LOGIN")},
        "allowed_input_dirs": [{"path": str(p), "exists": Path(p).expanduser().exists()} for p in cfg.input_dirs],
        "dirs": dirs,
        "all_required_present": all(d["ok"] for d in deps.values()),
    }


def install_missing() -> None:
    for name, ver in PINS.items():
        if installed(name) == ver:
            continue
        for extra in ([], ["--user"]):
            cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *extra, f"{name}=={ver}"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            if r.returncode == 0:
                break
        else:
            print(f"odoo-accountant: could not install {name}=={ver} ({r.stderr.strip()[-200:]}). "
                  f"Legacy .xls files will be refused until it is installed.", file=sys.stderr)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
    if not check:
        try:
            install_missing()
        except Exception as exc:  # noqa: BLE001 - a session hook must never break the session
            print(f"odoo-accountant: runtime setup skipped ({type(exc).__name__})", file=sys.stderr)
    st = status()
    if check:
        print(json.dumps(st, indent=2, ensure_ascii=False))
        return 0 if st["all_required_present"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
