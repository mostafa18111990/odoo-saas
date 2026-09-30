# الموظف المحاسب الآلي — odoo-accountant

موظف AI محاسب على **Odoo 19** (JSON-2) يعمل الآن داخل Claude Code، وجاهز معماريًا لاستقبال الأوامر لاحقًا من Telegram. القدرة التقنية كاملة (قراءة، إنشاء، تعديل، ترحيل، دفع، مطابقة، إشعار دائن، إلغاء/عكس، أنشطة، رسائل، قفل فترة)، لكن **كل تنفيذ حساس يمر ببوابة موافقة قابلة للتدقيق**. «صلاحيات كاملة» = توفر القدرة، لا التنفيذ العشوائي.

## ما يستطيعه
| النوع | العمليات |
|---|---|
| قراءة (فورية) | `accounting_snapshot`, `overdue_followup`, `bank_match_suggest`, `partner_data_quality`, `vendor_bill_review`, `period_close_check`, `normalize_statement_file`, `statement_import_preview` |
| كتابة (بموافقة) | `create_draft_customer_invoice`, `create_draft_vendor_bill`, `update_draft_move`, `post_move`, `register_payment`, `reconcile_statement_line`, `create_credit_note`, `cancel_or_reverse_move`, `create_followup_activity`, `send_followup_message`, `set_period_lock`, `import_bank_statement_lines` (عبر `propose_statement_import`) |
| مرفوض | الحذف (`unlink`) وأي دالة Odoo غير مدرجة. لا توجد أداة «استدعاء أي دالة». |

## مستويات الصلاحية
| المستوى | أمثلة | الشرط |
|---|---|---|
| `READ` | التقارير الستة | بلا موافقة |
| `DRAFT_WRITE` | مسودة فاتورة، تعديل مسودة، نشاط متابعة | موافقة واحدة بالكود؛ صلاحية 30 دقيقة |
| `FINANCIAL_FINAL` | استيراد كشف بنكي، ترحيل، دفع، مطابقة، إشعار دائن، إلغاء/عكس، رسالة خارجية، قفل فترة | موافقة صريحة على **بصمة الحمولة (payload_hash)** + الكود؛ صلاحية 10 دقائق |
| `DESTRUCTIVE` | حذف | غير مسموح افتراضيًا (`ODOO_ACCOUNTANT_ALLOW_DESTRUCTIVE=1` لا يضيف حذفًا فعليًا لعدم وجود handler) |

## دورة الأمر
1. **propose**: التحقق من المعاملات (رفض أي معامل غير معروف)، قراءة الحالة الحالية، حساب `expected` (بصمة الحالة)، اشتقاق مفتاح idempotency، وإنشاء سجل موافقة موقّع (HMAC) مع `payload_hash`. يُخزَّن الكود مجزّأً؛ نسخته الوحيدة في `.runtime/pending_codes/` للإنسان. إن وُجدت **موانع** (blockers) لا تُنشأ موافقة.
2. **approve**: يتحقق من المُوافِق (قائمة `ODOO_ACCOUNTANT_APPROVERS`)، والكود، والانتهاء، وبصمة الحمولة (إلزامية للمالية النهائية). 5 أكواد خاطئة = قفل الطلب.
3. **execute**: فحوص قبل التنفيذ (السياسة، الموافقة، عدم انحراف الحالة عن `expected`، idempotency)، ثم **استهلاك الموافقة ذريًا** (مرة واحدة)، ثم التنفيذ عبر `client._mutate` فقط، ثم **قراءة راجعة** للتحقق من الحالة والمبلغ والعملة، ثم تدقيق. `dry_run` يتحقق دون استهلاك.
4. **audit**: `.runtime/audit.jsonl` إضافة فقط ومتسلسل بالتجزئة (يكشف العبث: `verify-audit`)، مع حجب تلقائي لأي password/token/authorization/api key/كود.

ضمانات: منع الإعادة (replay)، كشف تغيير الحمولة بعد الموافقة، مفتاح idempotency، فشل مغلق (`failed_review`) عند فشل غير محسوم بدل إعادة المحاولة العمياء.

## أمثلة داخل Claude Code
- «أعطني ملخص آخر 90 يومًا» ← قراءة فورية.
- «رتّب متأخرات العملاء فوق 60 يومًا» ← `overdue_followup`.
- «رحّل المسودات 41001 و41002» ← يعرض الخطة (المبالغ، بصمة الحمولة، انتهاء الصلاحية). أنت تحصل على الكود بنفسك: `! python -m odoo_accountant.cli show-code apr_xxxx`، ثم تكتب: «أوافق، الكود XXXXXXXX، البصمة …» فيُنفَّذ مرة واحدة ويُقرأ الناتج للتحقق.
- من الطرفية: `python -m odoo_accountant.cli propose-action --file plan.json` ثم `approve <id> --code C --payload-hash H --execute` ثم `execute <id> --code C --payload-hash H` (dry-run) ثم أضف `--execute` للتنفيذ الفعلي.

مثال `plan.json`:
```json
{"action": "post_move", "params": {"move_ids": [41001]}}
```

## استيراد كشف الحساب البنكي
- **أدوات MCP (14 إجمالًا):** `normalize_statement_file` (تحويل محلي إلى XLSX قياسي، بلا Odoo وبلا تعديل للمصدر)، `statement_import_preview` (قراءة فقط) و`propose_statement_import` (proposal محلي فقط). لا توجد أداة تنفذ الاستيراد مباشرة؛ التنفيذ عبر `approve_action` ثم `execute_approved_action` بكود وبصمة الحمولة.
- **الصيغ المدعومة فعليًا:** CSV وXLSX وXLS القديم (عبر الاعتماد الاختياري المثبَّت `xlrd==2.0.1`: `pip install "xlrd==2.0.1"` أو `pip install -e ".[xls]"`؛ بدونه يُرفض XLS برسالة واضحة). **OFX وQFX وCAMT.053:** مخطَّطة وتُرفض برسالة واضحة (لا ادعاء دعم غير مختبر). مدخل بديل: `rows` جاهزة (مناسب لملفات Telegram لاحقًا). الحدود: 5MB و5000 صف، ورسائل خطأ عربية.
- **المعاينة:** اكتشاف الأعمدة (اقتراح فقط)، تنسيق التاريخ والأرقام (بما فيها الأرقام العربية)، العملة، الأرصدة الافتتاحية/الختامية وتسلسل عمود الرصيد، وبصمة الملف SHA-256 وبصمة كل حركة، ومقارنة exact/possible مع Odoo.
- **الهدف صريح دائمًا:** `company_id` و`journal_id` و`bank_account_id` و`currency`؛ أي تعارض ⇒ رفض.
- **الـ mapping profile:** JSON لكل بنك في `statement_profiles/` (`python -m odoo_accountant.cli profile-save --file p.json`).
- **التحقق الإلزامي:** كل معاينة تتضمن `description_visibility` (الحقل المعروض `payment_ref` يُثبَت من شاشات التسوية الحية، وتحليل عناوين الأعمدة كما يفعل المعالج، ولا سطر بلا وصف)؛ `blocked`/`unverified` يمنعان الاقتراح. التنفيذ يتحقق بقراءة راجعة أن `payment_ref` مخزَّن.
- **إصلاح أسطر بلا وصف:** `bank_match_suggest` يكشفها (`lines_without_label`) وعملية `fill_statement_line_label` (بموافقة مستقلة) تملأ التسمية الفارغة فقط بنسخ حرفية من `payment_reference`.
- **الأثر:** الاستيراد `create` فقط على `account.bank.statement.line` (قيد كشف بنكي مرحّل لكل سطر)، بلا تسوية ولا حذف ولا تعديل. التسوية مرحلة منفصلة بموافقة أخرى.
- **التطبيع:** ورقة `Bank Transactions` بأعمدة Date وLabel وAmount (العمود Label هو ما يربطه Odoo بالحقل `payment_ref` الذي تعرضه شاشة التسوية؛ العنوان «Payment Reference» يُربط بحقل القيد المخفي `payment_reference` فيظهر الوصف فارغًا)، تواريخ حقيقية، الوارد موجب، وصف مضغوط كامل، نصوص خاملة بلا معادلات، اسم آمن `outputs/statements/normalized_*.xlsx`، وتقرير (عدد، مجموع، تسلسل الرصيد، checksums للمصدر والناتج). `source_path` محصور بمجلدات مسموحة (`inputs/` وuploads وODOO_ACCOUNTANT_INPUT_DIRS). يتوقف عند الغموض ولا ينتج ملفًا عند أي صف مرفوض أو رصيد غير متسق.
- **CLI:** `statement-normalize` و`statement-preview` و`statement-propose` و`profile-save` و`profile-list`.
- مرجع مفصل: `.claude/skills/odoo-accountant/references/statement-import.md`.

## الإعداد
- متغيرات: `ODOO_URL`, `ODOO_DB`, `ODOO_LOGIN` (+ `ODOO_API_KEY` اختياري إن لم تتوفر بيانات اعتماد محقونة). انظر `.env.example` (أسماء فقط).
- `.mcp.json` يشغّل `scripts/run_odoo_accountant_mcp.py` (خادم MCP بمكتبة بايثون القياسية فقط).
- `.claude/settings.json`: أدوات القراءة و`propose_action` مسموحة؛ `approve/reject/execute` تسأل المستخدم دائمًا؛ وقراءة/كتابة `.runtime/` و`.env` ممنوعة على الوكيل.
- الوكيل الفرعي `.claude/agents/odoo-accountant.md` بلا Bash (لا curl) وبـ `permissionMode: default`.

## صدق الحدود
- بوابة الموافقة تعتمد على أن الكود يصل للإنسان فقط (ملف 0600 + منع القراءة في الإعدادات + سؤال المستخدم قبل approve/execute). ليست حماية تشفيرية ضد وكيل يملك Bash مفتوحًا؛ لذلك لا يُمنح الوكيل Bash.
- كتابات Odoo (خصوصًا `reconcile_statement_line` و`create_credit_note` و`cancel_or_reverse_move`) مبنية على دلالات ORM في Odoo 19 **ولم تُختبر على Odoo حي** (ممنوع أثناء البناء). معاينة الاستيراد وحدها اختُبرت حيًا قراءةً فقط. ابدأ بعنصر واحد قليل القيمة وراجع نتيجة القراءة الراجعة.
- المطابقة لا تعمل عند غياب الشريك أو تعدد المرشحين. الإشعار الدائن كامل فقط (لا جزئي).

## جاهزية Telegram
`channels/base.py` يحدد `ChannelAdapter` (إرسال، طلب موافقة، تسليم الكود للإنسان). `channels/telegram_stub.py` يقدّم عقدًا فقط: قائمة مسموحة `TELEGRAM_ALLOWED_USER_IDS`، وتحويل الرسالة إلى `CommandEnvelope`، وصيغة `callback_data` لأزرار Approve/Reject. **المؤجل الوحيد**: الاتصال الفعلي (bot token من البيئة، استقبال التحديثات، إرسال الأزرار والكود لمحادثة الإنسان)، وقد وُسمت بـ TODO. منطق المحاسبة نفسه مفصول عن القناة (`service.AccountingEmployee.handle`).

## الاختبارات
`PYTHONPATH=src python3 -m unittest discover -s tests -t .` — بلا اتصال حي: تصنيف السياسة، الانتهاء، العبث بالحمولة، الإعادة، idempotency، حجب الأسرار، مسارات القراءة، عدم تنفيذ أي تعديل دون موافقة، وخادم MCP.
