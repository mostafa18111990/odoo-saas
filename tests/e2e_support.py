"""Shared helper: speaks newline-delimited JSON-RPC to an MCP launcher like Claude Code does over stdio."""
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class McpProcess:
    """Speaks newline-delimited JSON-RPC to the launcher, like Claude Code does over stdio."""

    def __init__(self, env: dict, launcher: Path | None = None):
        launcher = launcher or ROOT / "scripts" / "run_odoo_accountant_mcp.py"
        self.p = subprocess.Popen([sys.executable, str(launcher)], cwd=str(launcher.parents[1]), env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: [self.q.put(l) for l in self.p.stdout], daemon=True).start()
        self.n = 0

    def rpc(self, method, params=None, timeout=30):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        msg = json.loads(self.q.get(timeout=timeout))
        assert msg["id"] == self.n, msg
        return msg

    def call(self, tool, **args):
        res = self.rpc("tools/call", {"name": tool, "arguments": args})["result"]
        return json.loads(res["content"][0]["text"]) | {"_is_error": res["isError"]}

    def close(self):
        self.p.stdin.close()
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
