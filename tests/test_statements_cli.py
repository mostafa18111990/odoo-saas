import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from odoo_accountant import cli
from odoo_accountant.service import AccountingEmployee
from odoo_accountant.statements.profiles import PROFILES_DIR, load_profile
from tests.fakes import FakeOdoo, TempRuntime, seed_statement_tables
from tests.statement_fixtures import CSV_STD, profile_std


class CliStatementTests(unittest.TestCase):
    def run_cli(self, argv, emp, cfg):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.run(argv, employee=emp, config=cfg)
        return code, out.getvalue()

    def test_preview_propose_and_no_code_leak(self):
        with TempRuntime() as rt, tempfile.TemporaryDirectory() as tmp:
            fake = FakeOdoo(seed_statement_tables())
            prof_dir = Path(tmp)
            (prof_dir / "p.json").write_text(json.dumps(profile_std()))
            (prof_dir / "s.csv").write_text(CSV_STD)
            emp = AccountingEmployee(rt.config, client=fake, profiles_dir=prof_dir)
            common = ["--file", str(prof_dir / "s.csv"), "--profile", "@" + str(prof_dir / "p.json"), "--company-id", "1", "--journal-id", "14",
                      "--bank-account-id", "9", "--currency", "SAR", "--opening", "10000", "--closing", "12749.5"]
            code, out = self.run_cli(["statement-preview"] + common, emp, rt.config)
            d = json.loads(out)
            self.assertEqual(code, 0); self.assertTrue(d["data"]["ready_to_propose"])
            code, out = self.run_cli(["statement-propose"] + common, emp, rt.config)
            d = json.loads(out)
            self.assertEqual(code, 0); self.assertNotIn("approval_code", d["data"])
            self.assertEqual(fake.mutations, [])
            # a human terminal may explicitly ask to print the one-time code
            code, out = self.run_cli(["statement-propose", "--print-code"] + common, emp, rt.config)
            d = json.loads(out)
            self.assertEqual(code, 0); self.assertEqual(len(d["data"]["approval_code"]), 8)
            self.assertEqual(fake.mutations, [])
            # dry-run is the default for execute: nothing written even with a valid (unapproved) id
            aid = d["data"]["approval"]["approval_id"]
            code, out = self.run_cli(["execute", aid, "--code", d["data"]["approval_code"]], emp, rt.config)
            self.assertEqual(code, 1); self.assertEqual(fake.mutations, [])

    def test_profile_save_and_example_profile_valid(self):
        self.assertEqual(load_profile("example-generic-csv", PROFILES_DIR).format, "csv")


if __name__ == "__main__":
    unittest.main()
