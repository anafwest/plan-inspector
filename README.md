---
title: Plan Inspector
emoji: 🏗️
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# نظام فحص المخططات الهندسية

نظام تدقيق رخص البناء الذكي — تطبيق FastAPI لفحص المخططات الهندسية وتوليد التقارير.

## النشر على Hugging Face Spaces

هذا التطبيق يُنشر تلقائيًا عبر Dockerfile الموجود في الجذر.

### الإعداد لأول مرة

1. أنشئ Space جديدًا من نوع **Docker** على https://huggingface.co/new-space
2. اربط الـ Space بمستودع GitHub هذا (أو ارفع الملفات مباشرة).
3. من **Settings → Variables and secrets** أضف الأسرار التالية:
   - `OPENAI_API_KEY` (إلزامي)
   - `OPENAI_BASE_URL` (اختياري)
   - `OPENAI_MODEL` (اختياري، الافتراضي `gpt-4o`)

### التخزين الدائم

يدعم التطبيق متغيّر البيئة `DATA_DIR` لتحديد مجلد البيانات (المرفوعات والتقارير).

- على Hugging Face Spaces: فعّل **Persistent Storage** من إعدادات الـ Space، وسيُركّب تلقائيًا على
  `/data`، وسيكتشفه التطبيق ويخزّن البيانات هناك تلقائيًا (بدون ضبط شيء).
- على التوافقية الأخرى: اضبط `DATA_DIR` على مسار قرص دائم (مثال `/data`).

بدون تخزين دائم (الطبقة المجانية) تُفقد الملفات والتقارير عند إعادة التشغيل أو البناء.


## التشغيل المحلي

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```
