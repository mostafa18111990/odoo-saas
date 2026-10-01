# odoo-accountant (Claude Code plugin) v0.2.0

موظف محاسب آلي لـ Odoo 19. الدليل الكامل بالعربية: `docs/PLUGIN.md` في المستودع https://github.com/mostafa18111990/odoo-saas.

```
/plugin marketplace add mostafa18111990/odoo-saas
/plugin install odoo-accountant@odoo-saas
```

المطلوب في بيئتك: `ODOO_URL` و`ODOO_DB` و`ODOO_LOGIN` (و`ODOO_API_KEY` اختياري إن لم تتوفر بيانات اعتماد محقونة)، وبايثون 3.10+.
أضف قواعد الصلاحيات من `settings.example.json` إلى إعداداتك (الإضافات لا تحمل صلاحيات): تنفيذ الموافقات يسأل المستخدم دائمًا.
الملف يُولَّد من المصدر بـ `python3 scripts/build_plugin.py` ولا يُعدَّل يدويًا.
