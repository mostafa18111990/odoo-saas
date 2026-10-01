"""End-to-end (non-live) audit of the real upload path:

  Claude attachment / base64  ->  normalize_statement_file  ->  statement_import_preview  ->  propose_statement_import
  ->  approval (human code)   ->  executor  ->  JSON-2 `account.bank.statement.line/create`  ->  read-back

The REAL MCP server runs as a subprocess (scripts/run_odoo_accountant_mcp.py, exactly as .mcp.json starts it) and talks
real HTTP to a local mock that speaks Odoo's JSON-2 URL shape. Nothing here touches a live Odoo.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from odoo_accountant.statements import readers
from tests.e2e_support import McpProcess
from tests.mock_odoo_http import MockOdoo
from tests.fakes import seed_statement_tables
from tests.statement_fixtures import b64

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_bank_export.xls"
TARGET = {"company_id": 1, "journal_id": 14, "bank_account_id": 9, "currency": "SAR"}
needs_xlrd = unittest.skipUnless(readers.xls_available(), 'needs "xlrd==2.0.1"')
FULL_1 = "حوالة محلية واردة ACME SENDER CO المرسل من مصرف تجريبي مبلغ التحويل 7,452.00 ريال سعودي مرجع العملية SYN000001"


class E2EBase(unittest.TestCase):
    token = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oa_e2e_"))
        self.home = self.tmp / "home"
        self.uploads = self.home / ".claude" / "uploads" / "session-1"      # where Claude Code stores attachments
        self.uploads.mkdir(parents=True)
        self.runtime, self.out = self.tmp / "runtime", self.tmp / "out"
        self.mock = MockOdoo(seed_statement_tables()).__enter__()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home), "ODOO_URL": self.mock.url, "ODOO_DB": "e2edb", "ODOO_LOGIN": "e2e",
               "ODOO_ACCOUNTANT_RUNTIME_DIR": str(self.runtime), "ODOO_ACCOUNTANT_OUTPUT_DIR": str(self.out), "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        if self.token:
            env["ODOO_API_KEY"] = self.token
        self.env = env
        self.mcp = McpProcess(env)
        init = self.mcp.rpc("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "odoo-accountant")

    def tearDown(self):
        self.mcp.close()
        self.mock.__exit__()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def human_code(self, approval_id: str, hint: str) -> str:
        """The human gets the code with the exact command the tool tells them to run (fresh shell, no PYTHONPATH)."""
        m = re.search(r"(python3 \S*odoo_accountant_cli\.py show-code apr_\w+)", hint)
        self.assertIsNotNone(m, f"hint must be a runnable command: {hint!r}")
        cmd = m.group(1).split()
        out = subprocess.run([sys.executable] + cmd[1:], cwd="/", env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        code = out.stdout.strip()
        self.assertRegex(code, r"^[A-Z2-9]{8}$")
        return code

    def creates(self):
        return [e for e in self.mock.mutations if e["method"] == "create"]


class ToolSurfaceTests(E2EBase):
    def test_tools_registered_and_wired_for_the_accountant(self):
        tools = {t["name"]: t for t in self.mcp.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(len(tools), 14)
        for name in ("normalize_statement_file", "statement_import_preview", "propose_statement_import", "approve_action", "execute_approved_action", "reject_action", "list_pending_approvals"):
            self.assertIn(name, tools)
        for name in ("normalize_statement_file", "statement_import_preview", "propose_statement_import"):
            props = tools[name]["inputSchema"]["properties"]
            self.assertIn("source_path", props, name); self.assertIn("content_base64", props, name)
            self.assertFalse(tools[name]["inputSchema"]["additionalProperties"])
        self.assertIn("xls", tools["normalize_statement_file"]["inputSchema"]["properties"]["format"]["enum"])
        self.assertIn("xls", tools["statement_import_preview"]["inputSchema"]["properties"]["format"]["enum"])
        self.assertEqual(self.mock.log, [])            # listing tools touches nothing


@needs_xlrd
class XlsAttachmentToOdooTests(E2EBase):
    def test_attached_xls_to_created_statement_lines(self):
        att = self.uploads / "b00911f5-accountTransactions_35.xls"
        att.write_bytes(FIXTURE.read_bytes())
        import hashlib
        src_sha = hashlib.sha256(att.read_bytes()).hexdigest()

        # 1) normalize: the attachment path is accepted by default (HOME/.claude/uploads), source untouched, nothing sent to Odoo
        n = self.mcp.call("normalize_statement_file", source_path=str(att))
        self.assertTrue(n["ok"], n["message"])
        d = n["data"]
        self.assertEqual((d["counts"]["rows_accepted"], d["totals"]["sum"]), (2, 13041.0))
        self.assertEqual(d["output"]["columns"], ["Date", "Label", "Amount"]); self.assertTrue(d["output"]["roundtrip_verified"])
        out_path = Path(d["output"]["path"])
        self.assertEqual(out_path.parent, self.out); self.assertTrue(out_path.exists())
        self.assertEqual(hashlib.sha256(att.read_bytes()).hexdigest(), src_sha); self.assertTrue(d["source"]["unchanged_after_run"])
        self.assertEqual(self.mock.log, [])

        # 2) preview on the normalized file (read-only calls to Odoo)
        pv = self.mcp.call("statement_import_preview", source_path=str(out_path), **TARGET)["data"]
        self.assertTrue(pv["ready_to_propose"], pv["blockers"])
        self.assertEqual(pv["description_visibility"]["status"], "ok")
        self.assertEqual(pv["provenance"]["source_sha256"], src_sha)
        self.assertEqual(self.mock.mutations, [])
        self.assertTrue({"res.company", "account.journal", "res.partner.bank", "ir.ui.view"} <= {e["model"] for e in self.mock.log})
        self.assertEqual({e["db"] for e in self.mock.log}, {"e2edb"})

        # 3) proposal: pending, no code leaked to the model, rows complete in the approval payload, still nothing written
        pr = self.mcp.call("propose_statement_import", source_path=str(out_path), **TARGET)
        self.assertTrue(pr["ok"], pr["message"])
        appr = pr["data"]["approval"]
        self.assertEqual((appr["status"], appr["risk"], appr["action"]), ("pending", "FINANCIAL_FINAL", "import_bank_statement_lines"))
        self.assertNotIn("approval_code", json.dumps(pr))
        self.assertEqual(self.mock.mutations, [])
        rec = json.loads((self.runtime / "approvals" / f"{appr['approval_id']}.json").read_text())
        lines = rec["params"]["lines"]
        self.assertEqual([(l["date"], l["amount"]) for l in lines], [("2026-09-29", 7452.0), ("2026-09-30", 5589.0)])
        self.assertEqual(lines[0]["payment_ref"], FULL_1)
        self.assertTrue(all(l["payment_ref"].strip() and "\n" not in l["payment_ref"] for l in lines))
        self.assertEqual((rec["params"]["journal_id"], rec["params"]["currency"], rec["params"]["file_sha256"] is not None), (14, "SAR", True))

        # 4) approval by the human: code delivered via the command the tool prints, exact payload hash required
        code = self.human_code(appr["approval_id"], pr["data"]["code_delivery"])
        self.assertFalse(self.mcp.call("approve_action", approval_id=appr["approval_id"], code=code)["ok"])            # hash is mandatory
        self.assertFalse(self.mcp.call("execute_approved_action", approval_id=appr["approval_id"], code=code)["ok"])     # not approved yet
        self.assertEqual(self.mock.mutations, [])
        self.assertTrue(self.mcp.call("approve_action", approval_id=appr["approval_id"], code=code, payload_hash=appr["payload_hash"])["ok"])
        dry = self.mcp.call("execute_approved_action", approval_id=appr["approval_id"], code=code, dry_run=True)
        self.assertEqual(dry["data"]["status"], "dry_run"); self.assertEqual(self.mock.mutations, [])

        # 5) execution: the correct Odoo model/method, complete vals, then read-back
        ex = self.mcp.call("execute_approved_action", approval_id=appr["approval_id"], code=code)
        self.assertTrue(ex["ok"], ex["message"]); self.assertEqual(ex["data"]["status"], "executed")
        self.assertEqual([(e["model"], e["method"]) for e in self.mock.mutations], [("account.bank.statement.line", "create")])
        req = self.creates()[0]
        self.assertEqual(req["path"], "/json/2/account.bank.statement.line/create")
        vals = req["body"]["vals_list"]
        self.assertEqual(len(vals), 2)
        for v, l in zip(vals, lines):
            self.assertEqual((v["journal_id"], v["date"], v["amount"], v["payment_ref"]), (14, l["date"], l["amount"], l["payment_ref"]))
            self.assertTrue(v["unique_import_id"].startswith("OA-"))
            self.assertNotIn("payment_reference", v)
        rows = self.mock.fake.tables["account.bank.statement.line"]
        self.assertEqual([(r["date"], r["amount"], r["payment_ref"]) for r in rows], [(l["date"], l["amount"], l["payment_ref"]) for l in lines])
        read_back = [e for e in self.mock.log[self.mock.log.index(req) + 1:] if e["model"] == "account.bank.statement.line" and e["method"] == "search_read"]
        self.assertTrue(read_back and "payment_ref" in read_back[0]["body"]["fields"])
        labels = [c for c in ex["data"]["verification"]["checks"] if "payment_ref" in c[0]]
        self.assertTrue(labels and all(ok for _, ok in labels))

        # 6) replay / re-proposal are refused; audit trail exists and has no file content
        self.assertFalse(self.mcp.call("execute_approved_action", approval_id=appr["approval_id"], code=code)["ok"])
        self.assertFalse(self.mcp.call("propose_statement_import", source_path=str(out_path), **TARGET)["ok"])
        self.assertEqual(len(self.creates()), 1)
        audit = (self.runtime / "audit.jsonl").read_text()
        for ev in ("statement_normalize", "propose", "approve", "execute_start", "execute_result"):
            self.assertIn(ev, audit)
        self.assertNotIn("ACME SENDER", audit)


REAL_ATTACHMENT = Path(os.environ.get("ODOO_ACCOUNTANT_TEST_XLS", "/root/.claude/uploads/dcc0629b-b026-5cd1-a399-62b5d832c40c/b00911f5-accountTransactions_35.xls"))


@needs_xlrd
@unittest.skipUnless(REAL_ATTACHMENT.exists(), "real attachment not present")
class RealAttachmentToOdooTests(E2EBase):
    """The user's actual bank file through the real MCP process into a MOCK Odoo (never the live one)."""

    def test_real_file_end_to_end(self):
        att = self.uploads / REAL_ATTACHMENT.name
        shutil.copyfile(REAL_ATTACHMENT, att)
        n = self.mcp.call("normalize_statement_file", source_path=str(att))
        self.assertTrue(n["ok"], n["message"])
        self.assertEqual((n["data"]["counts"]["rows_accepted"], n["data"]["totals"]["sum"]), (2, 13041.0))
        pr = self.mcp.call("propose_statement_import", source_path=n["data"]["output"]["path"], **TARGET)
        self.assertTrue(pr["ok"], pr["message"])
        appr = pr["data"]["approval"]
        code = self.human_code(appr["approval_id"], pr["data"]["code_delivery"])
        self.assertTrue(self.mcp.call("approve_action", approval_id=appr["approval_id"], code=code, payload_hash=appr["payload_hash"])["ok"])
        ex = self.mcp.call("execute_approved_action", approval_id=appr["approval_id"], code=code)
        self.assertTrue(ex["ok"], ex["message"])
        self.assertEqual([(e["model"], e["method"]) for e in self.mock.mutations], [("account.bank.statement.line", "create")])
        rows = self.mock.fake.tables["account.bank.statement.line"]
        self.assertEqual([(r["date"], r["amount"]) for r in rows], [("2026-09-29", 7452.0), ("2026-09-30", 5589.0)])
        for r in rows:
            self.assertIn("حوالة محلية واردة", r["payment_ref"]); self.assertGreater(len(r["payment_ref"]), 200); self.assertNotIn("\n", r["payment_ref"])


class Base64UploadTests(E2EBase):
    token = "E2E-TOKEN-SHOULD-NEVER-LEAK"

    def test_content_base64_csv_upload_with_token_hygiene(self):
        csv_text = 'Date,Description,Amount,Balance\n15/09/2026,"حوالة محلية واردة\n      ACME SENDER CO المرسل\n      مرجع العملية SYN000001",7452.00,"189,461.10"\n16/09/2026,"حوالة محلية واردة\n      ACME SENDER CO المرسل\n      مرجع العملية SYN000002",5589.00,"195,050.10"\n'
        n = self.mcp.call("normalize_statement_file", content_base64=b64(csv_text), filename="telegram_upload.csv")
        self.assertTrue(n["ok"], n["message"])
        xlsx = Path(n["data"]["output"]["path"]).read_bytes()
        # the normalized bytes themselves are accepted as an upload (what a Telegram handler would forward)
        pr = self.mcp.call("propose_statement_import", content_base64=b64(xlsx), filename=n["data"]["output"]["filename"], **TARGET)
        self.assertTrue(pr["ok"], pr["message"])
        appr = pr["data"]["approval"]
        code = self.human_code(appr["approval_id"], pr["data"]["code_delivery"])
        self.assertTrue(self.mcp.call("approve_action", approval_id=appr["approval_id"], code=code, payload_hash=appr["payload_hash"])["ok"])
        ex = self.mcp.call("execute_approved_action", approval_id=appr["approval_id"], code=code)
        self.assertTrue(ex["ok"], ex["message"])
        self.assertEqual([(e["model"], e["method"]) for e in self.mock.mutations], [("account.bank.statement.line", "create")])
        rows = self.mock.fake.tables["account.bank.statement.line"]
        self.assertEqual([r["payment_ref"] for r in rows], ["حوالة محلية واردة ACME SENDER CO المرسل مرجع العملية SYN000001", "حوالة محلية واردة ACME SENDER CO المرسل مرجع العملية SYN000002"])
        # credentials: sent as Bearer to Odoo, but never persisted or echoed
        self.assertTrue(all(e["auth"] == "Bearer " + self.token for e in self.mock.log))
        blob = "".join(p.read_text(errors="ignore") for p in self.tmp.rglob("*") if p.is_file() and p.suffix in (".json", ".jsonl", ".code"))
        self.assertNotIn(self.token, blob)
        self.assertNotIn(self.token, json.dumps([n, pr, ex], ensure_ascii=False))

    def test_paths_outside_allowed_dirs_are_refused(self):
        outside = self.tmp / "elsewhere.csv"
        outside.write_text("Date,Description,Amount\n15/09/2026,a,1.00\n")
        r = self.mcp.call("normalize_statement_file", source_path=str(outside))
        self.assertTrue(r["_is_error"]); self.assertIn("خارج المجلدات المسموح", r["message"])
        self.assertEqual(self.mock.log, [])


if __name__ == "__main__":
    unittest.main()
