"""The installable plugin plugins/odoo-accountant/ is GENERATED from the sources: guard drift, shape and real behaviour."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from odoo_accountant import __version__
from odoo_accountant.mcp_server import TOOLS
from tests.e2e_support import McpProcess  # noqa: F401  (re-export for readability)
from tests.mock_odoo_http import MockOdoo
from tests.fakes import seed_statement_tables
from tests.statement_fixtures import b64, profile_std

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "odoo-accountant"
PREFIX = "mcp__plugin_odoo-accountant_odoo-accountant__"
CLAUDE = shutil.which("claude")
TARGET = {"company_id": 1, "journal_id": 14, "bank_account_id": 9, "currency": "SAR"}


class PluginShapeTests(unittest.TestCase):
    def test_committed_plugin_is_in_sync_with_the_sources(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_plugin.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_manifests(self):
        man = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual((man["name"], man["version"]), ("odoo-accountant", __version__))
        mk = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        entry = next(p for p in mk["plugins"] if p["name"] == man["name"])
        self.assertTrue((ROOT / entry["source"] / ".claude-plugin" / "plugin.json").exists())
        self.assertEqual(mk["name"], "odoo-saas")

    def test_mcp_and_hooks_use_plugin_root_and_carry_no_secrets(self):
        mcp = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["odoo-accountant"]
        self.assertEqual(mcp["command"], "python3")
        self.assertTrue(mcp["args"][0].startswith("${CLAUDE_PLUGIN_ROOT}/scripts/")); self.assertTrue((PLUGIN / "scripts" / "run_odoo_accountant_mcp.py").exists())
        self.assertTrue(all(re.fullmatch(r"\$\{[A-Z_]+(:-)?\}", v) for v in mcp["env"].values()), mcp["env"])
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertEqual(hooks, 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ensure_runtime.py"')

    def test_agent_is_plugin_safe_and_fully_tooled(self):
        text = (PLUGIN / "agents" / "odoo-accountant.md").read_text()
        front = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
        for forbidden in ("permissionMode", "mcpServers", "hooks:"):
            self.assertNotIn(forbidden, front)               # not allowed for plugin-shipped agents
        tools = next(l for l in front.splitlines() if l.startswith("tools:"))
        for name in TOOLS:
            self.assertIn(PREFIX + name, tools, name)
        self.assertNotIn("mcp__odoo-accountant__", front)    # project-style names would match nothing in a plugin
        self.assertNotIn("Bash", tools)
        self.assertIn("- odoo-accountant:odoo-accountant", front)   # namespaced skill preload
        skill = (PLUGIN / "skills" / "odoo-accountant" / "SKILL.md").read_text()
        self.assertIn("وضع الإضافة (plugin)", skill); self.assertIn("~/.odoo-accountant/", skill)

    def test_settings_example_matches_the_real_tool_names(self):
        perms = json.loads((PLUGIN / "settings.example.json").read_text())["permissions"]
        names = {PREFIX + t for t in TOOLS}
        self.assertEqual(set(perms["allow"]) | set(perms["ask"]), names)
        self.assertEqual(set(perms["ask"]), {PREFIX + t for t in ("approve_action", "reject_action", "execute_approved_action")})

    def test_nothing_private_or_generated_is_shipped(self):
        tracked = subprocess.run(["git", "ls-files", "plugins/"], cwd=str(ROOT), capture_output=True, text=True).stdout.split()
        files = [ROOT / t for t in tracked] or [p for p in PLUGIN.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        self.assertTrue(files)
        bad = [str(p.relative_to(PLUGIN)) for p in files if PLUGIN in p.parents and any(x in p.relative_to(PLUGIN).parts for x in ("tests", ".runtime", "__pycache__", "outputs", "inputs")) or p.name.startswith(".env") or p.suffix in (".pyc", ".key", ".pem")]
        self.assertEqual(bad, [])
        blob = "".join(p.read_text(errors="ignore") for p in files if p.suffix in (".py", ".json", ".md"))
        self.assertNotRegex(blob, r"(?i)bearer [a-z0-9]{12,}")
        self.assertNotIn("EDAATTHIQAH", blob)


@unittest.skipUnless(CLAUDE, "claude CLI not installed")
class ClaudeValidatorTests(unittest.TestCase):
    def run_validate(self, path, *flags):
        return subprocess.run([CLAUDE, "plugin", "validate", str(path), *flags], capture_output=True, text=True, timeout=120)

    def test_plugin_and_marketplace_validate(self):
        for path in (PLUGIN, ROOT, PLUGIN / "agents", PLUGIN / "skills"):
            for flags in ((), ("--strict",)):
                r = self.run_validate(path, *flags)
                self.assertEqual(r.returncode, 0, f"{path} {flags}: {r.stdout}{r.stderr}")


class PluginRuntimeTests(unittest.TestCase):
    """Run the generated copy exactly as an installed plugin runs: from its own directory, state in the user's home."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oa_plugin_"))
        self.home = self.tmp / "home"; self.home.mkdir()
        self.data = self.home / ".odoo-accountant"
        self.mock = MockOdoo(seed_statement_tables()).__enter__()
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home), "ODOO_URL": self.mock.url, "ODOO_DB": "e2edb", "ODOO_LOGIN": "e2e",
                    "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1"}   # NOTE: no runtime/output dir overrides
        self.before = sorted(str(p.relative_to(PLUGIN)) for p in PLUGIN.rglob("*") if "__pycache__" not in p.parts)

    def tearDown(self):
        self.mock.__exit__()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, cwd="/"):
        return subprocess.run([sys.executable, str(PLUGIN / "scripts" / "odoo_accountant_cli.py"), *args], cwd=cwd, env=self.env, capture_output=True, text=True, timeout=60)

    def test_end_to_end_from_the_plugin_directory_with_persistent_user_state(self):
        mcp = McpProcess(self.env, PLUGIN / "scripts" / "run_odoo_accountant_mcp.py")
        try:
            init = mcp.rpc("initialize", {"protocolVersion": "2024-11-05"})["result"]["serverInfo"]
            self.assertEqual((init["name"], init["version"]), ("odoo-accountant", __version__))
            self.assertEqual(len(mcp.rpc("tools/list")["result"]["tools"]), 14)
            csv = 'Date,Description,Amount\n15/09/2026,"حوالة واردة\n   مرجع SYN1",7452.00\n'
            n = mcp.call("normalize_statement_file", content_base64=b64(csv), filename="up.csv")
            self.assertTrue(n["ok"], n["message"])
            out = Path(n["data"]["output"]["path"])
            self.assertEqual(out.parent, self.data / "outputs" / "statements")        # persistent, outside the plugin cache
            pr = mcp.call("propose_statement_import", source_path=str(out), **TARGET)
            self.assertTrue(pr["ok"], pr["message"])
            appr = pr["data"]["approval"]
            self.assertTrue((self.data / "runtime" / "approvals" / f"{appr['approval_id']}.json").exists())
            hint = pr["data"]["code_delivery"]
            m = re.search(r"python3 (\S+odoo_accountant_cli\.py) show-code (apr_\w+)", hint)
            self.assertIsNotNone(m, hint); self.assertEqual(Path(m.group(1)), PLUGIN / "scripts" / "odoo_accountant_cli.py")
            code = self.cli("show-code", m.group(2)).stdout.strip()                    # the hint is runnable from any cwd, no flags/env
            self.assertRegex(code, r"^[A-Z2-9]{8}$")
            self.assertTrue(mcp.call("approve_action", approval_id=appr["approval_id"], code=code, payload_hash=appr["payload_hash"])["ok"])
            ex = mcp.call("execute_approved_action", approval_id=appr["approval_id"], code=code)
            self.assertTrue(ex["ok"], ex["message"])
            self.assertEqual([(e["model"], e["method"]) for e in self.mock.mutations], [("account.bank.statement.line", "create")])
            self.assertEqual(self.mock.fake.tables["account.bank.statement.line"][0]["payment_ref"], "حوالة واردة مرجع SYN1")
            self.assertTrue((self.data / "runtime" / "audit.jsonl").exists())
        finally:
            mcp.close()
        after = sorted(str(p.relative_to(PLUGIN)) for p in PLUGIN.rglob("*") if "__pycache__" not in p.parts)
        self.assertEqual(after, self.before)                  # the (replaceable) plugin tree was never written to

    def test_profiles_are_saved_in_user_home_and_bundled_examples_stay_visible(self):
        pf = self.tmp / "p.json"; pf.write_text(json.dumps(profile_std()))
        self.assertEqual(self.cli("profile-save", "--file", str(pf)).returncode, 0)
        self.assertTrue((self.data / "statement_profiles" / "test-bank.json").exists())
        listed = json.loads(self.cli("profile-list").stdout)["profiles"]
        self.assertEqual(listed, ["example-generic-csv", "test-bank"])

    def test_doctor_reports_user_scoped_dirs(self):
        r = subprocess.run([sys.executable, str(PLUGIN / "scripts" / "ensure_runtime.py"), "--check"], cwd="/", env=self.env, capture_output=True, text=True)
        d = json.loads(r.stdout)
        self.assertEqual(Path(d["dirs"]["runtime_dir"]["path"]), self.data / "runtime")
        self.assertEqual(Path(d["dirs"]["output_dir"]["path"]), self.data / "outputs" / "statements")
        self.assertIn(str(self.home / ".claude" / "uploads"), [x["path"] for x in d["allowed_input_dirs"]])


if __name__ == "__main__":
    unittest.main()
