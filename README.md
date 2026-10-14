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

### ملاحظة حول التخزين

التخزين على الطبقة المجانية في Hugging Face **مؤقت**: تُفقد الملفات المرفوعة والتقارير عند
إعادة تشغيل الـ Space أو إعادة بنائه. لبيانات دائمة يُنصح بالترقية إلى تخزين دائم أو خادم VPS.

## التشغيل المحلي

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```
