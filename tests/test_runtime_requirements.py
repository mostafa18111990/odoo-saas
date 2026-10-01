"""Guards the operational prerequisites that used to live only in the temporary dev container."""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odoo_accountant.channels.base import LocalChannel
from odoo_accountant.config import Config
from odoo_accountant.mcp_server import TOOLS
from odoo_accountant.models import ChannelContext

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RuntimeScriptTests(unittest.TestCase):
    def test_pins_match_pyproject_and_are_exact(self):
        er = load_script("ensure_runtime")
        text = (ROOT / "pyproject.toml").read_text()
        extra = re.search(r"xls = \[(.*?)\]", text).group(1)
        self.assertEqual(set(re.findall(r'"([^"]+)"', extra)), {f"{n}=={v}" for n, v in er.PINS.items()})
        self.assertIn("dependencies = []", text)            # nothing mandatory beyond the stdlib

    def test_session_start_hook_installs_the_pinned_requirement(self):
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        hooks = settings["hooks"]["SessionStart"]
        cmds = [h["command"] for g in hooks for h in g["hooks"]]
        self.assertEqual(cmds, ['python3 "$CLAUDE_PROJECT_DIR/scripts/ensure_runtime.py"'])
        self.assertTrue((ROOT / "scripts" / "ensure_runtime.py").exists())

    def test_install_uses_only_fixed_pinned_args_and_never_fails_the_session(self):
        er = load_script("ensure_runtime")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1 if "--user" not in cmd else 0, "", "externally-managed-environment")

        with mock.patch.object(er, "installed", lambda n: None), mock.patch.object(er.subprocess, "run", fake_run):
            er.install_missing()
        self.assertEqual(calls[0], [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "xlrd==2.0.1"])
        self.assertEqual(calls[1][-2:], ["--user", "xlrd==2.0.1"])
        with mock.patch.object(er, "installed", lambda n: None), mock.patch.object(er.subprocess, "run", side_effect=OSError("no pip")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(er.main([]), 0)                                   # hook mode: swallow and exit 0
        with mock.patch.object(er, "installed", lambda n: er.PINS[n]), mock.patch.object(er.subprocess, "run") as run:
            er.install_missing()
            run.assert_not_called()                                            # already present -> no pip call

    def test_check_mode_exit_codes(self):
        er = load_script("ensure_runtime")
        with mock.patch.object(er, "installed", lambda n: er.PINS[n]):
            self.assertEqual(er.main(["--check"]), 0)
        with mock.patch.object(er, "installed", lambda n: "0.0.1"):
            self.assertEqual(er.main(["--check"]), 1)

    def test_doctor_runs_in_a_subprocess(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "ensure_runtime.py"), "--check"], capture_output=True, text=True, cwd="/")
        data = json.loads(r.stdout)
        self.assertIn("allowed_input_dirs", data); self.assertIn("odoo_env", data)
        self.assertEqual(r.returncode, 0 if data["all_required_present"] else 1)


class LauncherAndHintTests(unittest.TestCase):
    def test_cli_launcher_works_from_any_cwd_without_pythonpath(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "odoo_accountant_cli.py"), "--help"], capture_output=True, text=True, cwd="/", env=env)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("show-code", r.stdout)

    def test_approval_code_hint_is_a_command_that_exists(self):
        hint = LocalChannel().deliver_approval_code(ChannelContext(), "apr_abc123", "CODE1234")
        self.assertNotIn("CODE1234", hint)
        m = re.search(r"python3 (\S+odoo_accountant_cli\.py) show-code apr_abc123", hint)
        self.assertTrue(m, hint); self.assertTrue(Path(m.group(1)).is_absolute()); self.assertTrue(Path(m.group(1)).exists())


class SurfaceConsistencyTests(unittest.TestCase):
    def test_every_mcp_tool_is_available_to_the_agent_permissioned_and_documented(self):
        agent = next(l for l in (ROOT / ".claude/agents/odoo-accountant.md").read_text().splitlines() if l.startswith("tools:"))
        perms = json.loads((ROOT / ".claude/settings.json").read_text())["permissions"]
        skill = (ROOT / ".claude/skills/odoo-accountant/SKILL.md").read_text()
        for name in TOOLS:
            full = f"mcp__odoo-accountant__{name}"
            self.assertIn(full, agent, name)
            self.assertTrue(full in perms["allow"] or full in perms["ask"], name)
            self.assertIn(name, skill, name)
        for gated in ("approve_action", "reject_action", "execute_approved_action"):
            self.assertIn(f"mcp__odoo-accountant__{gated}", perms["ask"])         # human always prompted
            self.assertNotIn(f"mcp__odoo-accountant__{gated}", perms["allow"])
        self.assertNotIn("Bash", agent)                                          # no shell -> no bypass of the gate
        self.assertTrue(any(d.startswith("Read(./.runtime") for d in perms["deny"]))

    def test_mcp_config_has_no_secret_values_and_launcher_exists(self):
        cfg = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["odoo-accountant"]
        self.assertEqual(cfg["command"], "python3"); self.assertTrue((ROOT / cfg["args"][0]).exists())
        self.assertTrue(all(re.fullmatch(r"\$\{[A-Z_]+(:-)?\}", v) for v in cfg["env"].values()), cfg["env"])

    def test_default_config_allows_claude_attachments_and_generated_files(self):
        with tempfile.TemporaryDirectory() as home:
            cfg = Config.from_env({"HOME": home})
            with mock.patch("pathlib.Path.home", return_value=Path(home)):
                cfg = Config.from_env({})
            dirs = [Path(p) for p in cfg.input_dirs]
            self.assertIn(Path(home) / ".claude" / "uploads", dirs)
            self.assertIn(ROOT / "inputs", dirs); self.assertIn(cfg.output_dir, dirs)
            self.assertEqual(cfg.output_dir, ROOT / "outputs" / "statements")

    def test_generated_and_runtime_files_are_git_ignored(self):
        ign = (ROOT / ".gitignore").read_text()
        for pat in (".runtime/", "outputs/", "inputs/", ".env"):
            self.assertIn(pat, ign)


if __name__ == "__main__":
    unittest.main()
