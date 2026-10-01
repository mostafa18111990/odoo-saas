# odoo-accountant كـ Claude Code plugin («موظف» قابل للتثبيت)

نفس الموظف المحاسب (skill + agent + أدوات MCP + hook التهيئة) في حزمة واحدة قابلة للتثبيت في أي مشروع أو حساب، بدل بقائها داخل هذا المستودع فقط. المصدر الوحيد ما في المستودع، و`plugins/odoo-accountant/` **مولَّد** منه.

## التثبيت
```
/plugin marketplace add mostafa18111990/odoo-saas
/plugin install odoo-accountant@odoo-saas
```
- السوق (`.claude-plugin/marketplace.json`) يُقرأ من **الفرع الافتراضي** للمستودع؛ قبل الدمج جرّب محليًا:
  `claude plugin marketplace add /مسار/odoo-saas` ثم `claude plugin install odoo-accountant@odoo-saas`، أو للتجربة المؤقتة `claude --plugin-dir ./plugins/odoo-accountant`.
- بعد التثبيت أعد تشغيل الجلسة (أو `/reload-plugins`) ليتصل خادم MCP ويعمل الـ hook.

## المتطلبات
- `python3` 3.10 أو أحدث على الجهاز.
- متغيرات البيئة: `ODOO_URL` و`ODOO_DB` و`ODOO_LOGIN`، و`ODOO_API_KEY` اختياري إن لم تتوفر بيانات اعتماد محقونة. لا أسرار داخل الإضافة.
- `xlrd==2.0.1` لقراءة XLS القديم: يثبّته الـ hook `SessionStart` تلقائيًا (إصدار مثبّت، لا يفشل الجلسة).

## الصلاحيات (مهم)
الإضافات لا تحمل قواعد صلاحيات. انسخ `permissions` من `plugins/odoo-accountant/settings.example.json` إلى `~/.claude/settings.json` (أو إعدادات المشروع):
- **allow**: أدوات القراءة والمعاينة والاقتراح (لا تكتب في Odoo).
- **ask**: `approve_action` و`reject_action` و`execute_approved_action` — يُسأل المستخدم دائمًا.
- **deny**: قراءة/كتابة `~/.odoo-accountant/**` من الوكيل.
وبدون هذه القواعد تعمل الأدوات لكن كل استدعاء يطلب موافقتك (آمن بالافتراض).

## أين تُحفظ الحالة؟
في `~/.odoo-accountant/` (غيّره بـ `ODOO_ACCOUNTANT_HOME`) وليس داخل مجلد الإضافة، لأن Claude Code يستبدل مجلد الإضافة عند كل تحديث:
| المسار | المحتوى |
|---|---|
| `runtime/` | الموافقات المؤقتة وكودها، سجل التدقيق المتسلسل، idempotency، سجل بصمات الملفات |
| `outputs/statements/` | ملفات XLSX الناتجة من `normalize_statement_file` |
| `statement_profiles/` | profiles البنوك المحفوظة (الأمثلة المرفقة تبقى ظاهرة) |
| `inputs/` | مجلد اختياري لملفات الكشوف (إضافةً إلى `~/.claude/uploads`) |

## الفرق عن وضع المشروع
- أسماء أدوات MCP تبدأ بـ `mcp__plugin_odoo-accountant_odoo-accountant__` (مثل `…__normalize_statement_file`)، واسم الـ agent `odoo-accountant:odoo-accountant` والـ skill بالاسم نفسه.
- الـ agent لا يحمل `permissionMode` ولا `mcpServers` ولا `hooks` (غير مسموح بها لوكلاء الإضافات)، وبلا Bash.
- أمر كود الموافقة تعرضه الأداة بمسار مطلق جاهز للتشغيل من أي مجلد. في الجلسات السحابية لا تعمل أوامر `!`؛ يُسلَّم الكود كمرفق (انظر `references/statement-import.md`).

## التحديث والصيانة
1. عدّل المصدر في المستودع (الكود، `.claude/skills/odoo-accountant`, `.claude/agents/odoo-accountant.md`).
2. ارفع `__version__` في `src/odoo_accountant/__init__.py`.
3. `python3 scripts/build_plugin.py` لإعادة توليد الإضافة، ثم commit.
4. اختبار `tests/test_plugin_package.py` يفشل إن اختلفت الإضافة المرفوعة عن المصدر (`--check`)، ويشغّل `claude plugin validate --strict` وتشغيلًا كاملًا من مجلد الإضافة بحالة في `~/.odoo-accountant`.
5. المستخدمون: `/plugin update odoo-accountant@odoo-saas`.

## ما تحققتُ منه وما لا
تحققتُ عمليًا بـ Claude Code 2.1.42: `claude plugin validate` (الإضافة والسوق والـ agent والـ skill، وبـ `--strict`)، وتثبيت من السوق في HOME معزول (مكوّنات: 1 skill و1 agent و1 hook و1 MCP)، وتشغيل headless: خادم `plugin:odoo-accountant:odoo-accountant` متصل بـ 14 أداة بالأسماء أعلاه؛ وتشغيل كامل (تطبيع ← اقتراح ← موافقة ← تنفيذ) من مجلد الإضافة على Odoo وهمي بحالة في `~/.odoo-accountant`.
لم أتحقق من: تثبيت الإضافة من GitHub البعيد (الفرع الحالي غير الافتراضي)، وتحميل الـ agent المسبق للـ skill باسمها المسبوق في بيئتك، وتشغيل الـ hook على جهازك. وكتابات Odoo (غير الاستيراد المحاكى) لم تُختبر على Odoo حي.
