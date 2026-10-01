"""Configuration from the environment. Never prints or stores secrets."""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

_ROOT = Path(__file__).resolve().parents[2]
# Plugin mode: this tree is a copy inside Claude Code's plugin cache, which is REPLACED on every update.
# Approvals, audit log, generated files and saved profiles must live in a persistent per-user directory instead.
PLUGIN_MODE = (_ROOT / ".claude-plugin" / "plugin.json").exists()
DATA_HOME = Path(os.environ.get("ODOO_ACCOUNTANT_HOME") or (Path.home() / ".odoo-accountant"))
_DEFAULT_RUNTIME = DATA_HOME / "runtime" if PLUGIN_MODE else _ROOT / ".runtime"
_DEFAULT_OUTPUT = DATA_HOME / "outputs" / "statements" if PLUGIN_MODE else _ROOT / "outputs" / "statements"
_DEFAULT_INPUTS = DATA_HOME / "inputs" if PLUGIN_MODE else _ROOT / "inputs"


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    url: str = ""
    db: str = ""
    login: str = ""
    token: str | None = field(default=None, repr=False)
    runtime_dir: Path = _DEFAULT_RUNTIME
    timeout: float = 30.0
    ca_bundle: str | None = None
    ttl_draft_write: int = 1800
    ttl_financial_final: int = 600
    approvers: tuple = ("cli:*", "claude_code:*")
    allow_destructive: bool = False
    output_dir: Path = _DEFAULT_OUTPUT
    input_dirs: tuple = ()

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = os.environ if env is None else env
        ca = env.get("SSL_CERT_FILE") or env.get("REQUESTS_CA_BUNDLE") or env.get("CURL_CA_BUNDLE")
        if ca and not Path(ca).exists():
            ca = None
        approvers = tuple(
            p.strip() for p in env.get("ODOO_ACCOUNTANT_APPROVERS", "cli:*,claude_code:*").split(",") if p.strip()
        )
        runtime = env.get("ODOO_ACCOUNTANT_RUNTIME_DIR")
        output = Path(env["ODOO_ACCOUNTANT_OUTPUT_DIR"]) if env.get("ODOO_ACCOUNTANT_OUTPUT_DIR") else _DEFAULT_OUTPUT
        extra_in = tuple(Path(x) for x in env.get("ODOO_ACCOUNTANT_INPUT_DIRS", "").split(os.pathsep) if x.strip())
        inputs = (_DEFAULT_INPUTS, Path.home() / ".claude" / "uploads", output) + extra_in
        return cls(
            url=(env.get("ODOO_URL") or "").rstrip("/"),
            db=env.get("ODOO_DB") or "",
            login=env.get("ODOO_LOGIN") or "",
            token=env.get("ODOO_API_KEY") or env.get("ODOO_TOKEN") or None,
            runtime_dir=Path(runtime) if runtime else _DEFAULT_RUNTIME,
            ca_bundle=ca,
            approvers=approvers or ("cli:*", "claude_code:*"),
            allow_destructive=_flag(env.get("ODOO_ACCOUNTANT_ALLOW_DESTRUCTIVE")),
            output_dir=output,
            input_dirs=inputs,
        )

    def require_odoo(self) -> None:
        missing = [n for n, v in (("ODOO_URL", self.url), ("ODOO_DB", self.db)) if not v]
        if missing:
            raise ConfigError("Missing environment variables: " + ", ".join(missing))

    def is_approver(self, channel: str, actor_id: str) -> bool:
        key = f"{channel}:{actor_id}"
        return any(fnmatch.fnmatchcase(key, pat) for pat in self.approvers)

    def public_dict(self) -> dict:
        return {
            "url": self.url,
            "db": self.db,
            "login": self.login,
            "auth": "api-key" if self.token else "platform-injected",
            "runtime_dir": str(self.runtime_dir),
            "output_dir": str(self.output_dir),
        }
