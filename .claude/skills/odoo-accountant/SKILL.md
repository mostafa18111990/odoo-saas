---
name: odoo-accountant
description: Use for ANY accounting work on the Odoo instance (account.move, account.payment, account.bank.statement.line) — receivables and collections (overdue follow-up), bank reconciliation suggestions, vendor bill review, partner data quality (VAT/country/missing partner), period-close checks, bank-statement normalization (CSV/XLSX/legacy XLS to a standard XLSX), import preview, duplicate detection and import proposal, and accounting snapshots/summaries. Read-only by default over JSON-2; any write needs explicit per-operation user approval. المحاسب الآلي لـ Odoo - الذمم والتحصيل، مطابقة البنك، فواتير الموردين، جودة بيانات الشركاء، إقفال الفترة، الملخص المحاسبي.
---

# odoo-accountant — المحاسب الآلي لـ Odoo

مهارة قراءة أولًا لتحليل ومتابعة العمل المحاسبي في Odoo 19 عبر JSON-2. المخرجات بالعربية والحقول التقنية (`account.move`, `payment_state`, `amount_residual` ...) تبقى بالإنجليزية.

## الأدوات الآمنة (الأساس)
داخل هذا المشروع استخدم أدوات MCP `odoo-accountant` (`accounting_snapshot`, `overdue_followup`, `bank_match_suggest`, `partner_data_quality`, `vendor_bill_review`, `period_close_check`, `normalize_statement_file`, `statement_import_preview`) للقراءة، و`propose_statement_import` لاقتراح استيراد كشف بنكي (الترتيب: تطبيع ← معاينة ← proposal ← موافقة صريحة ← تنفيذ؛ لا يُرفع أي ملف إلى Odoo تلقائيًا؛ توقّف واسأل عند أي غموض في الأعمدة أو التواريخ أو الإشارة؛ والتحقق من ظهور الوصف في شاشة التسوية (description_visibility: الحقل payment_ref = Label) إلزامي في المعاينة قبل طلب أي موافقة، والعمود في الملف القياسي «Label» لا «Payment Reference»)، و`propose_action` ثم `approve_action` ثم `execute_approved_action` لأي كتابة. لا تستدعِ JSON-2 الكتابي مباشرة. التفاصيل: `docs/AI_ACCOUNTANT.md`. الأقسام التالية تبقى مرجع القواعد إن لم تتوفر الأدوات.

## الاتصال والأسرار
- استخدم متغيرات البيئة `ODOO_URL` و`ODOO_DB` و`ODOO_LOGIN` وبيانات الاعتماد الآمنة المتاحة في الجلسة (الوكيل يضيف `Authorization: Bearer` تلقائيًا).
- لا تطلب ولا تطبع ولا تحفظ أي سر أو token أو مفتاح API، لا في الملفات ولا في الردود ولا في السجلات.
- الصيغة: `POST $ODOO_URL/json/2/<model>/<method>` مع الرأسين `Content-Type: application/json` و`X-Odoo-Database: $ODOO_DB`، والجسم JSON بمعاملات الدالة.
- إن فشل الاتصال أو الصلاحية، أبلغ المستخدم بالخطأ ولا تتحايل بطرق أخرى.

## القواعد الإلزامية
1. **القراءة أولًا:** ابدأ دائمًا بـ `search_count` و`formatted_read_group` (استعلامات مجمعة). لا تقرأ سجلات تفصيلية (`search_read`) إلا لعينة محدودة (`limit`) وبحقول لازمة فقط.
2. **الإعلان في كل تقرير:** الفترة (من/إلى) ومحور التاريخ المستخدم، والشركة/الفرع، واليوميات، والعملات. إن لم تُحدَّد الفترة فاسأل أو استخدم آخر 90 يومًا مع إعلان ذلك.
3. **لا كتابة دون موافقة:** لا إنشاء أو تعديل أو ترحيل أو دفع أو تسوية/مطابقة أو إلغاء أو إرسال رسالة أو إنشاء نشاط أو قفل فترة، إلا بعد عرض: السجلات (أرقامها) والعدد والمبالغ والعملة والأثر المتوقع، ثم موافقة صريحة من المستخدم **على العملية الحالية**. موافقة سابقة لا تمتد لعملية أخرى.
4. **لا تخمين:** لا مطابقة تلقائية عند تعدد الاحتمالات أو غياب الشريك. لا تخمّن حسابًا أو ضريبة أو هوية شريك؛ اعرض الخيارات واسأل.
5. **بعد أي تغيير مصرح به:** أعد قراءة السجلات نفسها وتحقق من `state` والمبلغ والعملة، واعرض أرقام السجلات قبل/بعد.
6. **الخصوصية:** لا تجلب بيانات شخصية غير لازمة (هاتف، بريد، عنوان). استخدم معرّف الشريك واسمه فقط عند الحاجة.
7. **الأرقام القديمة:** `references/current-baseline.md` لقطة مؤرخة، ليست مصدر قرار. أعد القياس من Odoo قبل أي توصية.

## المسارات الستة + استيراد الكشف البنكي
اختر المسار حسب الطلب، وتفاصيل الخطوات في [references/workflows.md](references/workflows.md):

| المسار | متى |
|---|---|
| `accounting-snapshot` | ملخص محاسبي سريع للفترة |
| `overdue-followup` | ذمم متأخرة وتحصيل/سداد |
| `bank-match-suggest` | اقتراح مطابقة أسطر الكشف البنكي |
| `partner-data-quality` | نواقص بيانات الشركاء (VAT، بلد، شريك) |
| `vendor-bill-review` | مراجعة فواتير الموردين قبل الترحيل |
| `period-close-check` | جاهزية إقفال فترة |
| `statement-normalize` | تحويل كشف بنكي (CSV/XLSX/XLS القديم) إلى XLSX قياسي بأعمدة Date/Label/Amount: `normalize_statement_file` محلي بلا Odoo — انظر [references/statement-import.md](references/statement-import.md) |
| `statement-import` | استيراد كشف بنكي: معاينة بلا كتابة ثم proposal فقط (التسوية مرحلة منفصلة) — نفس المرجع |

الضوابط والحدود التفصيلية: [references/controls.md](references/controls.md). لقطة الأرقام (2026-09-29): [references/current-baseline.md](references/current-baseline.md).

## قالب رأس كل تقرير
```
الفترة: <من> → <إلى> (حسب date | invoice_date | invoice_date_due)
الشركة/الفرع: ...   اليوميات: ...   العملات: ...
المصدر: JSON-2 قراءة فقط — لم يتغير أي سجل
```
ثم الأرقام، ثم الملاحظات، ثم "إجراءات مقترحة تحتاج موافقتك" (إن وجدت).
