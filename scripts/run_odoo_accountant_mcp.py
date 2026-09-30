#!/usr/bin/env python3
"""Launcher for the odoo-accountant MCP server (adds ./src to sys.path)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from odoo_accountant.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
