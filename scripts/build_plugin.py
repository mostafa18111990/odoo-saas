#!/usr/bin/env python3
"""Build the installable Claude Code plugin `plugins/odoo-accountant/` from the repository's single source of truth.

    python3 scripts/build_plugin.py            # (re)generate plugins/odoo-accountant
    python3 scripts/build_plugin.py --check    # fail (exit 1) if the committed plugin has drifted from the sources

Sources -> plugin:
    src/odoo_accountant/**                          -> src/odoo_accountant/**
    scripts/{run_odoo_accountant_mcp,odoo_accountant_cli,ensure_runtime}.py -> scripts/
    statement_profiles/*.json                       -> statement_profiles/   (bundled examples)
    .claude/skills/odoo-accountant/**               -> skills/odoo-accountant/   (+ a plugin-mode section)
    .claude/agents/odoo-accountant.md               -> agents/odoo-accountant.md (plugin-safe frontmatter, plugin tool names)
generated: .claude-plugin/plugin.json, .mcp.json, hooks/hooks.json, settings.example.json, README.md
"""
import filecmp
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from odoo_accountant import __version__  # noqa: E402

PLUGIN = "odoo-accountant"
SERVER = "odoo-accountant"
OUT = ROOT / "plugins" / PLUGIN
TOOL_PREFIX_PROJECT = "mcp__odoo-accountant__"
TOOL_PREFIX_PLUGIN = f"mcp__plugin_{PLUGIN}_{SERVER}__"      # Claude Code names plugin MCP servers `plugin:<plugin>:<server>`
SCRIPTS = ("run_odoo_accountant_mcp.py", "odoo_accountant_cli.py", "ensure_runtime.py")
REPO_URL = "https://github.com/mostafa18111990/odoo-saas"


def write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def copy_tree(src: Path, dst: Path) -> None:
    for f in sorted(src.rglob("*")):
        if f.is_dir() or "__pycache__" in f.parts or f.suffix == ".pyc":
            continue
        target = dst / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)
        target.chmod(0o755 if f.stat().st_mode & 0o111 else 0o644)


def plugin_agent(text: str) -> str:
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    front, body = m.group(1), m.group(2)
    keep = []
    for line in front.splitlines():
        if line.startswith(("permissionMode:", "mcpServers:", "hooks:")):      # not allowed for plugin-shipped agents (security)
            continue
        if line.startswith("tools:"):
            line = line.replace(TOOL_PREFIX_PROJECT, TOOL_PREFIX_PLUGIN)
        keep.append(line)
    keep = [l.replace("- odoo-accountant", f"- {PLUGIN}:odoo-accountant") if l.strip() == "- odoo-accountant" else l for l in keep]   # plugin skills are namespaced
    body = body.replace("`.runtime/`", "`~/.odoo-accountant/` (أو `.runtime/` في المشروع)")
    return "---\n" + "\n".join(keep) + "\n---\n" + body


PLUGIN_MODE_SECTION = """
## وضع الإضافة (plugin)
- الحالة الدائمة (الموافقات، التدقيق، الملفات الناتجة، profiles المحفوظة) في `~/.odoo-accountant/` (يمكن تغييره بـ `ODOO_ACCOUNTANT_HOME`)، لأن مجلد الإضافة يُستبدل عند التحديث.
- السكربتات تحت `${CLAUDE_PLUGIN_ROOT}/scripts/` (`ensure_runtime.py --check` للفحص). أمر كود الموافقة الذي تعرضه الأداة يحمل مساره المطلق الجاهز للتشغيل.
- مجلدات الملفات المسموحة افتراضيًا: `~/.claude/uploads` و`~/.odoo-accountant/inputs` و`~/.odoo-accountant/outputs/statements`، وغيرها عبر `ODOO_ACCOUNTANT_INPUT_DIRS`.
- أسماء أدوات MCP في هذا الوضع تبدأ بـ `mcp__plugin_odoo-accountant_odoo-accountant__`.
"""


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    copy_tree(ROOT / "src" / "odoo_accountant", out / "src" / "odoo_accountant")
    (out / "scripts").mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        shutil.copyfile(ROOT / "scripts" / name, out / "scripts" / name)
        (out / "scripts" / name).chmod(0o755)
    for f in sorted((ROOT / "statement_profiles").glob("*.json")):
        write(out / "statement_profiles" / f.name, f.read_text(encoding="utf-8"), 0o644)
    copy_tree(ROOT / ".claude" / "skills" / "odoo-accountant", out / "skills" / PLUGIN)
    skill = out / "skills" / PLUGIN / "SKILL.md"
    write(skill, skill.read_text(encoding="utf-8").rstrip("\n") + "\n" + PLUGIN_MODE_SECTION, 0o644)
    write(out / "agents" / "odoo-accountant.md", plugin_agent((ROOT / ".claude" / "agents" / "odoo-accountant.md").read_text(encoding="utf-8")), 0o644)

    manifest = {
        "name": PLUGIN, "version": __version__,
        "description": "موظف محاسب آلي لـ Odoo 19: تقارير ذمم وتحصيل وإقفال، تطبيع واستيراد كشوف البنك (CSV/XLSX/XLS) بمعاينة وتحقق وبوابة موافقة قابلة للتدقيق.",
        "author": {"name": "mostafa18111990"}, "homepage": REPO_URL, "repository": REPO_URL,
        "keywords": ["odoo", "accounting", "bank-statement", "reconciliation", "arabic", "saudi"],
    }
    write(out / ".claude-plugin" / "plugin.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", 0o644)
    mcp = {"mcpServers": {SERVER: {"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/run_odoo_accountant_mcp.py"],
                                   "env": {"ODOO_URL": "${ODOO_URL}", "ODOO_DB": "${ODOO_DB}", "ODOO_LOGIN": "${ODOO_LOGIN}", "ODOO_API_KEY": "${ODOO_API_KEY:-}"}}}}
    write(out / ".mcp.json", json.dumps(mcp, indent=2) + "\n", 0o644)
    hooks = {"description": "Installs the pinned runtime requirements (xlrd==2.0.1 for legacy .xls) when missing; never fails the session.",
             "hooks": {"SessionStart": [{"matcher": "startup|resume", "hooks": [{"type": "command", "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ensure_runtime.py"'}]}]}}
    write(out / "hooks" / "hooks.json", json.dumps(hooks, indent=2) + "\n", 0o644)
    tools_ask = [f"{TOOL_PREFIX_PLUGIN}{t}" for t in ("approve_action", "reject_action", "execute_approved_action")]
    tools_allow = [f"{TOOL_PREFIX_PLUGIN}{t}" for t in ("accounting_snapshot", "overdue_followup", "bank_match_suggest", "partner_data_quality", "vendor_bill_review",
                                                      "period_close_check", "normalize_statement_file", "statement_import_preview", "propose_statement_import",
                                                      "propose_action", "list_pending_approvals")]
    settings = {"_comment": "Plugins cannot ship permission rules. Merge this into ~/.claude/settings.json (or the project's .claude/settings.json).",
                "permissions": {"allow": tools_allow, "ask": tools_ask, "deny": ["Read(~/.odoo-accountant/**)", "Edit(~/.odoo-accountant/**)", "Write(~/.odoo-accountant/**)"]}}
    write(out / "settings.example.json", json.dumps(settings, indent=2, ensure_ascii=False) + "\n", 0o644)
    write(out / "README.md", f"""# odoo-accountant (Claude Code plugin) v{__version__}

موظف محاسب آلي لـ Odoo 19. الدليل الكامل بالعربية: `docs/PLUGIN.md` في المستودع {REPO_URL}.

```
/plugin marketplace add mostafa18111990/odoo-saas
/plugin install odoo-accountant@odoo-saas
```

المطلوب في بيئتك: `ODOO_URL` و`ODOO_DB` و`ODOO_LOGIN` (و`ODOO_API_KEY` اختياري إن لم تتوفر بيانات اعتماد محقونة)، وبايثون 3.10+.
أضف قواعد الصلاحيات من `settings.example.json` إلى إعداداتك (الإضافات لا تحمل صلاحيات): تنفيذ الموافقات يسأل المستخدم دائمًا.
الملف يُولَّد من المصدر بـ `python3 scripts/build_plugin.py` ولا يُعدَّل يدويًا.
""", 0o644)


def trees_equal(a: Path, b: Path) -> list:
    problems = []
    ignore = lambda p: "__pycache__" in p.parts or p.suffix == ".pyc"      # bytecode written by running the copy is not part of the plugin
    fa = {p.relative_to(a) for p in a.rglob("*") if p.is_file() and not ignore(p)}
    fb = {p.relative_to(b) for p in b.rglob("*") if p.is_file() and not ignore(p)}
    problems += [f"missing in committed plugin: {p}" for p in sorted(fa - fb)] + [f"unexpected in committed plugin: {p}" for p in sorted(fb - fa)]
    for p in sorted(fa & fb):
        if not filecmp.cmp(a / p, b / p, shallow=False):
            problems.append(f"content differs: {p}")
        elif ((a / p).stat().st_mode & 0o111) != ((b / p).stat().st_mode & 0o111):
            problems.append(f"exec bit differs: {p}")
    return problems


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--check" in argv:
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / PLUGIN
            build(fresh)
            problems = trees_equal(fresh, OUT) if OUT.exists() else ["plugins/odoo-accountant does not exist"]
        for p in problems:
            print("DRIFT:", p)
        print("plugin is in sync with sources" if not problems else "run: python3 scripts/build_plugin.py")
        return 1 if problems else 0
    build(OUT)
    print(f"built {OUT.relative_to(ROOT)} (v{__version__})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
