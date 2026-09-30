#!/usr/bin/env python3
"""Launcher for the odoo-accountant CLI (adds ./src to sys.path; no `pip install` needed).

Example (what the human runs to get the one-time approval code):
    python3 scripts/odoo_accountant_cli.py show-code apr_xxxxxxxxxxxx
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from odoo_accountant.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
