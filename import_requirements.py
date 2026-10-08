import json
import os
import re
import sys
import unicodedata

HTML_PATH = r"C:\Users\anaf\OneDrive - Riyadh Municipality\سطح المكتب\برنامج تدقيق رخص البناء ادارة الاشترطات و البنود\v17تدقيق_المخططات (6).html"
OUTPUT_PATH = "wadi_hanifa_rules.json"

CLASSIFICATION_LABELS = {"residential": "سكني", "commercial": "تجاري"}
CODE_LABELS = {"wadi": "كود وادي حنيفة", "saudi": "الكود السعودي", "both": "وادي حنيفة + الكود السعودي"}


def strip_emoji(name: str) -> str:
    return re.sub(
        r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2190-\u21FF]",
        "",
        name,
    ).strip()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ئ", "ي").replace("ؤ", "و").replace("ة", "ه")
    return re.sub(r"[\s\-_]+", " ", text.lower()).strip()


def extract_projects(source: str) -> dict:
    projects = {}

    for block in re.finditer(r"const\s+(DEFAULT_PROJECT_TYPES_LIST|t41Types|t42Types)\s*=\s*\[(.*?)\];", source, re.S):
        for entry in re.finditer(r"\{\s*id:\s*'([^']+)',\s*name:\s*'([^']+)'", block.group(2)):
            projects[entry.group(1)] = entry.group(2).strip()

    for entry in re.finditer(
        r"const\s+(\w+Entry)\s*=\s*\{\s*id:\s*'([^']+)',\s*name:\s*'([^']+)'", source
    ):
        projects[entry.group(2)] = entry.group(3).strip()

    return projects


def extract_categories(source: str) -> dict:
    categories = {}

    for entry in re.finditer(
        r"\{\s*id:\s*'(cat_\w+)',\s*name:\s*'([^']+)',\s*classifications:\s*\[([^\]]*)\],\s*"
        r"codeTypes:\s*\[([^\]]*)\],\s*projects:\s*\[([^\]]*)\]",
        source,
    ):
        categories[entry.group(1)] = {
            "id": entry.group(1),
            "name": strip_emoji(entry.group(2)),
            "classifications": re.findall(r"'([^']+)'", entry.group(3)),
            "codeTypes": re.findall(r"'([^']+)'", entry.group(4)),
            "projects": re.findall(r"'([^']+)'", entry.group(5)),
        }

    return categories


def split_items(block: str) -> list:
    items = []
    depth = 0
    start = None
    in_string = False
    quote = ""
    index = 0

    while index < len(block):
        char = block[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                in_string = False
        elif char in "'\"":
            in_string = True
            quote = char
        elif char in "[{(":
            if depth == 0:
                start = index
            depth += 1
        elif char in "]})":
            depth -= 1
            if depth == 0 and start is not None:
                items.append(block[start : index + 1])
                start = None
        index += 1

    return items


def parse_requirement(item: str) -> dict | None:
    item = item.strip().strip("()").strip()
    item = re.sub(r"^req\s*\(", "", item)

    if item.startswith("{"):
        text = re.search(r"text:\s*'((?:[^'\\]|\\.)*)'", item)
        ref = re.search(r"ref:\s*'((?:[^'\\]|\\.)*)'", item)
        if not text:
            return None
        entry = {"text": text.group(1), "ref": ref.group(1) if ref else ""}
        for key in ("classifications", "codeTypes", "projects", "requestTypes"):
            found = re.search(key + r":\s*\[([^\]]*)\]", item)
            if found:
                entry[key] = re.findall(r"'([^']+)'", found.group(1))
        return entry

    args = re.findall(r"'((?:[^'\\]|\\.)*)'", item)
    if not args:
        return None

    return {"text": args[0], "ref": args[1] if len(args) > 1 else ""}


MIN_WORDS = [
    r"لا\s*يقل\s*عن",
    r"الحد\s*الأدنى",
    r"بحد\s*أدنى",
    r"لا\s*يكـ?ن?\s*أقل\s*من",
    r"لا\s*تقل\s*عن",
    r"لا\s*يقل",
]

MAX_WORDS = [
    r"لا\s*يزيد\s*عن",
    r"لا\s*تتجاوز",
    r"لا\s*يتجاوز",
    r"حد\s*أقصى",
    r"أقصى\s*حد",
    r"ما\s*لا\s*يزيد",
    r"لا\s*تزيد\s*عن",
]

GAP = r"[^0-9٠-٩]{0,18}"
NUMBER = r"([0-9]+(?:\.[0-9]+)?)"


CONCEPT_RULES = {
    "front": ["الواجهة الرئيسيه", "الواجهةRue", "الواجهةالاماميه", "ارتداد امامي", "الارتداد الامامي", "امامي"],
    "side": ["الواجهة الجانبيه", "ارتداد جانبي", "الارتداد الجانبي", "جانبي", "جانبيه"],
    "rear": ["الواجهة الخلفيه", "ارتداد خلفي", "الارتداد الخلفي", "خلفي", "خلفيه"],
    "coverage": ["نسبه البناء", "نسبه تغطيه", "تغطيه الارض", "كثافه البناء", "معامل البناء"],
    "height": ["ارتفاع", "الارتفاعات"],
    "floors": ["عدد الادوار", "ادوار", "طوابق", "دور"],
    "wall_height": ["ارتفاع السور", "الاسوار", "سور", "السياج"],
    "courtyard": ["الفناء الداخلي", "فناء", "الحوش", "حوش"],
    "width": ["عرض الشارع", "العرض", "عرض"],
    "depth": ["العمق", "عمق"],
    "length": ["الطول", "طول"],
    "area": ["المساحه", "مساحه", "مساحه الارض"],
    "facade_ratio": ["نسبه الكتله المصمته", "نسبه فتحه", "نسبه المظلات", "نسبه الارaze"],
}


def detect_concepts(text: str) -> list:
    normalized = normalize(text).replace(" ", "")
    concepts = []

    for concept, keywords in CONCEPT_RULES.items():
        for keyword in keywords:
            if normalize(keyword).replace(" ", "") in normalized:
                concepts.append(concept)
                break

    return concepts


def detect_unit(text: str, number: float | None = None) -> str:
    normalized = normalize(text)

    if number:
        position = normalized.find(str(number))
        if position >= 0:
            tail = normalized[position + len(str(number)) : position + len(str(number)) + 4]
            if tail.startswith("%"):
                return "%"
            if tail.startswith("م2") or tail.startswith("م²"):
                return "م²"
            if re.match(r"^\s*(م|متر)\b", tail) or tail.startswith("متر"):
                return "متر"

    if re.search(r"[0-9]\s*%", normalized):
        return "%"
    if re.search(r"[0-9]\s*م2|[0-9]\s*م²", normalized):
        return "م²"
    if re.search(r"[0-9]\s*(?:دور|طابق|أدوار|طوابق)", normalized):
        return "أدوار"
    return "متر"


def value_near(text: str, words: list) -> float | None:
    normalized = normalize(text)
    for word in words:
        for found in re.finditer(word + GAP + NUMBER, normalized):
            return float(found.group(1))
    return None


def extract_thresholds(text: str) -> dict:
    normalized = normalize(text)
    numbers = re.findall(r"[0-9]+(?:\.[0-9]+)?", normalized)

    minimum = value_near(text, MIN_WORDS)
    maximum = value_near(text, MAX_WORDS)

    if minimum is None and maximum is None:
        return {}

    if minimum is not None and maximum is not None:
        return {}

    if len(numbers) > 1:
        return {}

    value = minimum if minimum is not None else maximum
    if value is None or value <= 0:
        return {}

    return {
        "min_value": minimum,
        "max_value": maximum,
        "unit": detect_unit(text, value),
    }


def main() -> int:
    with open(HTML_PATH, encoding="utf-8") as handle:
        source = handle.read()

    projects = extract_projects(source)
    categories = extract_categories(source)

    body_match = re.search(
        r"function\s+getDefaultRequirements\s*\(\)\s*\{(.*?)\n\}", source, re.S
    )
    if not body_match:
        print("لم يتم العثور على الدالة getDefaultRequirements", file=sys.stderr)
        return 1
    body = body_match.group(1)

    requirements = []
    seen_texts: dict = {}
    skipped = 0
    duplicate_groups: list = []

    for cat_match in re.finditer(r"'(cat_\w+)'\s*:\s*\[(.*?)\n\s*\],", body, re.S):
        cat_id = cat_match.group(1)
        for raw_item in split_items(cat_match.group(2)):
            parsed = parse_requirement(raw_item)
            if not parsed or not parsed["text"].strip():
                continue

            section = section_from_ref(parsed.get("ref", "")) or section_from_category(cat_id)
            prefix = cat_id.rsplit("_", 1)[0]
            section = section or SECTION_FALLBACK.get(prefix, "")
            fingerprint = normalize(parsed["text"])

            if fingerprint in seen_texts:
                kept = seen_texts[fingerprint]
                origin = kept.get("section") or "عام"
                skipped += 1
                if section and section not in kept["also_in"] and section != kept.get("section"):
                    kept["also_in"].append(section)
                duplicate_groups.append(
                    {
                        "text": parsed["text"][:120],
                        "kept_from": origin,
                        "skipped_from": section or "عام",
                        "category_id": cat_id,
                    }
                )
                continue

            category = categories.get(cat_id, {})
            entry = {
                "category_id": cat_id,
                "category": category.get("name", cat_id),
                "text": parsed["text"],
                "ref": parsed.get("ref", ""),
                "section": section,
                "also_in": [],
                "classification": [
                    CLASSIFICATION_LABELS.get(c, c) for c in parsed.get("classifications") or category.get("classifications", [])
                ],
                "code": [
                    CODE_LABELS.get(c, c) for c in parsed.get("codeTypes") or category.get("codeTypes", [])
                ],
                "project": [
                    projects.get(p, p) for p in parsed.get("projects") or category.get("projects", [])
                ],
            }
            entry.update(extract_thresholds(parsed["text"]))
            entry["concepts"] = detect_concepts(parsed["text"])
            requirements.append(entry)
            seen_texts[fingerprint] = entry

    payload = {
        "source": "برنامج تدقيق رخص البناء v17 — إدارة الاشتراطات والبنود",
        "total": len(requirements),
        "duplicates_skipped": skipped,
        "categories": len(categories),
        "projects": len(projects),
        "duplicate_groups": duplicate_groups,
        "requirements": requirements,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    with_thresholds = sum(
        1 for r in requirements if r.get("min_value") is not None or r.get("max_value") is not None
    )
    print(f"التصنيفات: {len(categories)} | الاشتراطات الفريدة: {len(requirements)} | مكررة تم تخطيها: {skipped}")
    print(f"أنواع المشاريع: {len(projects)} | بنود ذات حد رقمي قابل للفحص الآلي: {with_thresholds}")
    return 0


def section_from_ref(ref: str) -> str:
    found = re.search(r"(T\d\.\d)", ref or "")
    return found.group(1) if found else ""


def section_from_category(cat_id: str) -> str:
    found = re.search(r"t(\d\d)", cat_id or "")
    if not found:
        return ""
    return "T" + found.group(1)


SECTION_FALLBACK = {
    "cat_rb": "الكود السعودي S.B.C.",
    "cat_villa": "الكود السعودي S.B.C.",
    "cat_villa_attached": "الكود السعودي S.B.C.",
}


if __name__ == "__main__":
    raise SystemExit(main())
