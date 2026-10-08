import asyncio
import html
import json
import os
import shutil
import threading
import winreg
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from uuid import uuid4


def _ensure_openai_env() -> None:
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "INSPECTOR_DEBUG"):
        if os.environ.get(name):
            continue
        for scope in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(scope, r"Environment") as key:
                    value, _ = winreg.QueryValueEx(key, name)
                os.environ[name] = value
                break
            except OSError:
                continue


_ensure_openai_env()

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

import inspector
import spatial
from inspector import ANNOTATED_DIR

app = FastAPI()

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_FILES = 10
UPLOAD_DIR = "uploads"

# أنواع المخططات المدعومة: ملفات CAD، ونماذج ثلاثية الأبعاد، وصور مسح/رفع
SUPPORTED_TYPES: dict[str, str] = {
    ".pdf": "مخطط PDF",
    ".dwg": "رسم CAD (AutoCAD)",
    ".dxf": "تبادل CAD (DXF)",
    ".dgn": "رسم MicroStation",
    ".rvt": "نموذج ثلاثي الأبعاد (Revit)",
    ".ifc": "نموذج IFC/BIM",
    ".skp": "نموذج SketchUp",
    ".jpg": "صورة مخطط",
    ".jpeg": "صورة مخطط",
    ".png": "صورة مخطط",
    ".bmp": "صورة مخطط",
    ".tif": "صورة مسح ضوئي",
    ".tiff": "صورة مسح ضوئي",
    ".webp": "صورة مخطط",
}

ALLOWED_EXTENSIONS = set(SUPPORTED_TYPES)

# قاعدة بيانات وهمية لتخزين الاشتراطات والأكواد البلدية (يمكن ربطها بقاعدة بيانات حقيقية لاحقاً مثل PostgreSQL)
municipal_codes_db = [
    {"id": 1, "category": "سكني", "rule_name": "الارتداد الأمامي جهة الشارع (خمس عرض الشارع)", "min_value": 3.0, "max_value": None, "unit": "متر"},
    {"id": 2, "category": "سكني", "rule_name": "الارتداد الجانبي/الخلفي جهة الجار", "min_value": 2.0, "max_value": None, "unit": "متر"},
    {"id": 3, "category": "سكني", "rule_name": "نسبة البناء القصوى للدور الأرضي", "min_value": None, "max_value": 60.0, "unit": "%"},
    {"id": 4, "category": "سكني", "rule_name": "نسبة البناء القصوى للدور الأول", "min_value": None, "max_value": 65.0, "unit": "%"},
    {"id": 5, "category": "سكني", "rule_name": "نسبة الملحق العلوي (من مساحة الدور الأول)", "min_value": None, "max_value": 50.0, "unit": "%"},
    {"id": 6, "category": "سكني", "rule_name": "ارتداد الملحق العلوي عن الشارع", "min_value": 2.0, "max_value": None, "unit": "متر"},
]

WADI_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wadi_hanifa_rules.json")
IMPORTED_RULE_BASE_ID = 1000


def load_wadi_requirements() -> List[dict]:
    if not os.path.exists(WADI_RULES_PATH):
        return []
    try:
        with open(WADI_RULES_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    return payload.get("requirements", [])


wadi_requirements: List[dict] = load_wadi_requirements()
wadi_meta = {
    "total": len(wadi_requirements),
    "sections": sorted({r.get("section") or "بدون قسم" for r in wadi_requirements}),
    "projects": sorted(
        {p for r in wadi_requirements for p in r.get("project", []) if isinstance(p, str)}
    ),
    "categories": sorted(
        {
            (r.get("category") or "").strip()
            for r in wadi_requirements
            if isinstance(r.get("category"), str) and r.get("category")
        }
    ),
    "numeric": sum(
        1
        for r in wadi_requirements
        if r.get("min_value") is not None or r.get("max_value") is not None
    ),
}

CUSTOM_RULE_BASE_ID = 1


def custom_rule_id(rule: dict) -> int:
    return int(rule.get("id", 0))


def imported_rule_id(request: dict) -> int:
    return int(request.get("_seq_id", 0))


def prepare_imported_rule(request: dict) -> dict:
    entry = {
        "id": int(request.get("_seq_id", 0) + IMPORTED_RULE_BASE_ID),
        "rule_name": request.get("text", ""),
        "category": f"{request.get('category') or 'عام'} · {request.get('section') or 'بدون قسم'}",
        "unit": request.get("unit") or "متر",
        "min_value": request.get("min_value"),
        "max_value": request.get("max_value"),
        "concepts": request.get("concepts") or [],
        "ref": request.get("ref", ""),
        "section": request.get("section") or "",
        "also_in": request.get("also_in") or [],
    }
    return entry


def prepare_imported_rules(section: str = "", classification: str = "") -> List[dict]:
    prepared = []
    for index, request in enumerate(wadi_requirements):
        request["_seq_id"] = index
        req_section = request.get("section") or ""
        also = request.get("also_in") or []

        if section and section != "الكل":
            if req_section != section and section not in also:
                continue

        if classification and classification != "الكل":
            classifications = request.get("classification") or request.get("code") or []
            labels = set(classifications)
            if request.get("categories"):
                labels.update(request.get("categories", []))
            if not any(classification == label for label in labels):
                continue

        prepared.append(prepare_imported_rule(request))

    return prepared


def rules_for_inspection(section: str = "", classification: str = "") -> List[dict]:
    rules = []
    for rule in municipal_codes_db:
        rules.append(dict(rule))
    rules.extend(prepare_imported_rules(section, classification))
    return rules


# تقارير الفحص المؤقتة (تُخزَّن في الذاكرة، يمكن استبدالها بقاعدة بيانات لاحقاً)
reports: Dict[str, dict] = {}

jobs: Dict[str, dict] = {}

REPORTS_STORE = os.path.join(UPLOAD_DIR, "reports_store.json")


def save_reports() -> None:
    try:
        payload = {}
        for report_id, entry in reports.items():
            if entry.get("pending"):
                continue
            saved = dict(entry)
            checks = saved.get("checks")
            if checks:
                saved["checks"] = [
                    asdict(check) if isinstance(check, inspector.Check) else check
                    for check in checks
                ]
            payload[report_id] = saved
        with open(REPORTS_STORE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except Exception:
        pass


def load_reports() -> None:
    if not os.path.exists(REPORTS_STORE):
        return
    try:
        with open(REPORTS_STORE, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return
    for report_id, entry in payload.items():
        try:
            entry["checks"] = [
                inspector.Check(**check) for check in (entry.get("checks") or [])
            ]
            if str(entry.get("assistant_status") or "") in ("queued", "pending", "running"):
                entry["assistant_status"] = "unavailable"
                entry["assistant_error"] = (
                    "انقطعت مراجعة المساعد أثناء إعادة تشغيل الخادم — استخدم زر إعادة المراجعة."
                )
            reports[report_id] = entry
        except (TypeError, ValueError):
            continue


load_reports()


def lan_host() -> str:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


SHARE_HOST = lan_host()

os.makedirs(ANNOTATED_DIR, exist_ok=True)
app.mount("/reports", StaticFiles(directory=ANNOTATED_DIR), name="reports")


class RuleSchema(BaseModel):
    category: str = Field(min_length=2, max_length=60)
    rule_name: str = Field(min_length=2, max_length=120)
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    unit: str = Field(min_length=1, max_length=20)


def rules_list_html() -> str:
    if not municipal_codes_db:
        return '<p class="text-sm text-slate-500 py-2">لا توجد اشتراطات مسجلة حالياً.</p>'

    rows = []
    for rule in municipal_codes_db:
        if rule["max_value"] is None:
            limit = f'>= {rule["min_value"]} {rule["unit"]}'
        else:
            limit = f'<= {rule["max_value"]} {rule["unit"]}'
        rows.append(
            f'<tr class="border-b border-slate-100 last:border-0">'
            f'<td class="py-2 px-1 text-slate-500">{rule["id"]}</td>'
            f'<td class="py-2 px-1 text-slate-800">{rule["category"]}</td>'
            f'<td class="py-2 px-1 text-slate-800">{rule["rule_name"]}</td>'
            f'<td class="py-2 px-1 text-emerald-700 font-medium">{limit}</td>'
            f'</tr>'
        )

    return (
        f'<p class="text-xs text-slate-500 mb-2">عدد الأكواد المفعّلة: '
        f'<span class="font-bold text-emerald-700">{len(municipal_codes_db)}</span></p>'
        f'<table class="w-full text-sm">'
        f'<thead><tr class="border-b border-slate-200 text-slate-500 text-xs">'
        f'<th class="text-right py-2 px-1">#</th>'
        f'<th class="text-right py-2 px-1">التصنيف</th>'
        f'<th class="text-right py-2 px-1">الاشتراط</th>'
        f'<th class="text-right py-2 px-1">الحد</th>'
        f'</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def wadi_limit_text(rule: dict) -> str:
    minimum = rule.get("min_value")
    maximum = rule.get("max_value")
    unit = rule.get("unit") or ""
    if minimum is not None and maximum is not None:
        return f"بين {minimum} و {maximum} {unit}"
    if minimum is not None:
        return f"لا يقل عن {minimum} {unit}"
    if maximum is not None:
        return f"لا يزيد عن {maximum} {unit}"
    return "—"


def wadi_rules_table(requirements: List[dict], start_index: int = 1) -> str:
    if not requirements:
        return '<p class="text-sm text-slate-500 py-3">لا توجد اشتراطات مطابقة.</p>'

    rows = []
    for index, rule in enumerate(requirements, start=start_index):
        section = html.escape(rule.get("section") or "عام")
        category = html.escape(rule.get("category") or "—")
        text = html.escape(rule.get("text") or "")
        ref = html.escape(rule.get("ref") or "—")
        also = rule.get("also_in") or []
        also_html = (
            f'<span class="text-[10px] text-slate-400">أيضاً في {", ".join(html.escape(s) for s in also)}</span>'
            if also
            else ""
        )
        numeric = rule.get("min_value") is not None or rule.get("max_value") is not None
        limit = (
            f'<span class="text-emerald-700 font-medium">{html.escape(wadi_limit_text(rule))}</span>'
            if numeric
            else '<span class="text-slate-400">يدوي</span>'
        )
        rows.append(
            f'<tr class="border-b border-slate-100 last:border-0">'
            f'<td class="py-2 px-1 text-slate-500">{index}</td>'
            f'<td class="py-2 px-1"><span class="bg-slate-100 text-slate-700 text-xs px-2 py-0.5 rounded-full">{section}</span></td>'
            f'<td class="py-2 px-1 text-slate-500 text-xs">{category}</td>'
            f'<td class="py-2 px-1 text-slate-800"><span class="text-sm">{text}</span>{also_html}'
            f'<div class="text-[10px] text-slate-400 mt-0.5">{ref}</div></td>'
            f'<td class="py-2 px-1 text-xs whitespace-nowrap">{limit}</td>'
            f'</tr>'
        )

    return (
        f'<table class="w-full text-sm">'
        f'<thead><tr class="border-b border-slate-200 text-slate-500 text-xs">'
        f'<th class="text-right py-2 px-1">#</th>'
        f'<th class="text-right py-2 px-1">المقطع</th>'
        f'<th class="text-right py-2 px-1">التصنيف</th>'
        f'<th class="text-right py-2 px-1">الاشتراط</th>'
        f'<th class="text-right py-2 px-1">الحد</th>'
        f'</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def filter_wadi_rules(section: str = "", project_type: str = "", search: str = "") -> List[dict]:
    query = ""
    if search:
        query = normalize_search(search)
    results = []
    for rule in wadi_requirements:
        rule_section = rule.get("section") or ""
        if section and rule_section != section:
            continue
        if project_type and project_type not in rule.get("project", []):
            continue
        if query:
            haystack = normalize_search(
                f"{rule.get('text','')} {rule.get('category','')} {rule.get('ref','')}"
            )
            if query not in haystack:
                continue
        results.append(rule)
    return results


def normalize_search(text: str) -> str:
    normalized = ""
    for char in (text or "").strip().lower():
        if "\u0641" <= char <= "\u064a" or char.isdigit() or char in " tm.%":
            normalized += char
    return normalized


def error_html(message: str) -> str:
    return (
        f'<div class="bg-red-50 border border-red-200 text-red-700 text-sm p-3 rounded-lg">'
        f'{message}</div>'
    )


def get_extension(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def allowed_types_text() -> str:
    labels = sorted(set(SUPPORTED_TYPES.values()))
    return "، ".join(labels)


def save_upload(file: UploadFile) -> Tuple[str, str, int]:
    if not file.filename:
        raise ValueError("اسم الملف غير موجود.")

    extension = get_extension(file.filename)
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError(f"صيغة الملف غير مدعومة. الأنواع المسموح بها: {allowed_types_text()}")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_stem = f"{uuid4().hex}_{os.path.splitext(os.path.basename(file.filename))[0][:60]}"
    stored_name = f"{safe_stem}{extension}"
    file_path = os.path.join(UPLOAD_DIR, stored_name)

    size = 0
    try:
        with open(file_path, "wb") as buffer:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise ValueError("حجم الملف يتجاوز الحد المسموح (20 ميجابايت).")
                buffer.write(chunk)
    except Exception:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise

    return file.filename, file_path, size


def layout(page_title: str, active: str, content: str) -> HTMLResponse:
    nav_items = [("/WEST.anaf", "فحص المخططات"), ("/rules", "إدارة الاشتراطات والأكواد")]
    links = ""
    for href, label in nav_items:
        style = "bg-slate-700 text-white" if href == active else "hover:bg-slate-700 text-slate-300"
        links += f'<li><a href="{href}" class="block p-2 rounded {style}">{label}</a></li>'

    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{page_title} - منصة تدقيق رخص البناء الذكية</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://unpkg.com/htmx.org@1.9.12"></script>
    </head>
    <body class="bg-gray-100 font-sans" dir="rtl">
        <div class="flex min-h-[100dvh]">
            <!-- القائمة الجانبية (شاشات كبيرة) -->
            <div class="hidden md:flex w-64 bg-slate-800 text-white p-5 flex-col justify-between shrink-0">
                <div>
                    <h1 class="text-xl font-bold mb-8 text-emerald-400">إدارة رخص البناء</h1>
                    <ul class="space-y-4">{links}</ul>
                </div>
                <div class="text-xs text-slate-400">الإصدار 1.0 - بلدية الرياض</div>
            </div>

            <!-- المحتوى الرئيسي -->
            <div class="flex-1 flex flex-col min-w-0">
                <!-- شريط علوي (جوال) -->
                <nav class="md:hidden bg-slate-800 text-white p-3">
                    <h1 class="text-lg font-bold mb-2 text-emerald-400">إدارة رخص البناء</h1>
                    <ul class="flex flex-wrap gap-2">{links}</ul>
                </nav>
                <header class="bg-white shadow p-4">
                    <h2 class="text-lg font-semibold text-slate-700">{page_title}</h2>
                </header>
                <main class="p-3 sm:p-6 overflow-x-auto">{content}</main>
            </div>
        </div>
    </body>
    </html>
    """)


@app.get("/", include_in_schema=False)
async def home_root():
    return RedirectResponse(url="/WEST.anaf", status_code=307)


@app.get("/WEST.anaf", response_class=HTMLResponse)
async def home():
    section_options = "".join(
        f'<option value="{html.escape(s)}">{html.escape(s)}</option>' for s in wadi_meta["sections"]
    )
    return layout(
        page_title="فحص المخططات الهندسية",
        active="/WEST.anaf",
        content=f"""
        <div class="max-w-3xl space-y-6">
            <div class="bg-white p-6 rounded-xl shadow">
                <h3 class="text-md font-bold mb-1 text-slate-700">رفع المخططات والتقارير الهندسية</h3>
                <p class="text-xs text-slate-500 mb-4">يمكن رفع أكثر من تقرير في المرة الواحدة (حتى {MAX_FILES} ملفات).</p>
                <form hx-post="/upload"
                      hx-target="#upload-result"
                      hx-swap="innerHTML"
                      hx-encoding="multipart/form-data"
                      class="space-y-4">
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <div>
                            <label class="block text-xs text-slate-600 mb-1">موقع المخطط ضمن كود وادي حنيفة</label>
                            <select name="section" class="w-full border p-2 rounded text-sm">
                                <option value="الكل">الكل</option>
                                {section_options}
                            </select>
                        </div>
                        <div>
                            <label class="block text-xs text-slate-600 mb-1">تصنيف الاستخدام</label>
                            <select name="classification" class="w-full border p-2 rounded text-sm">
                                <option value="الكل">الكل</option>
                                <option value="سكني">سكني</option>
                                <option value="تجاري">تجاري</option>
                            </select>
                        </div>
                    </div>
                    <input type="file" id="blueprint-files" name="files" multiple required
                           accept="{",".join(sorted(ALLOWED_EXTENSIONS))}"
                           onchange="showSelectedFiles(this)"
                           class="block w-full text-sm text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-full file:border-0 file:text-sm file:font-semibold file:bg-emerald-50 file:text-emerald-700 hover:file:bg-emerald-100">
                    <ul id="selected-files" class="text-xs text-slate-600 space-y-1"></ul>
                    <button type="submit" class="w-full bg-emerald-600 text-white py-2 rounded-lg hover:bg-emerald-700 transition">تحليل التقارير والمقارنة بالاشتراطات</button>
                </form>
                <div id="upload-result" class="mt-4"></div>
            </div>

            <div class="bg-white p-6 rounded-xl shadow">
                <h3 class="text-md font-bold mb-2 text-slate-700">أنواع المخططات المدعومة</h3>
                <div class="flex flex-wrap gap-2">
                    {"".join(f'<span class="bg-slate-100 text-slate-600 text-xs px-2 py-1 rounded-full">{label}</span>' for label in sorted(set(SUPPORTED_TYPES.values())))}
                </div>
            </div>
        </div>
        <script>
        function showSelectedFiles(input) {{
            const list = document.getElementById('selected-files');
            list.innerHTML = '';
            Array.from(input.files).forEach(f => {{
                const li = document.createElement('li');
                li.textContent = '• ' + f.name;
                list.appendChild(li);
            }});
        }}
        </script>
        """,
    )


@app.get("/rules", response_class=HTMLResponse)
async def rules_page():
    section_options = "".join(
        f'<option value="{html.escape(s)}">{html.escape(s)}</option>' for s in wadi_meta["sections"]
    )
    return layout(
        page_title="إدارة الاشتراطات والأكواد البلدية",
        active="/rules",
        content=f"""
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div class="bg-white p-6 rounded-xl shadow">
                <h3 class="text-md font-bold mb-4 text-slate-700">إضافة / تحديث اشتراط أو كود</h3>
                <form hx-post="/add-rule"
                      hx-target="#rules-list"
                      hx-swap="innerHTML"
                      hx-on::after-request="if (event.detail.successful) this.reset()"
                      class="space-y-3">
                    <div>
                        <label class="block text-xs text-slate-600 mb-1">نوع التصنيف (سكني / تجاري)</label>
                        <input type="text" name="category" placeholder="مثال: سكني" class="w-full border p-2 rounded text-sm" required>
                    </div>
                    <div>
                        <label class="block text-xs text-slate-600 mb-1">اسم الاشتراط أو الكود</label>
                        <input type="text" name="rule_name" placeholder="مثال: الارتداد الأمامي الأقل" class="w-full border p-2 rounded text-sm" required>
                    </div>
                    <div class="grid grid-cols-3 gap-2">
                        <input type="number" step="0.1" name="min_value" placeholder="الحد الأدنى" class="border p-2 rounded text-sm">
                        <input type="number" step="0.1" name="max_value" placeholder="الحد الأقصى" class="border p-2 rounded text-sm">
                        <input type="text" name="unit" placeholder="الوحدة (متر / %)" class="border p-2 rounded text-sm" required>
                    </div>
                    <button type="submit" class="w-full bg-blue-600 text-white py-2 rounded-lg hover:bg-blue-700 transition text-sm">حفظ الكود الجديد</button>
                </form>
                <div id="rules-list" class="mt-4 border-t border-slate-100 pt-4">{rules_list_html()}</div>
            </div>

            <div class="lg:col-span-2 bg-white p-6 rounded-xl shadow">
                <h3 class="text-md font-bold mb-1 text-slate-700">اشتراطات كود وادي حنيفة المستوردة</h3>
                <p class="text-xs text-slate-500 mb-4">
                    إجمالي {wadi_meta['total']} اشتراطًا — {wadi_meta['numeric']} بندًا بحد رقمي قابل للفحص الآلي.
                </p>

                <form hx-get="/rules/list"
                      hx-target="#wadi-rules-result"
                      hx-push-url="false"
                      class="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-4"
                      hx-trigger="change, submit from:find(input[name=search])">
                    <div>
                        <label class="block text-xs text-slate-600 mb-1">المقطع</label>
                        <select name="section" class="w-full border p-2 rounded text-sm">
                            <option value="">الكل</option>
                            {section_options}
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs text-slate-600 mb-1">نوع المشروع</label>
                        <select name="project" class="w-full border p-2 rounded text-sm">
                            <option value="">الكل</option>
                            {"".join(f'<option value="{html.escape(p)}">{html.escape(p)}</option>' for p in wadi_meta["projects"])}
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs text-slate-600 mb-1">بحث</label>
                        <input type="search" name="search" placeholder="ابحث في نص الاشتراط..." class="w-full border p-2 rounded text-sm">
                    </div>
                </form>

                <div id="wadi-rules-result">{wadi_rules_table(wadi_requirements[:50], start_index=1)}</div>
            </div>
        </div>
        """,
    )


@app.get("/rules/list", response_class=HTMLResponse)
async def rules_list(
    section: str = "",
    project: str = "",
    search: str = "",
):
    results = filter_wadi_rules(section=section, project_type=project, search=search)
    return HTMLResponse(wadi_rules_table(results, start_index=1))


@app.post("/add-rule", response_class=HTMLResponse)
async def add_rule(
    category: str = Form(""),
    rule_name: str = Form(""),
    unit: str = Form(""),
    min_value: Optional[float] = Form(None),
    max_value: Optional[float] = Form(None),
):
    payload = {
        "category": category.strip(),
        "rule_name": rule_name.strip(),
        "min_value": min_value,
        "max_value": max_value,
        "unit": unit.strip(),
    }

    try:
        rule = RuleSchema(**payload)
    except ValidationError:
        return HTMLResponse(error_html("تعذّر الحفظ: تأكد من إدخال التصنيف واسم الاشتراط والوحدة بشكل صحيح."))

    if rule.min_value is None and rule.max_value is None:
        return HTMLResponse(error_html("تعذّر الحفظ: يجب إدخال الحد الأدنى أو الحد الأقصى على الأقل."))

    if rule.min_value is not None and rule.max_value is not None and rule.min_value > rule.max_value:
        return HTMLResponse(error_html("تعذّر الحفظ: الحد الأدنى أكبر من الحد الأقصى."))

    existing = next(
        (
            r
            for r in municipal_codes_db
            if r["category"] == rule.category and r["rule_name"] == rule.rule_name
        ),
        None,
    )

    if existing:
        existing["min_value"] = rule.min_value
        existing["max_value"] = rule.max_value
        existing["unit"] = rule.unit
    else:
        next_id = max((r["id"] for r in municipal_codes_db), default=0) + 1
        municipal_codes_db.append({"id": next_id, **rule.model_dump()})

    return HTMLResponse(rules_list_html())


def _spatial_limits() -> dict:
    limits = dict(spatial.DEFAULT_LIMITS)
    for rule in municipal_codes_db:
        name = str(rule.get("rule_name", ""))
        min_value = rule.get("min_value")
        max_value = rule.get("max_value")
        if min_value is not None and ("أمامي" in name or "امامي" in name):
            limits["front_min"] = min_value
        elif min_value is not None and ("جانبي" in name or "خلفي" in name):
            limits["side_rear_min"] = min_value
        if max_value is not None and "نسبة" in name and "أرضي" in name:
            limits["coverage_gf_max"] = max_value
    return limits


@app.post("/api/v3/audit-blueprint")
async def audit_blueprint(file: UploadFile = File(...)):
    filename = file.filename or ""
    if not filename.lower().endswith(".dxf"):
        raise HTTPException(status_code=400, detail="يرجى رفع ملف هندسي بصيغة DXF حصراً.")

    file_path = os.path.join(UPLOAD_DIR, f"{uuid4().hex[:8]}_{os.path.basename(filename)}")
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        report = spatial.audit_dxf(file_path, limits=_spatial_limits())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"فشل في الفحص الهندسي: {exc}") from exc

    report["platform_metadata"]["file"] = os.path.basename(file_path)
    return JSONResponse(content=report)


@app.post("/audit-plan/")
async def audit_plan(file: UploadFile = File(...)):
    """تدقيق بصري للمخالفات الحرجة: يعيد ملاحظات JSON وصوراً محدّدة بالدوائر."""
    try:
        original_name, file_path, _ = save_upload(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    extension = get_extension(original_name)

    def run_audit() -> Optional[dict]:
        return inspector.audit_critical(file_path, extension)

    try:
        result = await asyncio.to_thread(run_audit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"فشل التدقيق البصري: {exc}") from exc

    if result is None:
        raise HTTPException(
            status_code=500,
            detail="تعذّر التحليل البصري: تأكد من صيغة الملف (PDF أو صورة) ومفتاح الواجهة.",
        )

    return JSONResponse(
        content={
            "file_name": original_name,
            "total_pages": len(result["pages"]),
            "model": result.get("_model"),
            "pages_audit": result["pages"],
        }
    )


STATUS_STYLES = {
    "pass": ("مطابق", "bg-emerald-100 text-emerald-800 border-emerald-200"),
    "fail": ("غير مطابق", "bg-red-100 text-red-700 border-red-200"),
    "unknown": ("يتطلب مراجعة", "bg-amber-100 text-amber-800 border-amber-200"),
    "na": ("غير منطبق", "bg-slate-100 text-slate-600 border-slate-200"),
}

SOURCE_LABELS = {"auto": "قراءة آلية", "vision": "رؤية ذكية", "manual": "إدخال يدوي", "none": "—"}

PRINT_CSS = """
<style>
@media print {
  .no-print { display: none !important; }
  body { background: white !important; }
  .print-card { box-shadow: none !important; border: 1px solid #cbd5e1; }
  .print-page { break-after: page; }
}
</style>
"""


def check_row(check: inspector.Check, index: int, report_id: str) -> str:
    label, badge = STATUS_STYLES[check.status]
    measured = f"{check.measured} {check.unit}" if check.measured is not None else "—"

    row_bg = {
        "pass": "",
        "fail": "bg-red-50/60",
        "unknown": "bg-amber-50/60",
        "na": "bg-slate-50/60",
    }[check.status]

    if check.status == "unknown":
        reading_cell = f"""
        <div class="flex items-center gap-1 no-print">
            <input id="value-{check.rule_id}" type="number" step="0.1"
                   class="w-20 border rounded px-1 py-0.5 text-xs"
                   placeholder="أدخل القياس">
            <button class="bg-amber-500 text-white text-xs px-2 py-1 rounded"
                    hx-post="/report/{report_id}/reading"
                    hx-target="#row-{check.rule_id}"
                    hx-swap="outerHTML"
                    hx-vals='{{"rule_id": "{check.rule_id}"}}'
                    hx-include="#value-{check.rule_id}">تحديث</button>
        </div>
        """
    else:
        reading_cell = f'<span class="font-medium text-slate-700">{measured}</span>'

    marker_text = (
        f'<span class="text-xs text-red-600 font-bold"> (الدائرة رقم {index})</span>'
        if check.marker and check.status == "fail"
        else ""
    )

    assistant_alert = ""
    if getattr(check, "assistant_note", ""):
        assistant_alert = (
            f'<span class="block text-[10px] text-sky-700 mt-1">'
            f'{html.escape(check.assistant_note)}</span>'
        )

    return f"""
    <tr id="row-{check.rule_id}" class="border-b border-slate-100 last:border-0 {row_bg}">
        <td class="py-2 px-2 text-slate-500">{index}</td>
        <td class="py-2 px-2 text-slate-800">{html.escape(check.rule_name)}{marker_text}</td>
        <td class="py-2 px-2 text-slate-500">{html.escape(check.category)}</td>
        <td class="py-2 px-2 text-slate-500">{html.escape(check.limit_text)}</td>
        <td class="py-2 px-2">{reading_cell}
            <span class="text-[10px] text-slate-400 block">{SOURCE_LABELS[check.source]}</span>{assistant_alert}</td>
        <td class="py-2 px-2"><span class="text-xs px-2 py-1 rounded-full border {badge}">{label}</span></td>
    </tr>
    """


def check_table(checks: List[inspector.Check], report_id: str) -> str:
    rows = "".join(check_row(check, index, report_id) for index, check in enumerate(checks, start=1))
    return f"""
    <table class="w-full text-sm">
        <thead class="bg-slate-100 text-slate-600 text-xs">
            <tr>
                <th class="text-right py-2 px-2">#</th>
                <th class="text-right py-2 px-2">الاشتراط</th>
                <th class="text-right py-2 px-2">التصنيف</th>
                <th class="text-right py-2 px-2">الحد المطلوب</th>
                <th class="text-right py-2 px-2">القياس من المخطط</th>
                <th class="text-right py-2 px-2">الحالة</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
    """


def classification_badge_html(report: dict) -> str:
    detected = report.get("classification_detected")
    if not detected or detected == "غير محدد":
        return ""
    color = "bg-blue-100 text-blue-800 border-blue-200"
    label = f"تحديد الاستخدام بالذكاء الاصطناعي: {html.escape(detected)}"
    return f"""
    <div class="mb-4">
        <span class="text-xs px-3 py-1 rounded-full border font-medium inline-block {color}">{label}</span>
    </div>
    """


ASSISTANT_LINKS = [
    ("ارتداد",),
    ("نسبة البناء", "نسبة الاشغال"),
    ("مواقف",),
    ("ارتفاع",),
    ("ملحق",),
    ("واجهة",),
    ("مساحة",),
    ("حديقة", "منسوب"),
]
ASSISTANT_LINKS_NORM = [
    tuple(inspector.normalize(keyword) for keyword in group) for group in ASSISTANT_LINKS
]


def _assistant_worker(
    report_id: str,
    file_path: str,
    extension: str,
    drawing: Optional[inspector.Drawing] = None,
) -> None:
    report = reports.get(report_id)
    if not report:
        return
    try:
        report["assistant_status"] = "running"
        audit = inspector.audit_critical(file_path, extension, drawing)
        if audit:
            report["assistant"] = audit
            report["assistant_model"] = audit.get("_model") or ""
            _link_assistant_alerts(report, audit)
            report["assistant_status"] = "done"
        else:
            report["assistant_status"] = "unavailable"
            report["assistant_error"] = (
                "لم يتمكّن المساعد من مراجعة هذا الملف (صيغة غير مدعومة أو تعذّر الاتصال بنموذج الرؤية)."
            )
    except Exception as exc:
        report["assistant_status"] = "error"
        report["assistant_error"] = str(exc)
    finally:
        save_reports()


def _link_assistant_alerts(report: dict, audit: dict) -> None:
    """يربط ملاحظات المساعد بالاشتراطات كملاحظة جانبية دون تغيير الحالة."""
    checks = report.get("checks") or []
    if not checks:
        return

    links: List[dict] = []
    seen = set()
    for page in audit.get("pages") or []:
        for error in page.get("errors") or []:
            text = str(error.get("text") or "").strip()
            if not text:
                continue
            norm_text = inspector.normalize(text)
            for check in checks:
                if not isinstance(check, inspector.Check):
                    continue
                norm_rule = inspector.normalize(check.rule_name)
                matched = any(
                    keyword in norm_text and keyword in norm_rule
                    for group in ASSISTANT_LINKS_NORM
                    for keyword in group
                )
                if not matched:
                    continue
                note = "تنبيه المساعد: " + text[:160]
                if note not in (check.assistant_note or ""):
                    check.assistant_note = (
                        note if not check.assistant_note else f"{check.assistant_note}؛ {note}"
                    )
                key = (check.rule_id, text[:80])
                if key not in seen and len(links) < 12:
                    seen.add(key)
                    links.append(
                        {
                            "rule_id": check.rule_id,
                            "rule_name": check.rule_name,
                            "status": check.status,
                            "text": text,
                        }
                    )
    report["assistant_links"] = links


def assistant_pending_html(report_id: str, delay: int = 3) -> str:
    return f"""
    <div id="assistant-section" class="bg-white p-4 sm:p-6 rounded-xl shadow print-card no-print"
         hx-get="/report/{report_id}/assistant" hx-trigger="load delay:{delay}s" hx-swap="outerHTML">
        <div class="flex items-center gap-3">
            <span class="inline-block w-4 h-4 border-2 border-sky-600 border-t-transparent rounded-full animate-spin shrink-0"></span>
            <div>
                <h3 class="text-sm font-bold text-slate-700">مساعد Gemini يراجع المخطط بصريًا...</h3>
                <p class="text-xs text-slate-500">ملاحظات المساعد تظهر هنا كمرجع إضافي ولا تغيّر نتائج الفحص الأساسية.</p>
            </div>
        </div>
    </div>
    """


def assistant_done_html(report: dict) -> str:
    status = str(report.get("assistant_status") or "unavailable")
    audit = report.get("assistant") or {}
    pages = audit.get("pages") or []
    model = report.get("assistant_model") or audit.get("_model") or ""

    rerun_html = ""
    file_path = report.get("file_path") or ""
    if file_path and os.path.exists(file_path):
        rerun_html = (
            f'<button hx-post="/report/{report["id"]}/assistant/run" '
            f'hx-target="#assistant-section" hx-swap="outerHTML" '
            f'class="bg-sky-600 text-white text-xs px-3 py-1.5 rounded-lg hover:bg-sky-700">'
            f'إعادة مراجعة المساعد</button>'
        )

    if status in ("pending", "running", "queued"):
        return assistant_pending_html(report["id"], delay=3)

    if status != "done" or not pages:
        message = report.get("assistant_error") or "لم تتوفر ملاحظات من المساعد لهذا الملف."
        body = f'<p class="text-xs text-slate-500">{html.escape(str(message))}</p>'
    else:
        pages_html = ""
        for page in pages:
            page_status = str(page.get("status") or "")
            badge_class, badge_text = {
                "approved": ("bg-emerald-100 text-emerald-700", "لا ملاحظات"),
                "rejected": ("bg-red-100 text-red-700", "توجد ملاحظات"),
            }.get(page_status, ("bg-amber-100 text-amber-700", "غير مؤكد"))

            error_items = ""
            for error in page.get("errors") or []:
                kind = str(error.get("type") or "critical")
                tag_class = "bg-red-100 text-red-700" if kind == "critical" else "bg-amber-100 text-amber-700"
                tag_text = "حرج" if kind == "critical" else "تحذير"
                error_items += (
                    f'<li class="flex items-start gap-2 py-1">'
                    f'<span class="text-[10px] px-1.5 py-0.5 rounded shrink-0 mt-0.5 {tag_class}">{tag_text}</span>'
                    f'<span class="text-xs text-slate-700">{html.escape(str(error.get("text") or ""))}</span>'
                    f"</li>"
                )
            if not error_items:
                error_items = '<li class="py-1 text-xs text-slate-500">لا توجد ملاحظات على هذه الصفحة.</li>'

            image_html = ""
            image_url = str(page.get("annotated_image_url") or "")
            if image_url:
                image_html = (
                    f'<details class="mt-2"><summary class="text-xs text-sky-700 cursor-pointer">'
                    f'عرض الصورة المحدّدة بالدوائر</summary>'
                    f'<img src="{html.escape(image_url)}" loading="lazy" class="mt-2 w-full border border-slate-200 rounded-lg">'
                    f"</details>"
                )

            pages_html += f"""
            <div class="border border-slate-200 rounded-lg p-3">
                <div class="flex items-center justify-between gap-2 mb-1">
                    <span class="text-xs font-bold text-slate-700">صفحة {page.get("page_number")}</span>
                    <span class="text-[10px] px-2 py-0.5 rounded-full {badge_class}">{badge_text}</span>
                </div>
                <p class="text-xs text-slate-600 mb-1">{html.escape(str(page.get("summary") or ""))}</p>
                <ul class="divide-y divide-slate-100">{error_items}</ul>
                {image_html}
            </div>
            """

        links_html = ""
        links = report.get("assistant_links") or []
        if links:
            items = "".join(
                f'<li class="py-1 border-b border-slate-100 last:border-0 text-xs">'
                f'<span class="font-medium text-slate-700">{html.escape(str(link.get("rule_name") or ""))}</span>'
                f'<span class="text-slate-500"> — {html.escape(str(link.get("text") or ""))}</span>'
                f"</li>"
                for link in links
            )
            links_html = f"""
            <div class="mt-3 bg-slate-50 border border-slate-200 rounded-lg p-3">
                <h4 class="text-xs font-bold text-slate-700 mb-1">ربط المساعد بالاشتراطات (كملاحظة)</h4>
                <ul>{items}</ul>
            </div>
            """

        body = f'<div class="space-y-3">{pages_html}</div>{links_html}'

    model_html = (
        f'<span class="text-[10px] text-slate-400">النموذج: {html.escape(model)}</span>'
        if model
        else ""
    )
    return f"""
    <div id="assistant-section" class="bg-white p-4 sm:p-6 rounded-xl shadow print-card">
        <div class="flex flex-wrap items-center justify-between gap-2 mb-1">
            <h3 class="text-md font-bold text-slate-700">مساعد Gemini — مراجعة بصرية إضافية</h3>
            <div class="flex items-center gap-2 no-print">{rerun_html}</div>
        </div>
        <p class="text-xs text-slate-500 mb-3">مرجع إضافي لا يعتمد كقرار نهائي: الحالات في جدول الفحص أعلاه هي المرجع، وتنبيهات المساعد تُعرض هنا فقط.</p>
        {model_html}
        {body}
    </div>
    """


def assistant_block(report: dict) -> str:
    status = str(report.get("assistant_status") or "unavailable")
    if status in ("queued", "pending", "running"):
        return assistant_pending_html(report["id"], delay=3)
    return assistant_done_html(report)


def report_content(report: dict) -> str:
    checks: List[inspector.Check] = report["checks"]
    passed = [c for c in checks if c.status == "pass"]
    failed = [c for c in checks if c.status == "fail"]
    unknown = [c for c in checks if c.status == "unknown"]
    na = [c for c in checks if c.status == "na"]

    def status_block(title: str, items: List[inspector.Check], card: str, text: str) -> str:
        if not items:
            return f"""
            <div class="bg-white p-4 rounded-xl shadow print-card">
                <h4 class="text-sm font-bold mb-2 {text}">{title}</h4>
                <p class="text-xs text-slate-400">لا توجد عناصر.</p>
            </div>
            """
        lines = "".join(
            f'<li class="py-1 border-b border-slate-100 last:border-0">'
            f'<span class="font-medium">{html.escape(c.rule_name)}</span> — '
            + (
                f'<span class="text-slate-500">السبب: {html.escape(c.note or "—")}</span>'
                if c.status == "na"
                else
                f'<span class="text-slate-500">المطلوب: {html.escape(c.limit_text)}، '
                f'القياس: {c.measured if c.measured is not None else "—"} {html.escape(c.unit)}</span>'
            )
            + '</li>'
            for c in items
        )
        return f"""
        <div class="{card} p-4 rounded-xl shadow print-card">
            <h4 class="text-sm font-bold mb-2">{title} ({len(items)})</h4>
            <ul class="text-xs text-slate-700">{lines}</ul>
        </div>
        """

    images_html = ""
    if report["images"]:
        pages_html = "".join(
            f'<div class="print-page mb-6">'
            f'<h4 class="text-sm font-bold text-slate-700 mb-2">{html.escape(label)}</h4>'
            f'<img src="/reports/{image}" class="w-full border border-slate-200 rounded-lg">'
            f'</div>'
            for label, image in report["images"]
        )
        images_html = f"""
        <div class="bg-white p-6 rounded-xl shadow print-card">
            <h3 class="text-md font-bold mb-1 text-slate-700">المخطط بعد وضع العلامات</h3>
            <p class="text-xs text-slate-500 mb-4">الدوائر الحمراء تدل على مواضع المخالفات، والأرقام داخلها مطابقة لأرقام الجدول.</p>
            {pages_html}
        </div>
        """

    note_html = (
        f'<div class="bg-amber-50 border border-amber-200 text-amber-800 text-sm p-3 rounded-lg">'
        f'{html.escape(report["drawing_note"])}</div>'
        if report["drawing_note"]
        else ""
    )

    scope_html = ""
    if report.get("applicable_note"):
        scope_html = (
            f'<div class="bg-blue-50 border border-blue-200 text-blue-800 text-xs p-3 rounded-lg mt-3">'
            f'{html.escape(report["applicable_note"])}</div>'
        )
    if report.get("vision_warning"):
        scope_html += (
            f'<div class="bg-amber-50 border border-amber-300 text-amber-800 text-xs p-3 rounded-lg mt-3">'
            f'{html.escape(report["vision_warning"])}</div>'
        )

    manual_html = ""
    text_rules = report.get("text_rules") or []
    if text_rules:
        manual_items = "".join(
            f'<li class="py-1 border-b border-slate-100 last:border-0">'
            f'<input type="checkbox" class="ml-2"> '
            f'<span class="font-medium">{html.escape(rule["rule_name"])}</span>'
            f'<div class="text-[10px] text-slate-400">{html.escape(rule["category"])} · {html.escape(rule.get("ref") or "—")}</div>'
            f'</li>'
            for rule in text_rules
        )
        manual_html = f"""
        <div class="bg-white p-6 rounded-xl shadow print-card no-print">
            <details>
                <summary class="text-sm font-bold text-slate-700 cursor-pointer">
                    قائمة اشتراطات القسم للمراجعة اليدوية ({len(text_rules)})
                </summary>
                <ul class="text-xs text-slate-700 mt-2 max-h-80 overflow-y-auto">{manual_items}</ul>
            </details>
        </div>
        """

    batch_html = ""
    if report.get("batch"):
        links = "".join(
            f'<li class="py-1 border-b border-slate-100 last:border-0">'
            f'<a href="/report/{item["id"]}" class="text-emerald-700 hover:underline">'
            f'{html.escape(item["filename"])}</a></li>'
            for item in report["batch"]
        )
        batch_html = f"""
        <div class="bg-white p-4 rounded-xl shadow print-card no-print">
            <h4 class="text-sm font-bold mb-2 text-slate-700">تقارير أخرى في نفس الدفعة</h4>
            <ul class="text-xs">{links}</ul>
        </div>
        """

    return f"""
    <div class="space-y-4 sm:space-y-6 w-full">
        <div class="bg-white p-4 sm:p-6 rounded-xl shadow print-card">
            <div class="flex flex-wrap justify-between items-center gap-3 mb-4">
                <div>
                    <h3 class="text-md font-bold text-slate-700">تقرير فحص المخطط الهندسي</h3>
                    <p class="text-xs text-slate-500 mt-1">الملف: {html.escape(report["filename"])} — {html.escape(report["file_type"])}</p>
                    <p class="text-xs text-slate-500">تاريخ الفحص: {html.escape(report["created_at"])} — {html.escape(report.get("engine_label") or "")}</p>
                </div>
                <div class="flex flex-wrap gap-2 no-print">
                    <button onclick="copyShare(this)" class="bg-emerald-600 text-white text-sm px-4 py-2 rounded-lg hover:bg-emerald-700">نسخ رابط المشاركة</button>
                    <button onclick="shareWhatsApp()" class="bg-green-600 text-white text-sm px-4 py-2 rounded-lg hover:bg-green-700">مشاركة واتساب</button>
                    <button onclick="window.print()" class="bg-slate-700 text-white text-sm px-4 py-2 rounded-lg hover:bg-slate-800">طباعة / تصدير PDF</button>
                    <a href="/WEST.anaf" class="bg-slate-500 text-white text-sm px-4 py-2 rounded-lg hover:bg-slate-600">فحص مخطط آخر</a>
                </div>
            </div>

            {classification_badge_html(report)}

            <div class="grid grid-cols-2 lg:grid-cols-4 gap-3 text-center">
                <div class="bg-emerald-50 border border-emerald-200 rounded-lg p-3">
                    <p class="text-2xl font-bold text-emerald-700">{len(passed)}</p>
                    <p class="text-xs text-slate-600 mt-1">نقاط مطابقة</p>
                </div>
                <div class="bg-red-50 border border-red-200 rounded-lg p-3">
                    <p class="text-2xl font-bold text-red-600">{len(failed)}</p>
                    <p class="text-xs text-slate-600 mt-1">نقاط غير مطابقة</p>
                </div>
                <div class="bg-amber-50 border border-amber-200 rounded-lg p-3">
                    <p class="text-2xl font-bold text-amber-600">{len(unknown)}</p>
                    <p class="text-xs text-slate-600 mt-1">تحتاج مراجعة</p>
                </div>
                <div class="bg-slate-100 border border-slate-200 rounded-lg p-3">
                    <p class="text-2xl font-bold text-slate-600">{len(na)}</p>
                    <p class="text-xs text-slate-600 mt-1">غير منطبق</p>
                </div>
            </div>
            {note_html}
            {scope_html}
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
            {status_block("النقاط المطابقة للاشتراطات", passed, "bg-white", "text-emerald-700")}
            {status_block("النقاط غير المطابقة (المخالفات)", failed, "bg-red-50 border border-red-200", "text-red-700")}
            {status_block("اشتراطات غير منطبقة على المخطط", na, "bg-slate-50 border border-slate-200", "text-slate-600")}
        </div>

        {assistant_block(report)}

        {manual_html}

        <div class="bg-white p-4 sm:p-6 rounded-xl shadow print-card">
            <h3 class="text-md font-bold mb-3 text-slate-700">التفاصيل الكاملة</h3>
            <div class="overflow-x-auto -mx-1 px-1">
                {check_table(checks, report["id"])}
            </div>
        </div>

        {images_html}
        {batch_html}
    </div>
    """


@app.post("/upload", response_class=HTMLResponse)
async def upload_blueprints(
    files: List[UploadFile] = File(...),
    section: str = Form("الكل"),
    classification: str = Form("الكل"),
):
    if len(files) > MAX_FILES:
        return HTMLResponse(error_html(f"يمكن رفع {MAX_FILES} ملفات كحد أقصى في المرة الواحدة."))

    errors: List[str] = []
    created: List[str] = []

    for file in files:
        try:
            original_name, file_path, _ = save_upload(file)
        except ValueError as exc:
            errors.append(f"{file.filename or 'ملف بدون اسم'}: {exc}")
            continue
        created.append(build_report(original_name, file_path, section, classification))

    if not created:
        return HTMLResponse(error_html("تعذّر رفع أي من الملفات: " + " | ".join(errors)))

    first_id = created[0]
    if len(created) > 1:
        reports[first_id]["batch"] = [
            {"id": report_id, "filename": reports[report_id]["filename"]}
            for report_id in created[1:]
        ]
        save_reports()

    response = HTMLResponse("")
    response.headers["HX-Redirect"] = f"/report/{first_id}"
    return response


def build_report(filename: str, file_path: str, section: str = "الكل", classification: str = "الكل") -> str:
    report_id = inspector.new_report_id()
    reports[report_id] = {
        "id": report_id,
        "filename": filename,
        "file_path": file_path,
        "extension": get_extension(filename),
        "file_type": SUPPORTED_TYPES.get(get_extension(filename), get_extension(filename)),
        "section": "جميع الأقسام" if section in ("", "الكل") else section,
        "classification": "بدون تصنيف" if classification in ("", "الكل") else classification,
        "pending": True,
        "assistant_status": "queued",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    thread = threading.Thread(
        target=_inspect_report_worker,
        args=(report_id, filename, file_path, section, classification),
        daemon=True,
    )
    thread.start()
    return report_id


def _inspect_report_worker(
    report_id: str,
    original_name: str,
    file_path: str,
    section: str,
    classification: str,
) -> None:
    try:
        jobs[report_id] = {"message": "استخراج النصوص والأبعاد من المخطط..."}
        extension = get_extension(original_name)
        drawing = inspector.extract(file_path, extension)

        jobs[report_id] = {"message": "الذكاء الاصطناعي يقرأ المخطط ويقيس الاشتراطات..."}
        prepared = prepare_imported_rules(section, classification)
        applied_rules = [dict(rule) for rule in municipal_codes_db] + prepared
        checks, ai_result = inspector.run_checks(applied_rules, drawing, file_path, extension, {})

        check_ids = {c.rule_id for c in checks}
        text_rules = [
            {
                "id": rule["id"],
                "rule_name": rule["rule_name"],
                "category": rule["category"],
                "ref": rule.get("ref", ""),
            }
            for rule in prepared
            if rule["id"] not in check_ids
        ]

        jobs[report_id] = {"message": "تجهيز الصور ووضع علامات النتائج..."}
        images = inspector.render_pages(drawing, file_path, checks, uuid4().hex[:8])

        labels = {"pdf": "صفحة", "dxf": "الرسم", "image": "الصورة"}
        section_label = "جميع الأقسام" if section in ("", "الكل") else section
        classification_label = "بدون تصنيف" if classification in ("", "الكل") else classification

        detected_classification = None
        engine_label = "محلّل النصوص"
        vision_warning = ""
        if ai_result:
            detected_classification = ai_result.get("classification")
            if detected_classification in ("سكني", "تجاري"):
                classification_label = detected_classification
            engine_label = f"محرك الرؤية الذكي ({getattr(inspector, 'VISION_MODEL_USED', None) or inspector.VISION_MODEL})"
        elif inspector.VISION_LAST_ERROR == "quota":
            vision_warning = "تنبيه: محرك الرؤية بالذكاء الاصطناعي متعطّل مؤقتًا (انتهت الحصة المجانية اليوم). الفحص الحالي بالمحرك النصي — حاول غدًا أو أضف مصدرًا ممولًا."

        engine_note = (
            f"قرأت المخطط بـ {engine_label} وقارنت {len(prepared)} اشتراطًا مستوردًا "
            f"(المقطع: {section_label}، الاستخدام: {classification_label}) + {len(municipal_codes_db)} كود محلي."
        )

        report = {
            "id": report_id,
            "filename": original_name,
            "file_path": file_path,
            "extension": extension,
            "file_type": SUPPORTED_TYPES.get(extension, extension),
            "section": section_label,
            "classification": classification_label,
            "classification_detected": detected_classification,
            "engine": "ai" if ai_result else "text",
            "engine_label": engine_label,
            "vision_warning": vision_warning,
            "applicable_note": engine_note,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "checks": checks,
            "text_rules": text_rules,
            "drawing_note": drawing.note,
            "images": [(f"{labels.get(drawing.kind, 'المخطط')} {index}", name) for index, name in enumerate(images, start=1)],
            "assistant_status": "queued",
        }
        report["batch"] = (reports.get(report_id) or {}).get("batch")
        report["pending"] = False
        reports[report_id] = report

        threading.Thread(
            target=_assistant_worker,
            args=(report_id, file_path, extension, drawing),
            daemon=True,
        ).start()
    except Exception as exc:
        reports[report_id] = {
            "id": report_id,
            "filename": original_name,
            "pending": False,
            "failed": True,
            "error": str(exc),
            "batch": (reports.get(report_id) or {}).get("batch"),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    finally:
        save_reports()
        jobs.pop(report_id, None)


@app.get("/report/{report_id}", response_class=HTMLResponse)
async def report_page(report_id: str):
    report = reports.get(report_id)
    if not report:
        return layout(
            page_title="تقرير الفحص",
            active="/WEST.anaf",
            content=error_html("انتهت صلاحية التقرير أو لم يوجد. يرجى رفع المخطط من جديد."),
        )

    return HTMLResponse(
        PRINT_CSS
        + f"""
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>تقرير فحص المخطط - منصة تدقيق رخص البناء الذكية</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://unpkg.com/htmx.org@1.9.12"></script>
    </head>
    <body class="bg-gray-100 font-sans" dir="rtl">
        <div class="p-3 sm:p-6 space-y-4">
            <div class="no-print flex flex-wrap items-center justify-between gap-2 bg-white shadow rounded-xl px-4 py-3">
                <h2 class="text-lg font-semibold text-slate-700">تقرير فحص المخطط الهندسي</h2>
                <a href="/WEST.anaf" class="text-sm text-emerald-700 hover:underline">رجوع لصفحة الرفع</a>
            </div>
            {report_body(report)}
        </div>
        <script>
            var SHARE_HOST = "{SHARE_HOST}";
            function shareUrl() {{
                var h = location.hostname;
                if (h === "127.0.0.1" || h === "localhost") h = SHARE_HOST;
                return location.protocol + "//" + h + (location.port ? ":" + location.port : "") + location.pathname;
            }}
            function copyShare(btn) {{
                var url = shareUrl();
                var done = function () {{
                    var old = btn.textContent;
                    btn.textContent = "تم النسخ ✓";
                    setTimeout(function () {{ btn.textContent = old; }}, 2000);
                }};
                if (navigator.clipboard && navigator.clipboard.writeText) {{
                    navigator.clipboard.writeText(url).then(done, function () {{ prompt("انسخ الرابط:", url); }});
                }} else {{
                    prompt("انسخ الرابط:", url);
                }}
            }}
            function shareWhatsApp() {{
                window.open("https://wa.me/?text=" + encodeURIComponent("تقرير فحص مخطط هندسي: " + shareUrl()), "_blank");
            }}
        </script>
    </body>
    </html>
    """
    )


@app.get("/report/{report_id}/poll", response_class=HTMLResponse)
async def report_poll(report_id: str):
    report = reports.get(report_id)
    if not report:
        return HTMLResponse(
            f'<div id="report-body">{error_html("انتهت صلاحية التقرير.")}</div>'
        )
    if report.get("pending"):
        return HTMLResponse(pending_body(report_id, trigger="every 2.5s"))
    if report.get("failed"):
        return HTMLResponse(
            f'<div id="report-body">{error_html("تعذّر إكمال الفحص: " + html.escape(str(report.get("error", ""))))}</div>'
        )
    return HTMLResponse(f'<div id="report-body">{report_content(report)}</div>')


@app.get("/report/{report_id}/assistant", response_class=HTMLResponse)
async def report_assistant(report_id: str):
    report = reports.get(report_id)
    if not report:
        return HTMLResponse("")
    status = str(report.get("assistant_status") or "unavailable")
    if status in ("queued", "pending", "running"):
        return HTMLResponse(assistant_pending_html(report_id, delay=3))
    return HTMLResponse(assistant_done_html(report))


@app.post("/report/{report_id}/assistant/run", response_class=HTMLResponse)
async def report_assistant_run(report_id: str):
    report = reports.get(report_id)
    if not report or report.get("pending") or report.get("failed"):
        return HTMLResponse("")
    if str(report.get("assistant_status") or "") in ("queued", "pending", "running"):
        return HTMLResponse(assistant_pending_html(report_id, delay=3))

    file_path = report.get("file_path") or ""
    extension = report.get("extension") or get_extension(report.get("filename", ""))
    report.pop("assistant", None)
    report.pop("assistant_links", None)
    report.pop("assistant_error", None)

    if not file_path or not os.path.exists(file_path):
        report["assistant_status"] = "unavailable"
        report["assistant_error"] = "لم يعد الملف الأصلي متاحًا لإعادة المراجعة."
        save_reports()
        return HTMLResponse(assistant_done_html(report))

    report["assistant_status"] = "pending"
    save_reports()
    threading.Thread(
        target=_assistant_worker,
        args=(report_id, file_path, extension, None),
        daemon=True,
    ).start()
    return HTMLResponse(assistant_pending_html(report_id, delay=3))


def report_body(report: dict) -> str:
    if report.get("pending"):
        return pending_body(report["id"], trigger="load delay:800ms")
    if report.get("failed"):
        return (
            '<div id="report-body">'
            + error_html("تعذّر إكمال الفحص: " + html.escape(str(report.get("error", ""))))
            + "</div>"
        )
    return f'<div id="report-body">{report_content(report)}</div>'


def pending_body(report_id: str, trigger: str) -> str:
    message = (jobs.get(report_id) or {}).get("message", "جارٍ التحليل...")
    spinner = f"""
    <div class="flex flex-col items-center justify-center py-24 text-center">
        <div class="w-14 h-14 border-4 border-emerald-200 border-t-emerald-600 rounded-full animate-spin mb-6"></div>
        <p class="text-slate-700 font-semibold text-lg mb-1">جارٍ فحص المخطط...</p>
        <p class="text-slate-500 text-sm">{html.escape(message)}</p>
        <p class="text-xs text-slate-400 mt-4">سيظهر التقرير تلقائيًا عند اكتمال القراءة</p>
    </div>
    """
    return (
        f'<div id="report-body" hx-get="/report/{report_id}/poll" '
        f'hx-trigger="{trigger}" hx-swap="outerHTML">{spinner}</div>'
    )


@app.post("/report/{report_id}/reading", response_class=HTMLResponse)
async def update_reading(report_id: str, rule_id: str = Form(...), value: str = Form("")):
    report = reports.get(report_id)
    if not report:
        return HTMLResponse(error_html("انتهت صلاحية التقرير."))

    target = next((c for c in report["checks"] if c.rule_id == int(rule_id)), None)
    if target is None:
        return HTMLResponse(error_html("الاشتراط غير موجود."))

    try:
        measured = float(value)
    except (TypeError, ValueError):
        return check_row(target, report["checks"].index(target) + 1, report_id)

    target.measured = measured
    target.source = "manual"
    target.marker = None
    above_min = target.min_value is None or measured >= target.min_value
    below_max = target.max_value is None or measured <= target.max_value
    target.status = "pass" if (above_min and below_max) else "fail"
    target.note = "مطابق للاشتراط" if target.status == "pass" else "مخالفة للاشتراط"
    save_reports()

    return check_row(target, report["checks"].index(target) + 1, report_id)
