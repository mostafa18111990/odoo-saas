# odoo-saas — الموظف المحاسب الآلي (odoo-accountant)

هذا المشروع يحتوي موظفًا محاسبًا آليًا على **Odoo 19** (JSON-2). يعمل الآن داخل Claude Code، وسيستقبل لاحقًا أوامر من Telegram عبر نفس المنطق.

## التفويض
- أي طلب محاسبي أو يخص Odoo (فواتير، تحصيل، ذمم، بنك، موردون، شركاء، إقفال، ملخص) **يُفوَّض تلقائيًا** إلى subagent `odoo-accountant` (`.claude/agents/odoo-accountant.md`) الذي يحمّل skill `odoo-accountant`.
- المخرجات بالعربية؛ الحقول التقنية بالإنجليزية.

## القراءة مقابل الكتابة
- **القراءة** (الملخص، المتأخرات، اقتراح المطابقة، جودة الشركاء، مراجعة الموردين، جاهزية الإقفال): تُنفَّذ فورًا عبر أدوات MCP `odoo-accountant`، مجمّعًا أولًا.
- **أي عملية حساسة** (إنشاء/تعديل/ترحيل/دفع/مطابقة/إشعار دائن/إلغاء/رسالة/نشاط/قفل فترة): تُحضَّر بـ `propose_action` كخطة (سجلات، مبالغ، أثر، بصمة حمولة) ثم **تنتظر موافقة المستخدم الصريحة**. الموافقة تحتاج الكود المؤقت الذي يراه المستخدم فقط، ثم `execute_approved_action` مرة واحدة، ثم قراءة راجعة وتدقيق.
- الحذف مرفوض افتراضيًا.

## استيراد الكشف البنكي
- `statement_import_preview` (قراءة) ثم `propose_statement_import` (proposal فقط) ثم موافقة مستقلة وتنفيذ؛ CSV وXLSX فقط. التسوية مرحلة منفصلة بموافقة أخرى. التفاصيل: `.claude/skills/odoo-accountant/references/statement-import.md`.

## قواعد صارمة
- لا تكشف أو تطلب أو تحفظ أي سر. الاعتماد يأتي من البيئة (`ODOO_URL`, `ODOO_DB`, `ODOO_LOGIN`) وحقن بيانات الاعتماد؛ لا تطلب `ODOO_SECRET`.
- **لا تستدعِ JSON-2 الكتابي مباشرة** (لا curl ولا سكربت خاص). كل تغيير يمر عبر الأدوات الآمنة فقط (`src/odoo_accountant/executor.py`).
- لا `bypassPermissions` ولا `--dangerously-skip-permissions`.
- لا مطابقة تلقائية عند التعدد أو غياب الشريك؛ لا تخمين حساب/ضريبة/شريك.
- `.runtime/` و`.env` خارج git وممنوع قراءتهما من الوكيل.

## التشغيل والاختبار
- اختبارات بلا اتصال حي: `PYTHONPATH=src python3 -m unittest discover -s tests -t .`
- CLI: `PYTHONPATH=src python3 -m odoo_accountant.cli --help` (أوامر الكتابة dry-run افتراضيًا؛ `--execute` للتنفيذ الفعلي).
- المرجع الكامل: `docs/AI_ACCOUNTANT.md`. الأرقام المرجعية القديمة: `.claude/skills/odoo-accountant/references/current-baseline.md` (لقطة مؤرخة، تُحدَّث قبل أي قرار).

## الحالة
أدوات MCP: 13 (6 تقارير + معاينة استيراد + اقتراح استيراد + 4 للموافقات والتنفيذ والقائمة). OFX/QFX/CAMT.053 مخطَّطة وغير مدعومة. Telegram: عقد فقط (`channels/telegram_stub.py`) بلا اتصال فعلي. عملية `reconcile_statement_line` وباقي الكتابات لم تُختبر على Odoo حي (لا كتابة حية أثناء البناء).
