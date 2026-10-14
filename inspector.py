import base64
import io
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

def _data_dir() -> str:
    return os.environ.get("DATA_DIR") or ("/data" if os.path.isdir("/data") else ".")


ANNOTATED_DIR = os.path.join(_data_dir(), "uploads", "reports")

VISION_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
VISION_BASE_URL = os.environ.get("OPENAI_BASE_URL") or None
MAX_VISION_PAGES = 4

NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")

CONCEPT_KEYWORDS: Dict[str, List[str]] = {
    "front": [
        "الارتداد الامامي",
        "ارتداد امامى",
        "-front",
        "front",
        "الواجهة",
        "واجهة",
    ],
    "side": ["الارتداد الجانبي", "ارتداد جانبى", "side", "جانبي", "جانبية"],
    "rear": ["الارتداد الخلفي", "ارتداد خلفى", "rear", "back", "خلفي", "خلفية"],
    "coverage": [
        "نسبه البناء",
        "نسبة الفراغ",
        "كثافه البناء",
        "معامل البناء",
        "coverage",
        "far",
        "gfa",
        "built up",
    ],
    "height": ["ارتفاع", "height", "الارتفاع"],
    "width": ["عرض الشارع", "street width", "width", "العرض", "عرض"],
    "depth": ["عمق", "depth", "العمق"],
    "length": ["طول", "length", "الطول"],
    "area": ["مساحه", "مساحة الاراضيه", "area", "المساحه", "المساحة"],
}


@dataclass
class TextItem:
    page: int
    text: str
    x: float
    y: float
    size: float


@dataclass
class Drawing:
    kind: str
    pages: List[List[TextItem]] = field(default_factory=list)
    page_sizes: List[Tuple[float, float]] = field(default_factory=list)
    note: str = ""
    images: List[str] = field(default_factory=list)


@dataclass
class Check:
    rule_id: int
    rule_name: str
    category: str
    unit: str
    limit_text: str
    min_value: Optional[float]
    max_value: Optional[float]
    measured: Optional[float] = None
    source: str = "none"
    status: str = "unknown"
    note: str = ""
    marker: Optional[dict] = None
    assistant_note: str = ""


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ئ", "ي").replace("ؤ", "و").replace("ة", "ه")
    return re.sub(r"[\s\-_]+", " ", text.lower())


def parse_number(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", "."))
    except ValueError:
        return None


def unit_kind(unit: str) -> str:
    unit = normalize(unit)
    if "%" in unit:
        return "percent"
    if "م2" in unit or "م²" in unit or "sqm" in unit or "m2" in unit:
        return "area"
    if "م" in unit or "m" == unit or "meter" in unit or "metre" in unit:
        return "length"
    return "any"


def build_lines(items: List[TextItem], tolerance_ratio: float = 0.6) -> List[Dict]:
    lines: List[Dict] = []
    for item in sorted(items, key=lambda i: (-i.y, i.x)):
        target = None
        for line in lines:
            if abs(line["y"] - item.y) <= max(item.size, 1) * tolerance_ratio:
                target = line
                break
        if target is None:
            lines.append({"y": item.y, "page": item.page, "items": [item]})
        else:
            target["items"].append(item)

    for line in lines:
        line["items"].sort(key=lambda i: i.x)
        line["text"] = " ".join(i.text for i in line["items"])
    return lines


def extract_pdf(path: str) -> Drawing:
    reader = PdfReader(path)
    pages: List[List[TextItem]] = []
    sizes: List[Tuple[float, float]] = []

    for page_index, page in enumerate(reader.pages):
        items: List[TextItem] = []

        def visitor(text, cm, tm, font_dict, font_size, items=items, page_index=page_index):
            if not text or not text.strip():
                return
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            items.append(
                TextItem(
                    page=page_index,
                    text=text.strip(),
                    x=float(x),
                    y=float(y),
                    size=float(font_size or 10),
                )
            )

        page.extract_text(visitor_text=visitor)
        pages.append(items)
        box = page.mediabox
        sizes.append((float(box.width), float(box.height)))

    has_text = any(items for items in pages)
    return Drawing(
        kind="pdf",
        pages=pages,
        page_sizes=sizes,
        note="" if has_text else "الملف PDF لا يحتوي على نصوص (مخطط ممسوح ضوئياً) — يلزم إدخال القياسات يدوياً.",
    )


def extract_dxf(path: str) -> Drawing:
    import ezdxf

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    items: List[TextItem] = []

    for entity in msp:
        kind = entity.dxftype()
        if kind == "TEXT":
            content = entity.dxf.text
            point = entity.dxf.insert
        elif kind == "MTEXT":
            content = entity.plain_text() if hasattr(entity, "plain_text") else entity.text
            point = entity.dxf.insert
        elif kind == "DIMENSION":
            content = entity.dxf.text or ""
            try:
                measurement = entity.get_measurement()
            except Exception:
                measurement = None
            content = f"{content} {measurement}".strip() if measurement else content
            try:
                point = entity.dxf.defpoint
            except Exception:
                point = (0, 0)
        else:
            continue

        if not content or not str(content).strip():
            continue

        items.append(
            TextItem(
                page=0,
                text=str(content).strip(),
                x=float(point[0]),
                y=float(point[1]),
                size=1.0,
            )
        )

    extents = _dxf_extents(msp)
    return Drawing(
        kind="dxf",
        pages=[items],
        page_sizes=[extents],
        note="" if items else "لا يحتوي ملف DXF على نصوص أو أبعاد قابلة للقراءة.",
    )


def _dxf_extents(msp) -> Tuple[float, float]:
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for entity in msp:
        try:
            for point in entity.bbox():
                min_x, min_y = min(min_x, point[0]), min(min_y, point[1])
                max_x, max_y = max(max_x, point[0]), max(max_y, point[1])
        except Exception:
            continue
    if min_x == float("inf"):
        return (1000.0, 1000.0)
    return (max(max_x - min_x, 1.0), max(max_y - min_y, 1.0))


def extract_image(path: str) -> Drawing:
    with Image.open(path) as img:
        width, height = img.size
    return Drawing(
        kind="image",
        pages=[[]],
        page_sizes=[(float(width), float(height))],
        note="الصور لا تحتوي طبقة نصية قابلة للقراءة آلياً — يلزم إدخال القياسات يدوياً.",
    )


OPAQUE_NOTE = {
    ".dwg": "صيغة DWG ثنائية لا يمكن تحليلها آلياً — احفظ الملف بصيغة DXF ثم أعد الرفع.",
    ".rvt": "نماذج Revit لا يمكن تحليلها آلياً — أدخل القياسات يدوياً.",
    ".ifc": "نماذج IFC لا يمكن تحليلها آلياً — أدخل القياسات يدوياً.",
    ".skp": "نماذج SketchUp لا يمكن تحليلها آلياً — أدخل القياسات يدوياً.",
}


def extract(path: str, extension: str) -> Drawing:
    try:
        if extension == ".pdf":
            return extract_pdf(path)
        if extension == ".dxf":
            return extract_dxf(path)
        if extension in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}:
            return extract_image(path)
    except Exception as exc:
        return Drawing(kind="opaque", note=f"تعذّر قراءة الملف آلياً: {exc}")

    return Drawing(
        kind="opaque",
        note=OPAQUE_NOTE.get(extension, "هذه الصيغة لا تدعم القراءة الآلية — أدخل القياسات يدوياً."),
    )


def rule_concepts(rule_name: str) -> List[str]:
    normalized = normalize(rule_name)
    return [
        concept
        for concept, keywords in CONCEPT_KEYWORDS.items()
        if any(normalize(keyword) in normalized for keyword in keywords)
    ]


def concept_keywords(concepts: List[str]) -> List[str]:
    keywords: List[str] = []
    for concept in concepts:
        keywords.extend(CONCEPT_KEYWORDS.get(concept, []))
    return keywords


def unit_tokens(unit: str) -> List[str]:
    kind = unit_kind(unit)
    if kind == "percent":
        return ["%"]
    if kind == "area":
        return ["م2", "م²", "m2", "sqm", "m^2"]
    if kind == "length":
        return ["متر", "م", "m", "meter", "metre"]
    return []


def find_reading(drawing: Drawing, rule: dict) -> Tuple[Optional[float], Optional[dict]]:
    concepts = rule.get("concepts") or rule_concepts(rule["rule_name"])
    keywords = concept_keywords(concepts)
    kind = unit_kind(rule["unit"])
    tokens = unit_tokens(rule["unit"])

    if not any(drawing.pages):
        return None, None

    for page_index, items in enumerate(drawing.pages):
        for line in build_lines(items):
            match = _match_by_keyword(line, keywords, kind)
            if match:
                value, item = match
                return value, {"page": page_index, "x": item.x, "y": item.y, "size": item.size}

    if concepts:
        return None, None

    for page_index, items in enumerate(drawing.pages):
        for line in build_lines(items):
            match = _match_by_unit(line, tokens, kind)
            if match:
                value, item = match
                return value, {"page": page_index, "x": item.x, "y": item.y, "size": item.size}

    return None, None


def _line_candidates(line: Dict, kind: str) -> List[Tuple[float, TextItem, int, int]]:
    candidates: List[Tuple[float, TextItem, int, int]] = []
    offset = 0

    for item in line["items"]:
        for match in NUMBER_RE.finditer(item.text):
            value = parse_number(match.group())
            if value is None:
                continue
            if kind == "percent" and not (0 <= value <= 1000):
                continue
            if kind == "length" and value <= 0:
                continue
            start = offset + match.start()
            end = offset + match.end()
            candidates.append((value, item, start, end))
        offset += len(item.text) + 1

    return candidates


def _match_by_keyword(
    line: Dict, keywords: List[str], kind: str
) -> Optional[Tuple[float, TextItem]]:
    candidates = _line_candidates(line, kind)
    if not candidates:
        return None

    normalized_items = [(item, normalize(item.text)) for item in line["items"]]

    for keyword in keywords:
        normalized_keyword = normalize(keyword)
        for item, normalized_item in normalized_items:
            if normalized_keyword not in normalized_item:
                continue
            value, nearest, _, _ = min(
                candidates, key=lambda c: abs(c[1].x - item.x)
            )
            return value, nearest

    return None


def _match_by_unit(
    line: Dict, tokens: List[str], kind: str
) -> Optional[Tuple[float, TextItem]]:
    candidates = _line_candidates(line, kind)
    if not candidates or not tokens:
        return None

    normalized_line = normalize(line["text"])
    normalized_tokens = [normalize(token) for token in tokens]

    for value, item, _start, end in candidates:
        tail = normalized_line[end : end + 8]
        if any(token and token in tail for token in normalized_tokens):
            return value, item

    return None


VECTOR_KEYWORD_GAP = 30


def _value_allowed(value: float, kind: str) -> bool:
    if kind == "percent":
        return 0 <= value <= 1000
    if kind == "length":
        return value > 0
    return True


def _char_gap(k_start: int, k_end: int, n_start: int, n_end: int) -> int:
    if n_start >= k_end:
        return n_start - k_end
    if k_start >= n_end:
        return k_start - n_end
    return 0


def _keyword_positions(text: str, keyword: str) -> List[Tuple[int, int]]:
    """مواقع الكلمة في النص؛ الكلمات اللاتينية تُطابق بحدود كلمة كاملة
    (لمنع مطابقة back داخل setback)، والكلمات العربية تُطابق كما هي
    لأن العربية تُركّب بالحروف (ال+اسم)."""
    positions: List[Tuple[int, int]] = []
    latin = keyword.isascii()
    start = 0
    while True:
        pos = text.find(keyword, start)
        if pos == -1:
            break
        end = pos + len(keyword)
        if latin:
            before = text[pos - 1] if pos > 0 else ""
            after = text[end] if end < len(text) else ""
            if (before and before.isalnum()) or (after and after.isalnum()):
                start = pos + 1
                continue
        positions.append((pos, end))
        start = pos + 1
    return positions


def collect_vector_readings(
    numeric_rules: List[dict], drawing: Drawing
) -> Dict[int, Tuple[float, Optional[dict]]]:
    """مطابقة حتمية بين كلمات الاشتراط وأرقام نص المخطط.

    قواعد صارمة لمنع القراءات الخاطئة:
    - الرقم يُقرأ بالاتجاه الأمامي للكلمة أولًا (التسمية قبل القيمة).
    - كل رقم في المخطط يُمنح لقاعدة واحدة فقط؛ إذا تداخلت قواعد على نفس
      الرقم والمسافات متساوية → يُترك الرقم للرؤية الذكية.
    """

    prepared: List[Tuple[List[str], List[str], str]] = []
    for rule in numeric_rules:
        concepts = rule_concepts(rule["rule_name"])
        keywords = [normalize(k) for k in concept_keywords(concepts)]
        tokens = [normalize(t) for t in unit_tokens(rule["unit"])]
        prepared.append((keywords, tokens, unit_kind(rule["unit"])))

    claims: Dict[Tuple[int, int], List[dict]] = {}

    for page_index, items in enumerate(drawing.pages):
        for line in build_lines(items):
            parts = [normalize(item.text) for item in line["items"]]
            spans: List[Tuple[int, int, TextItem]] = []
            pos = 0
            for item, part in zip(line["items"], parts):
                spans.append((pos, pos + len(part), item))
                pos += len(part) + 1
            text = " ".join(parts)

            numbers: List[Tuple[int, int, float, TextItem]] = []
            for match in NUMBER_RE.finditer(text):
                value = parse_number(match.group())
                if value is None:
                    continue
                item = next(
                    (it for start, end, it in spans if start <= match.start() <= end),
                    None,
                )
                if item is None:
                    continue
                numbers.append((match.start(), match.end(), value, item))
            if not numbers:
                continue

            for rule_index, (keywords, tokens, kind) in enumerate(prepared):
                valid = [n for n in numbers if _value_allowed(n[2], kind)]
                if not valid:
                    continue

                if keywords:
                    for keyword in keywords:
                        if not keyword:
                            continue
                        for kpos, k_end in _keyword_positions(text, keyword):
                            best = None
                            forward = [n for n in valid if n[0] >= k_end]
                            if forward:
                                candidate = min(forward, key=lambda n: n[0] - k_end)
                                if candidate[0] - k_end <= VECTOR_KEYWORD_GAP:
                                    best = candidate
                            if best is None:
                                backward = [n for n in valid if n[1] <= kpos]
                                if backward:
                                    candidate = max(backward, key=lambda n: n[1])
                                    if kpos - candidate[1] <= VECTOR_KEYWORD_GAP:
                                        best = candidate
                            if best is not None:
                                claims.setdefault((page_index, best[0]), []).append(
                                    {
                                        "rule": rule_index,
                                        "value": best[2],
                                        "item": best[3],
                                        "gap": _char_gap(kpos, k_end, best[0], best[1]),
                                        "weak": False,
                                    }
                                )

    results: Dict[int, Tuple[float, Optional[dict], int]] = {}
    for (page_index, _start), claim_list in claims.items():
        strong = [c for c in claim_list if not c["weak"]]
        pool = strong or claim_list
        owners = {c["rule"] for c in pool}
        best = min(pool, key=lambda c: c["gap"])
        if len(owners) > 1:
            rivals = [c["gap"] for c in pool if c["rule"] != best["rule"]]
            if best["gap"] >= min(rivals):
                continue
        rule_index = best["rule"]
        previous = results.get(rule_index)
        if previous is None or best["gap"] < previous[2]:
            item = best["item"]
            marker = {"page": page_index, "x": item.x, "y": item.y, "size": item.size}
            results[rule_index] = (best["value"], marker, best["gap"])

    return {
        numeric_rules[rule_index]["id"]: (value, marker)
        for rule_index, (value, marker, _gap) in results.items()
    }


def build_checks(rules: List[dict], drawing: Drawing, manual: Dict[int, float]) -> List[Check]:
    numeric_rules = [
        rule
        for rule in rules
        if rule.get("min_value") is not None or rule.get("max_value") is not None
    ]
    vector_readings = collect_vector_readings(numeric_rules, drawing)
    checks: List[Check] = []

    for rule in numeric_rules:
        if rule["id"] in manual:
            value: Optional[float] = manual[rule["id"]]
            source = "manual"
            marker = None
        elif rule["id"] in vector_readings:
            value, marker = vector_readings[rule["id"]]
            source = "auto"
        else:
            value = None
            source = "none"
            marker = None

        checks.append(_make_check(rule, value, source, marker))

    return checks


def _render_pdf_image(path: str, page_index: int, scale: float = 3.0) -> Image.Image:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(path)
    page = document[page_index]
    image = page.render(scale=scale).to_pil().convert("RGB")
    document.close()
    return image


def vision_input_images(
    path: str, drawing: Drawing, extension: str, max_pages: int = MAX_VISION_PAGES
) -> List[Tuple[int, Image.Image]]:
    images: List[Tuple[int, Image.Image]] = []
    try:
        if drawing.kind == "pdf":
            total = len(drawing.pages)
            for index in range(min(total, max_pages)):
                images.append((index, _render_pdf_image(path, index)))
        elif drawing.kind == "dxf":
            rendered = _render_dxf_image(path)
            if rendered is not None:
                images.append((0, rendered))
        elif drawing.kind == "image":
            with Image.open(path) as source:
                images.append((0, source.convert("RGB")))
    except Exception:
        return []
    return images


def _encode_image(image: Image.Image, max_side: int = 2200) -> str:
    source = image
    if max(source.size) > max_side:
        ratio = max_side / max(source.size)
        source = image.resize((int(image.width * ratio), int(image.height * ratio)))
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", quality=85)
    data = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{data}"


def _parse_json(value: Optional[str]) -> Optional[dict]:
    if not value:
        return None
    text = value.strip()
    if "```" in text:
        text = re.sub(r"```(?:json)?", "", text).strip("` \r\n")
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _vlog(message: str) -> None:
    if os.environ.get("INSPECTOR_DEBUG"):
        stamp = time.strftime("%H:%M:%S", time.localtime())
        print(f"[VISION {stamp}.{int((time.time() % 1) * 1000):03d}] {message}", flush=True)


VISION_LAST_ERROR: Optional[str] = None
VISION_MODEL_USED: Optional[str] = None
_QUOTA_FILE = os.path.join(_data_dir(), "uploads", ".vision_state.json")


def _load_vision_state() -> dict:
    try:
        with open(_QUOTA_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_vision_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_QUOTA_FILE), exist_ok=True)
        with open(_QUOTA_FILE, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
    except Exception:
        pass


def _record_quota_dead(model: str) -> None:
    state = _load_vision_state()
    quota_dead = state.setdefault("quota_dead", {})
    if quota_dead.get(model) != time.strftime("%Y-%m-%d"):
        quota_dead[model] = time.strftime("%Y-%m-%d")
        _save_vision_state(state)


_MODEL_COOLDOWN: Dict[str, float] = {}
MODEL_COOLDOWN_SECONDS = 300.0


def _record_success(model: str) -> None:
    _MODEL_COOLDOWN.pop(model, None)
    state = _load_vision_state()
    scores = state.setdefault("scores", {})
    scores[model] = int(scores.get(model, 0)) + 1
    _save_vision_state(state)


def _is_quota_dead_today(model: str) -> bool:
    state = _load_vision_state()
    return (state.get("quota_dead", {}) or {}).get(model) == time.strftime("%Y-%m-%d")


def _record_soft_failure(model: str) -> None:
    _MODEL_COOLDOWN[model] = time.time()
    state = _load_vision_state()
    scores = state.setdefault("scores", {})
    scores[model] = max(0, int(scores.get(model, 0)) - 1)
    _save_vision_state(state)


def _is_rate_limit(exc: Exception) -> bool:
    return "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)


def _is_daily_quota(exc: Exception) -> bool:
    text = str(exc)
    return "PerDay" in text or "GenerateRequestsPerDay" in text or "free_tier_requests" in text


def _retry_delay_seconds(exc: Exception) -> float:
    text = str(exc)
    match = re.search(r"retry in ([\d.]+)s", text)
    if match:
        return max(1.0, min(float(match.group(1)), 60.0))
    match = re.search(r"retryDelay': '(\d+)s'", text)
    if match:
        return max(1.0, min(float(match.group(1)), 60.0))
    return 8.0


def _vision_models() -> List[str]:
    primary = VISION_MODEL
    override = os.environ.get("INSPECTOR_VISION_MODELS")
    if override:
        extras = [model.strip() for model in override.split(",") if model.strip()]
    else:
        extras = [
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-flash-latest",
            "gemini-3.1-flash-lite",
        ]
    state = _load_vision_state()
    quota_dead = state.get("quota_dead", {}) or {}
    scores = state.get("scores", {}) or {}
    today = time.strftime("%Y-%m-%d")
    extras = sorted(
        extras,
        key=lambda model: -int(scores.get(model, 0)),
    )
    return [
        model
        for model in [primary] + [model for model in extras if model != primary]
        if quota_dead.get(model) != today
    ]


def _call_vision(client, messages) -> Optional[str]:
    global VISION_LAST_ERROR, VISION_MODEL_USED
    for model in _vision_models():
        if _is_quota_dead_today(model):
            _vlog(f"{model} already exhausted today — skipped")
            continue
        if time.time() - _MODEL_COOLDOWN.get(model, 0.0) < MODEL_COOLDOWN_SECONDS:
            _vlog(f"{model} cooling down after recent failure — skipped")
            continue
        for attempt in (0, 1):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=6000,
                )
                VISION_MODEL_USED = model
                VISION_LAST_ERROR = None
                _record_success(model)
                return response.choices[0].message.content
            except Exception as exc:
                _vlog(f"{model} attempt {attempt + 1} failed: {exc!r}")
                if _is_rate_limit(exc):
                    VISION_LAST_ERROR = "quota"
                    if _is_daily_quota(exc):
                        _record_quota_dead(model)
                        _vlog(f"{model} quota exhausted for today — skipping it")
                        break
                    if attempt == 1:
                        _record_soft_failure(model)
                        break
                    time.sleep(min(_retry_delay_seconds(exc), 8.0))
                    continue
                lowered = str(exc).lower()
                if "404" in str(exc) or "not found" in lowered:
                    break
                if "connection" in lowered or "timed out" in lowered or "timeout" in lowered:
                    _record_soft_failure(model)
                    _vlog(f"{model} connection problem — trying next model without plain retry")
                    break
                if "503" in str(exc) or "unavailable" in lowered or "overloaded" in lowered:
                    _record_soft_failure(model)
                    _vlog(f"{model} unavailable — trying next model without plain retry")
                    break
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        max_tokens=6000,
                    )
                    VISION_MODEL_USED = model
                    VISION_LAST_ERROR = None
                    _record_success(model)
                    return response.choices[0].message.content
                except Exception as plain_exc:
                    _vlog(f"{model} plain attempt failed: {plain_exc!r}")
                    _record_soft_failure(model)
                    break
    return None


def _sent_size(image: Image.Image, max_side: int = 2200) -> Tuple[int, int]:
    width, height = image.size
    if max(width, height) > max_side:
        ratio = max_side / max(width, height)
        return int(width * ratio), int(height * ratio)
    return width, height


def ask_vision(
    rules: List[dict],
    path: str,
    drawing: Drawing,
    extension: str,
    emphasis: str = "",
    images: Optional[List[Tuple[int, Image.Image]]] = None,
) -> Optional[dict]:
    api_key = os.environ.get("OPENAI_API_KEY")
    _vlog(f"api_key={'set' if api_key else 'MISSING'}")
    if not api_key:
        return None

    if images is None:
        images = vision_input_images(path, drawing, extension)
    _vlog(f"images={len(images) if images else 0}")
    if not images:
        return None

    try:
        from openai import OpenAI
    except Exception as exc:
        _vlog(f"openai import failed: {exc!r}")
        return None

    numeric_rules = [
        rule
        for rule in rules
        if rule.get("min_value") is not None or rule.get("max_value") is not None
    ]
    if not numeric_rules:
        numeric_rules = list(rules)
    numeric_rules = numeric_rules[:100]
    _vlog(f"numeric rules to read: {len(numeric_rules)}")

    coord_scale: Dict[str, List[float]] = {}
    encoded: List[Tuple[int, str]] = []
    for page_index, image in images:
        full_width, full_height = image.size
        sent_width, sent_height = _sent_size(image)
        coord_scale[str(page_index)] = [full_width / sent_width, full_height / sent_height]
        encoded.append((page_index, _encode_image(image)))

    system_prompt = (
        "أنت مهندس تدقيق رخص بناء خبير يقرأ المخطط الهندسي جزءًا جزءًا دون استثناء. "
        "كل مخطط يحوي تفصيلًا كثيرًا لازم تقرأه كلّه: مخطط الموقع (حدود الأرض، الشارع وعرضه المكتوب "
        "بخط أبعاده، خطوط الأبعاد، خط المبنى، خط الملحق العلوي)، جداول المساحات ونسب البناء، "
        "الواجهات والتفاصيل الهندسية، الحواشي، وبلاطة العنوان.\n"
        "طريقة العمل الإلزامية قبل كل استجابة:\n"
        "1) افحص مخطط الموقع بالكامل: اقرأ عرض الشارع من خط أبعاده، وجميع ارتدادات الجهات من خطوط "
        "الأبعاد بين حدود الأرض وخط المبنى، وارتداد الملحق العلوي عن الشارع إن رأيت خط الملحق.\n"
        "2) افحص جداول المساحات: نسبة البناء ومساحات الأدوار مكتوبة فيها إن وُجدت — اقرأها كما هي.\n"
        "3) افحص باقي الصفحات كلها: الواجهات، التفاصيل، الحواشي، وبلاطة العنوان — لا تترك ركنًا دون فحص.\n"
        "4) لكل اشتراط في القائمة حدّد مكانه المناسب ثم أعطه قيمته مع إحداثيات x و y على الصورة.\n"
        "قواعد حاسمة:\n"
        "- الارتداد الأمامي: إن لم تكن قيمته مكتوبة فاحسب خمس عرض الشارع = W ÷ 5 من خط أبعاد الشارع.\n"
        "- نسبة البناء: من جدول المساحات، أو احسبها (مساحة المبنى ÷ مساحة الأرض) × 100 إن كانت الأبعاد مرسومة.\n"
        "- قواعد الملحق العلوي منطبقة دائمًا على المخططات السكنية — لا تجعلها applicable:false أبدًا "
        "قبل أن تفحص مخطط الموقع والواجهات وجدول المساحات بحثًا عن قيمتها.\n"
        "- applicable:false مسموح فقط للاشتراطات التجارية (محلات/واجهات تجارية/مدخل تجاري) بعد فحص "
        "كل الصفحات والتأكد من عدم وجود أي استخدام تجاري في المخطط.\n"
        "- لا تخمن: قيمتك يجب أن تكون مقروءة من الرسم أو محسوبة منه بمعادلة مذكورة أعلاه.\n"
        "- لا تترك اشتراطًا منطبقًا بلا إجابة إلا بعد الفحص الكامل لكل جزء في كل الصفحات.\n"
        "ارجع JSON بالصيغة الدقيقة التالية دون أي نص إضافي:\n"
        "{\"classification\":\"سكني\"أو\"تجاري\"أو\"غير محدد\", "
        "\"usage_note\":\"ملاحظة قصيرة عن الاستخدام المستنتج\", "
        "\"readings\":["
        "{\"rule_id\":1,\"value\":1.5,\"unit\":\"متر\",\"page\":0,\"x\":120,\"y\":340,"
        "\"confidence\":0.9,\"note\":\"القيمة مقروءة من خط الأبعاد\"},"
        "{\"rule_id\":5,\"applicable\":false,\"note\":\"لا يوجد ملحق علوي في المخطط\"}]}"
    )

    client = OpenAI(api_key=api_key, base_url=VISION_BASE_URL, timeout=45.0, max_retries=0)
    detail = "high" if len(images) <= 2 else "low"
    readings: Dict[int, dict] = {}
    classification: Optional[str] = None
    usage_note: Optional[str] = None

    chunk_size = 40
    chunks = [
        numeric_rules[index : index + chunk_size]
        for index in range(0, len(numeric_rules), chunk_size)
    ][:5]

    for chunk_index, chunk in enumerate(chunks):
        rules_payload = [
            {
                "id": rule["id"],
                "rule_name": rule["rule_name"][:140],
                "unit": rule.get("unit", ""),
                "min_value": rule.get("min_value"),
                "max_value": rule.get("max_value"),
            }
            for rule in chunk
        ]
        user_text = (
            "المخطط المرفق فيه تفصيل كثير — اقرأ كل جزء فيه دون استثناء: مخطط الموقع، عرض الشارع "
            "وأبعاده، جداول المساحات ونِسب البناء، خطوط الملحق العلوي وارتداده، الواجهات والتفاصيل، "
            "الحواشي وبلاطة العنوان. قدّم قراءة دقيقة لكل اشتراط من القائمة تجده في أي جزء من المخطط، "
            "أو applicable:false فقط للاشتراطات التجارية في مخطط سكني وبعد التأكد. "
            "الإحداثيات x و y بالبكسل من أعلى يسار الصورة المرسلة، ورقم الصفحة يبدأ من 0.\n"
            f"الاشتراطات:\n{json.dumps(rules_payload, ensure_ascii=False, indent=2)}"
            + (("\n" + emphasis) if emphasis else "")
        )
        content: List[dict] = [{"type": "text", "text": user_text}]
        for page_index, url in encoded:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": url, "detail": detail},
                }
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]

        _vlog(f"request chunk {chunk_index + 1}/{len(chunks)} with {len(chunk)} rules")
        content_text = _call_vision(client, messages)
        data = _parse_json(content_text)
        _vlog(f"chunk parsed={bool(data)}")
        if not data:
            if chunk_index == 0:
                VISION_LAST_ERROR = VISION_LAST_ERROR or "format"
                return None
            break

        for entry in data.get("readings") or []:
            try:
                rule_id = int(entry["rule_id"])
                readings[rule_id] = entry
            except (KeyError, TypeError, ValueError):
                continue
        classification = classification or data.get("classification")
        usage_note = usage_note or data.get("usage_note")

    payload = {
        "classification": classification or "غير محدد",
        "usage_note": usage_note or "",
        "readings": list(readings.values()),
        "_pages": [page_index for page_index, _ in images],
        "_coord_scale": coord_scale,
    }
    return payload


AUDIT_SYSTEM_INSTRUCTION = (
    "أنت نظام ذكاء اصطناعي خبير ومهندس تدقيق معتمد للمخططات البلدية والمعمارية في المملكة العربية السعودية.\n"
    "يجب عليك الالتزام التام والتدقيق بناءً على:\n"
    "1) كود البناء السعودي (SBC).\n"
    "2) اشتراطات أمانة منطقة الرياض (جدول المواقف، الارتدادات النظامية، نسب البناء، وعدم ترك جداول الحفر أو الخزانات فارغة).\n"
    "3) ضوابط كود وادي حنيفة (القيود البيئية، الارتفاعات المقيدة، وطابع الأسوار والموقع).\n"
    "مهمتك تحليل صورة المخطط بدقة واكتشاف أي أخطاء أو مخالفات.\n"
    "يجب أن تكون مخرجاتك بصيغة JSON حصراً بدون أي نصوص إضافي، بالشكل التالي:\n"
    "{\"status\":\"rejected\",\"summary\":\"وجود ملاحظات حرجة تستدعي التعديل والتصحيح\","
    "\"errors\":[{\"x\":450,\"y\":200,\"type\":\"critical\",\"text\":\"جدول المواقف أو الحفر فارغ أو مخالف\"}]}\n"
    "إذا كان المخطط سليماً ومستوفياً لكافة الشروط اجعل status يساوي approved وقائمة errors فارغة.\n"
    "الإحداثيات x و y بالبكسل من أعلى يسار الصورة المرسلة، و type إما critical أو warning."
)


def _draw_audit_errors(image: Image.Image, errors: List[dict]) -> None:
    draw = ImageDraw.Draw(image)
    radius = max(45, int(min(image.size) * 0.03))
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", radius)
    except OSError:
        font = ImageFont.load_default()

    for position, err in enumerate(errors, start=1):
        try:
            x = int(err.get("x", 0))
            y = int(err.get("y", 0))
        except (TypeError, ValueError):
            continue
        err_type = str(err.get("type", "critical")).lower()
        color = (220, 30, 30) if err_type == "critical" else (245, 180, 20)
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], outline=color, width=5)
        label = str(position)
        draw.text((x + radius + 8, y - radius), label, fill=color, font=font)


def audit_critical(path: str, extension: str, drawing: Optional[Drawing] = None) -> Optional[dict]:
    """تدقيق بصري للمخالفات الحرجة يعيد ملاحظات JSON مع صور محدّدة بالدوائر."""
    api_key = os.environ.get("OPENAI_API_KEY")
    _vlog(f"audit api_key={'set' if api_key else 'MISSING'}")
    if not api_key:
        return None

    if drawing is None:
        drawing = extract(path, extension)
    if drawing.kind not in ("pdf", "dxf", "image"):
        return None

    images = vision_input_images(path, drawing, extension)
    _vlog(f"audit images={len(images)}")
    if not images:
        return None

    try:
        from openai import OpenAI
    except Exception as exc:
        _vlog(f"openai import failed: {exc!r}")
        return None

    os.makedirs(ANNOTATED_DIR, exist_ok=True)
    client = OpenAI(api_key=api_key, base_url=VISION_BASE_URL, timeout=45.0, max_retries=0)
    user_text = (
        "دقّق هذا المخطط الهندسي وفقاً للقواعد الصارمة المذكورة، "
        "وأعطني الإحداثيات والنتائج بصيغة JSON حصراً."
    )

    pages: List[dict] = []
    for page_index, image in images:
        full_width, full_height = image.size
        sent_width, sent_height = _sent_size(image)
        scale_x = full_width / sent_width
        scale_y = full_height / sent_height

        content: List[dict] = [
            {"type": "text", "text": user_text},
            {
                "type": "image_url",
                "image_url": {"url": _encode_image(image), "detail": "high"},
            },
        ]
        messages = [
            {"role": "system", "content": AUDIT_SYSTEM_INSTRUCTION},
            {"role": "user", "content": content},
        ]

        _vlog(f"audit page {page_index + 1}: request")
        data = _parse_json(_call_vision(client, messages))
        _vlog(f"audit page {page_index + 1}: parsed={bool(data)}")

        errors: List[dict] = []
        for entry in (data or {}).get("errors") or []:
            if not isinstance(entry, dict):
                continue
            try:
                x = float(entry.get("x", 0))
                y = float(entry.get("y", 0))
            except (TypeError, ValueError):
                continue
            errors.append(
                {
                    "x": int(x * scale_x),
                    "y": int(y * scale_y),
                    "type": str(entry.get("type") or "critical"),
                    "text": str(entry.get("text") or "مخالفة"),
                }
            )

        if data is None:
            status = "unknown"
            summary = "تعذّر تحليل هذه الصفحة"
        else:
            status = str(data.get("status") or ("rejected" if errors else "approved"))
            summary = str(data.get("summary") or "")

        annotated = image.copy()
        _draw_audit_errors(annotated, errors)
        output_name = f"audit_{uuid4().hex[:8]}_p{page_index + 1}.png"
        annotated.save(os.path.join(ANNOTATED_DIR, output_name))

        pages.append(
            {
                "page_number": page_index + 1,
                "status": status,
                "summary": summary,
                "errors": errors,
                "annotated_image_url": f"/reports/{output_name}",
            }
        )

    if not pages:
        return None
    return {"pages": pages, "_model": VISION_MODEL_USED}


def build_checks_ai(
    rules: List[dict],
    drawing: Drawing,
    path: str,
    extension: str,
    manual: Dict[int, float],
) -> Optional[Tuple[List[Check], dict]]:
    numeric_rules = [
        rule
        for rule in rules
        if rule.get("min_value") is not None or rule.get("max_value") is not None
    ]

    vector_readings = collect_vector_readings(numeric_rules, drawing)
    vision_needed = [
        rule
        for rule in numeric_rules
        if rule["id"] not in manual and rule["id"] not in vector_readings
    ]

    _vlog(
        f"vector readings: {len(vector_readings)}/{len(numeric_rules)}"
        f" | vision needed: {len(vision_needed)}"
    )

    force_vision = bool(os.environ.get("INSPECTOR_FORCE_VISION"))
    vision_data: Optional[dict] = None
    prepared_images: Optional[List[Tuple[int, Image.Image]]] = None
    if vision_needed or force_vision or not numeric_rules:
        prepared_images = vision_input_images(path, drawing, extension)
        vision_data = ask_vision(
            vision_needed or rules, path, drawing, extension, images=prepared_images
        )
        if vision_data is None and not vector_readings:
            return None

    vision_readings: Dict[int, dict] = {}
    vision_na: Dict[int, str] = {}
    if vision_data:
        for entry in vision_data.get("readings") or []:
            try:
                rule_id = int(entry["rule_id"])
            except (KeyError, TypeError, ValueError):
                continue
            vision_readings[rule_id] = entry
            applicable = entry.get("applicable")
            if applicable is False or str(applicable).strip().lower() == "false":
                vision_na[rule_id] = str(
                    entry.get("note") or "لا ينطبق هذا الاشتراط على المخطط"
                )

    commercial_re = re.compile(r"تجاري|محلات|مدخل التجار")

    def _rule_name(rule: dict) -> str:
        return str(rule.get("rule_name") or "")

    def _entry_value(rule_id: int) -> Optional[float]:
        entry = vision_readings.get(rule_id)
        return _safe_float(entry.get("value")) if entry else None

    retry_rules: List[dict] = []
    for rule in vision_needed:
        rule_id = rule["id"]
        rejected_na = rule_id in vision_na and not commercial_re.search(_rule_name(rule))
        missing_value = _entry_value(rule_id) is None and rule_id not in vision_na
        if rejected_na or missing_value:
            retry_rules.append(rule)

    if retry_rules:
        protected_re = re.compile(r"ملحق|نسبة البناء|ارتداد|المساحة المبنية")
        protected = [r for r in retry_rules if protected_re.search(_rule_name(r))]
        others = [r for r in retry_rules if r not in protected]
        retry_rules = (protected + others)[:25]
        _vlog(f"focused second pass: {len(retry_rules)} rules")
        emphasis = (
            "هذه الاشتراطات بالذات منطبقة وقيمها موجودة في هذا المخطط — لا ترد applicable:false "
            "لها بعد هذا الفحص المركّز: ارجع لمخطط الموقع وجدول المساحات والواجهات واقرأ قيمها الفعلية "
            "من خطوط الأبعاد، وانسخ نسبة البناء من جدول المساحات إن كانت مكتوبة."
        )
        second = ask_vision(
            retry_rules, path, drawing, extension, emphasis=emphasis, images=prepared_images
        )
        second_ids: set = set()
        if second:
            for entry in second.get("readings") or []:
                try:
                    rule_id = int(entry["rule_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                second_ids.add(rule_id)
                applicable = entry.get("applicable")
                if applicable is False or str(applicable).strip().lower() == "false":
                    vision_na[rule_id] = str(
                        entry.get("note") or "لا ينطبق هذا الاشتراط على المخطط"
                    )
                    vision_readings[rule_id] = entry
                elif _safe_float(entry.get("value")) is not None:
                    vision_readings[rule_id] = entry
                    vision_na.pop(rule_id, None)
            if vision_data is not None:
                vision_data.setdefault("readings", []).extend(second.get("readings") or [])

        if second:
            for rule in retry_rules:
                rule_id = rule["id"]
                if rule_id in vision_na and rule_id not in second_ids:
                    if not commercial_re.search(_rule_name(rule)):
                        vision_na.pop(rule_id, None)

    coord_scale: Dict[str, List[float]] = (
        vision_data.get("_coord_scale") if vision_data else None
    ) or {}

    checks: List[Check] = []
    for rule in numeric_rules:
        if rule["id"] in manual:
            value: Optional[float] = manual[rule["id"]]
            source = "manual"
            marker = None
        elif rule["id"] in vector_readings:
            value, marker = vector_readings[rule["id"]]
            source = "auto"
        elif rule["id"] in vision_na:
            check = _make_check(rule, None, "vision", None)
            check.status = "na"
            check.note = vision_na[rule["id"]]
            checks.append(check)
            continue
        elif rule["id"] in vision_readings:
            entry = vision_readings[rule["id"]]
            value = _safe_float(entry.get("value"))
            source = "vision"
            marker = None
            if value is not None and isinstance(entry.get("x"), (int, float)):
                page_index = int(entry.get("page", 0) or 0)
                scale = coord_scale.get(str(page_index)) or [1.0, 1.0]
                marker = {
                    "page": page_index,
                    "x": float(entry["x"]) * scale[0],
                    "y": float(entry.get("y", 0) or 0) * scale[1],
                    "size": 14,
                    "pixel": True,
                }
        else:
            value = None
            source = "none"
            marker = None

        check = _make_check(rule, value, source, marker)
        checks.append(check)

    donors: Dict[Tuple, Check] = {}
    for check in checks:
        if check.measured is None:
            continue
        key = (check.rule_name.strip(), check.min_value, check.max_value, check.unit)
        donors.setdefault(key, check)
    for check in checks:
        if check.measured is not None or check.status != "unknown":
            continue
        key = (check.rule_name.strip(), check.min_value, check.max_value, check.unit)
        donor = donors.get(key)
        if donor is None:
            continue
        check.measured = donor.measured
        check.source = donor.source
        check.marker = donor.marker
        check.status = donor.status
        check.note = donor.note

    return checks, vision_data


def _safe_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _make_check(rule: dict, value: Optional[float], source: str, marker: Optional[dict]) -> Check:
    if rule.get("max_value") is None and rule.get("min_value") is not None:
        limit_text = f"لا يقل عن {rule['min_value']} {rule['unit']}"
    elif rule.get("min_value") is None and rule.get("max_value") is not None:
        limit_text = f"لا يزيد عن {rule['max_value']} {rule['unit']}"
    else:
        limit_text = f"بين {rule['min_value']} و {rule['max_value']} {rule['unit']}"

    check = Check(
        rule_id=rule["id"],
        rule_name=rule["rule_name"],
        category=rule["category"],
        unit=rule["unit"],
        limit_text=limit_text,
        min_value=rule.get("min_value"),
        max_value=rule.get("max_value"),
        measured=value,
        source=source,
        marker=marker,
    )

    if value is None:
        check.status = "unknown"
        check.note = "لم يتم العثور على قياس في المخطط"
    else:
        above_min = check.min_value is None or value >= check.min_value
        below_max = check.max_value is None or value <= check.max_value
        check.status = "pass" if (above_min and below_max) else "fail"
        check.note = "مطابق للاشتراط" if check.status == "pass" else "مخالفة للاشتراط"

    return check


def run_checks(
    rules: List[dict],
    drawing: Drawing,
    path: str,
    extension: str,
    manual: Dict[int, float],
) -> Tuple[List[Check], Optional[dict]]:
    """يحاول محرك الرؤية بالذكاء الاصطناعي أولاً، ويعود لمحرك النص عند غياب مفتاح الواجهة."""
    ai_result = build_checks_ai(rules, drawing, path, extension, manual)
    if ai_result is not None:
        return ai_result
    return build_checks(rules, drawing, manual), None


def render_pages(drawing: Drawing, path: str, checks: List[Check], report_id: str) -> List[str]:
    os.makedirs(ANNOTATED_DIR, exist_ok=True)
    circles: Dict[int, List[Tuple[int, Check]]] = {}

    for position, check in enumerate(checks, start=1):
        if check.marker and check.status == "fail":
            circles.setdefault(check.marker["page"], []).append((position, check))

    if drawing.kind == "pdf":
        return _render_pdf(path, circles, report_id, drawing)
    if drawing.kind == "dxf":
        return _render_dxf(path, circles, report_id)
    if drawing.kind == "image":
        return _render_image(path, circles, report_id)
    return []


def _draw_marker(draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float, label: str) -> None:
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline="red", width=4)
    draw.text((cx + radius, cy - radius - 2), label, fill="red")


def _render_pdf(path: str, circles: Dict[int, List[Tuple[int, Check]]], report_id: str, drawing: Drawing) -> List[str]:
    scale = 2.0
    document = pdfium.PdfDocument(path)
    outputs: List[str] = []

    for page_index in range(len(document)):
        page = document[page_index]
        image = page.render(scale=scale).to_pil().convert("RGB")
        page_height_pts = float(page.get_height())

        page_checks = circles.get(page_index, [])
        if page_checks:
            draw = ImageDraw.Draw(image)
            for index, check in page_checks:
                marker = check.marker
                if marker.get("pixel"):
                    px, py = float(marker["x"]), float(marker["y"])
                else:
                    px = marker["x"] * scale
                    py = (page_height_pts - marker["y"]) * scale
                radius = max(22, marker["size"] * scale * 2.2)
                _draw_marker(draw, px, py, radius, f"{index}")

        output_name = f"{report_id}_page{page_index + 1}.png"
        output_path = os.path.join(ANNOTATED_DIR, output_name)
        image.save(output_path)
        outputs.append(output_name)

    return outputs


def _render_dxf_image(path: str) -> Optional[Image.Image]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    dpi = 120
    figure = plt.figure(figsize=(16, 12), dpi=dpi)
    axes = figure.add_axes([0, 0, 1, 1])
    axes.set_axis_off()
    Frontend(RenderContext(doc), MatplotlibBackend(axes)).draw_layout(msp, finalize=True)
    figure.set_size_inches(16, 12, forward=True)
    figure.canvas.draw()

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=dpi, facecolor="white")
    plt.close(figure)
    buffer.seek(0)

    with Image.open(buffer) as source:
        return source.convert("RGB")


def _render_dxf(path: str, circles: Dict[int, List[Tuple[int, Check]]], report_id: str) -> List[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    dpi = 120
    figure = plt.figure(figsize=(16, 12), dpi=dpi)
    axes = figure.add_axes([0, 0, 1, 1])
    axes.set_axis_off()
    Frontend(RenderContext(doc), MatplotlibBackend(axes)).draw_layout(msp, finalize=True)
    figure.set_size_inches(16, 12, forward=True)
    figure.canvas.draw()

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=dpi, facecolor="white")
    plt.close(figure)
    buffer.seek(0)

    with Image.open(buffer) as source:
        image = source.convert("RGB")

    page_checks = circles.get(0, [])
    if page_checks:
        draw = ImageDraw.Draw(image)
        width, height = image.size
        radius = max(24, int(min(width, height) * 0.02))
        for index, check in page_checks:
            marker = check.marker
            if marker.get("pixel"):
                px, py = float(marker["x"]), float(marker["y"])
            else:
                px, py = axes.transData.transform((marker["x"], marker["y"]))
            _draw_marker(draw, float(px), float(py), float(radius), f"{index}")

    output_name = f"{report_id}_dxf.png"
    output_path = os.path.join(ANNOTATED_DIR, output_name)
    image.save(output_path)
    return [output_name]


def _render_image(path: str, circles: Dict[int, List[Tuple[int, Check]]], report_id: str) -> List[str]:
    with Image.open(path) as source:
        image = source.convert("RGB")

    page_checks = circles.get(0, [])
    if page_checks:
        draw = ImageDraw.Draw(image)
        width, height = image.size
        radius = max(24, int(min(width, height) * 0.02))
        for index, check in page_checks:
            marker = check.marker
            if marker is None:
                continue
            _draw_marker(draw, marker["x"], marker["y"], float(radius), f"{index}")

    output_name = f"{report_id}_image.png"
    output_path = os.path.join(ANNOTATED_DIR, output_name)
    image.save(output_path)
    return [output_name]


def new_report_id() -> str:
    return uuid4().hex[:12]
