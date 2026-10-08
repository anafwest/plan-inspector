"""محرك حتمي للفحص الهندسي المكاني: قياس الارتدادات والمسطحات من هندسة DXF مباشرة."""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import ezdxf
from shapely.affinity import scale as scale_geom
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

Point2D = Tuple[float, float]

PROPERTY_HINTS = ("PROPERTY", "BOUNDARY", "PLOT", "LOT", "SITE", "LAND", "حدود", "أرض", "ارض")
BUILDING_HINTS = ("BUILDING", "WALL", "FOOTPRINT", "MASS", "BASEMENT", "مبني", "مبنى", "بناء")
STREET_HINTS = ("ROAD", "STREET", "شارع", "طريق")

COMPASS = (
    (0.0, "شرق"),
    (90.0, "شمال"),
    (180.0, "غرب"),
    (270.0, "جنوب"),
)

DEFAULT_LIMITS = {
    "side_rear_min": 2.0,
    "front_min": None,
    "coverage_gf_max": None,
}


def _layer_name(entity) -> str:
    try:
        return str(entity.dxf.layer or "")
    except Exception:
        return ""


def _matches(layer: str, hints: Sequence[str]) -> bool:
    upper = layer.upper()
    return any(hint.upper() in upper or hint in layer for hint in hints)


def _polyline_points(entity) -> List[Point2D]:
    kind = entity.dxftype()
    if kind == "LWPOLYLINE":
        return [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
    if kind == "POLYLINE":
        return [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
    return []


def _is_closed(entity, points: List[Point2D]) -> bool:
    for attr in ("closed", "is_closed"):
        try:
            if getattr(entity, attr):
                return True
        except Exception:
            pass
    if len(points) >= 4 and math.dist(points[0], points[-1]) <= 1e-6:
        return True
    return False


def _make_polygon(points: List[Point2D]) -> Optional[Polygon]:
    if len(points) >= 4 and math.dist(points[0], points[-1]) <= 1e-9:
        points = points[:-1]
    if len(points) < 3:
        return None
    polygon = Polygon(points)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty or polygon.area <= 0:
        return None
    if polygon.geom_type == "MultiPolygon":
        polygon = max(polygon.geoms, key=lambda part: part.area)
    if polygon.geom_type != "Polygon":
        return None
    return polygon


def _line_loops(lines: Sequence[Tuple[Point2D, Point2D]]) -> List[Polygon]:
    def key(point: Point2D) -> Point2D:
        return (round(point[0], 3), round(point[1], 3))

    adjacency: Dict[Point2D, List[Tuple[Point2D, int]]] = {}
    coords: Dict[Point2D, Point2D] = {}
    for index, (start, end) in enumerate(lines):
        ka, kb = key(start), key(end)
        if ka == kb:
            continue
        coords[ka], coords[kb] = start, end
        adjacency.setdefault(ka, []).append((kb, index))
        adjacency.setdefault(kb, []).append((ka, index))

    used: set = set()
    polygons: List[Polygon] = []
    for start_node in list(adjacency):
        if all(edge_id in used for _, edge_id in adjacency[start_node]):
            continue
        path = [start_node]
        current = start_node
        previous: Optional[Point2D] = None
        while True:
            options = [
                (neighbor, edge_id)
                for neighbor, edge_id in adjacency[current]
                if edge_id not in used and neighbor != previous
            ]
            if not options:
                break
            neighbor, edge_id = options[0]
            used.add(edge_id)
            previous, current = current, neighbor
            if current == start_node:
                break
            path.append(current)
        if current == start_node and len(path) >= 3:
            polygon = _make_polygon([coords[node] for node in path])
            if polygon is not None:
                polygons.append(polygon)
    return polygons


def _compass(normal_x: float, normal_y: float) -> str:
    angle = math.degrees(math.atan2(normal_y, normal_x)) % 360
    return min(
        COMPASS,
        key=lambda item: min(abs(angle - item[0]), 360 - abs(angle - item[0])),
    )[1]


def _edge_records(property_poly: Polygon, target, front_index: Optional[int]) -> List[dict]:
    coords = list(property_poly.exterior.coords)
    eps = max(1e-6, property_poly.area**0.5 * 1e-6)
    records: List[dict] = []
    for index in range(len(coords) - 1):
        edge = LineString([coords[index], coords[index + 1]])
        mid_x = (coords[index][0] + coords[index + 1][0]) / 2
        mid_y = (coords[index][1] + coords[index + 1][1]) / 2
        dx = coords[index + 1][0] - coords[index][0]
        dy = coords[index + 1][1] - coords[index][1]
        length = math.hypot(dx, dy) or 1.0
        nx, ny = dy / length, -dx / length
        if property_poly.contains(Point(mid_x + nx * eps, mid_y + ny * eps)):
            nx, ny = -nx, -ny
        records.append(
            {
                "index": index,
                "direction": _compass(nx, ny),
                "distance_m": round(edge.distance(target), 2),
            }
        )

    if front_index is not None and records:
        rear_index = (front_index + len(records) // 2) % len(records)
        for index, record in enumerate(records):
            if index == front_index:
                record["edge"] = "front"
            elif index == rear_index:
                record["edge"] = "rear"
            else:
                record["edge"] = "side"
    else:
        for record in records:
            record["edge"] = "unlabeled"
    return records


def audit_dxf(path: str, limits: Optional[dict] = None) -> dict:
    limits = {**DEFAULT_LIMITS, **(limits or {})}

    try:
        doc = ezdxf.readfile(path)
        msp = doc.modelspace()
    except Exception as exc:
        raise ValueError(f"فشل في قراءة ملف المخطط الهندسي: {exc}") from exc

    dimensions_count = len(list(msp.query("DIMENSION")))

    property_candidates: List[Tuple[str, Polygon]] = []
    building_candidates: List[Tuple[str, Polygon]] = []
    generic: List[Tuple[str, Polygon]] = []
    line_entities: List[Tuple[Point2D, Point2D]] = []
    street_lines: List[LineString] = []

    for entity in msp:
        kind = entity.dxftype()
        layer = _layer_name(entity)
        if kind in ("LWPOLYLINE", "POLYLINE"):
            points = _polyline_points(entity)
            if not _is_closed(entity, points):
                continue
            polygon = _make_polygon(points)
            if polygon is None:
                continue
            if _matches(layer, PROPERTY_HINTS):
                property_candidates.append((layer, polygon))
            elif _matches(layer, BUILDING_HINTS):
                building_candidates.append((layer, polygon))
            else:
                generic.append((layer, polygon))
            if _matches(layer, STREET_HINTS):
                street_lines.append(polygon.exterior)
        elif kind == "LINE":
            try:
                start = (float(entity.dxf.start.x), float(entity.dxf.start.y))
                end = (float(entity.dxf.end.x), float(entity.dxf.end.y))
            except Exception:
                continue
            line_entities.append((start, end))
            if _matches(layer, STREET_HINTS) and start != end:
                street_lines.append(LineString([start, end]))

    explicit = {
        "property": bool(property_candidates),
        "building": bool(building_candidates),
    }

    if not property_candidates:
        pool = [poly for _, poly in generic] + [poly for _, poly in building_candidates]
        if pool:
            property_candidates = [("auto", max(pool, key=lambda poly: poly.area))]
    if not building_candidates:
        property_poly_first = max((poly for _, poly in property_candidates), default=None)
        pool = [poly for _, poly in generic]
        if property_poly_first is not None:
            pool = [
                poly
                for poly in pool
                if property_poly_first.contains(poly) and poly.area < property_poly_first.area * 0.95
            ]
        if pool:
            building_candidates = [("auto", max(pool, key=lambda poly: poly.area))]

    if not property_candidates and not building_candidates and line_entities:
        loops = _line_loops(line_entities)
        if loops:
            loops.sort(key=lambda poly: poly.area, reverse=True)
            property_candidates = [("auto", loops[0])]
            contained = [
                poly for poly in loops[1:] if loops[0].contains(poly) and poly.area < loops[0].area * 0.95
            ]
            if contained:
                building_candidates = [("auto", contained[0])]

    property_poly = max((poly for _, poly in property_candidates), default=None)
    building_poly = max((poly for _, poly in building_candidates), default=None)

    scale = 1.0
    if property_poly is not None:
        min_x, min_y, max_x, max_y = property_poly.bounds
        if max(max_x - min_x, max_y - min_y) > 500:
            scale = 0.001

    def convert(polygon: Optional[Polygon]) -> Optional[Polygon]:
        if polygon is None:
            return None
        if scale == 1.0:
            return polygon
        return scale_geom(polygon, xfact=scale, yfact=scale, origin=(0, 0))

    property_poly = convert(property_poly)
    building_poly = convert(building_poly)
    street_lines = [
        LineString([(x * scale, y * scale) for x, y in line.coords]) for line in street_lines
    ]

    violations: List[dict] = []
    recommendations: List[str] = []
    missing_roles = [role for role, found in explicit.items() if not found]

    if property_poly is None or building_poly is None:
        missing = []
        if property_poly is None:
            missing.append("حدود الأرض (BOUNDARY)")
        if building_poly is None:
            missing.append("مسقط المبنى (BUILDING)")
        setbacks_analysis = {
            "status": "Warning",
            "message": "لم يتم العثور على هندسة كافية لحساب الارتدادات: " + "، ".join(missing),
        }
        violations.append(
            {
                "code": "SYS-LAYER-MISSING",
                "severity": "Critical",
                "description": (
                    "تعذر التحقق الآلي من الارتدادات لعدم توفر أشكال مغلقة لـ " + "، ".join(missing) + "."
                ),
            }
        )
        recommendations.append(
            "ارسم حدود الأرض ومسقط المبنى كخطوط مغلقة (POLYLINE) في طبقات مسمّاة بشكل صحيح."
        )
    else:
        is_contained = property_poly.contains(building_poly)
        min_distance = round(property_poly.exterior.distance(building_poly), 2)

        front_index: Optional[int] = None
        street_detected = bool(street_lines)
        if street_detected:
            street = unary_union(street_lines)
            coords = list(property_poly.exterior.coords)
            distances = [
                LineString([coords[index], coords[index + 1]]).distance(street)
                for index in range(len(coords) - 1)
            ]
            front_index = min(range(len(distances)), key=lambda i: distances[i])

        edge_records = _edge_records(property_poly, building_poly, front_index)
        labeled = [r for r in edge_records if r["edge"] in ("side", "rear")]
        side_rear_min = min((r["distance_m"] for r in labeled), default=min_distance)
        front_setback = None
        if front_index is not None and front_index < len(edge_records):
            front_setback = edge_records[front_index]["distance_m"]

        coverage = round(building_poly.area / property_poly.area * 100, 1) if property_poly.area else None

        setbacks_analysis = {
            "status": "Success",
            "calculated_min_setback_meters": min_distance,
            "is_contained": is_contained,
            "side_rear_min_setback_meters": round(side_rear_min, 2),
            "front_setback_meters": front_setback,
            "street_detected": street_detected,
            "edge_setbacks": edge_records,
            "layer_detection": "explicit" if not missing_roles else "auto",
        }

        if not is_contained:
            violations.append(
                {
                    "code": "SBC-BUILDING-OUTSIDE-PLOT",
                    "severity": "Critical",
                    "description": "مسقط المبنى يتجاوز حدود الأرض — يجب أن يعمل المسقط بالكامل داخل القسيمة.",
                }
            )
            recommendations.append(
                "عدّل تصميم المسقط ليقع بالكامل داخل حدود الأرض مع الحفاظ على الارتدادات النظامية."
            )

        required_side_rear = limits.get("side_rear_min")
        if required_side_rear is not None and side_rear_min < float(required_side_rear) - 1e-9:
            violations.append(
                {
                    "code": "SBC-110-SETBACK",
                    "severity": "Critical",
                    "description": (
                        f"الارتداد الجانبي/الخلفي المحسوب ({side_rear_min} م) يقل عن الحد النظامي "
                        f"({required_side_rear} م)."
                    ),
                }
            )
            recommendations.append(
                "تعديل مسقط المبنى لزيادة مسافة الارتداد الجانبي والخلفي وفقاً لمتطلبات الكود السعودي."
            )

        required_front = limits.get("front_min")
        if (
            required_front is not None
            and front_setback is not None
            and front_setback < float(required_front) - 1e-9
        ):
            violations.append(
                {
                    "code": "SBC-111-FRONT-SETBACK",
                    "severity": "Critical",
                    "description": (
                        f"الارتداد الأمامي المحسوب ({front_setback} م) يقل عن الحد النظامي "
                        f"({required_front} م)."
                    ),
                }
            )
            recommendations.append(
                "إعادة تخطيط الواجهة الأمامية لتحقيق الارتداد الأمامي المطلوب أمام الشارع."
            )

        coverage_max = limits.get("coverage_gf_max")
        if coverage is not None and coverage_max is not None and coverage > float(coverage_max) + 1e-9:
            violations.append(
                {
                    "code": "SBC-301-COVERAGE",
                    "severity": "Major",
                    "description": (
                        f"نسبة البناء المحسوبة ({coverage}%) تتجاوز الحد الأقصى ({coverage_max}%)."
                    ),
                }
            )
            recommendations.append("تقليل مساحة مسقط المبنى لضمان الالتزام بنسبة البناء النظامية.")

        if missing_roles:
            violations.append(
                {
                    "code": "SYS-LAYER-MISSING",
                    "severity": "High",
                    "description": (
                        "أبعُدت الأشكال آليًا لعدم تسمية طبقات "
                        + " و ".join(missing_roles)
                        + " هندسياً بشكل صحيح — راجع النتيجة يدوياً."
                    ),
                }
            )
            recommendations.append(
                "تسمية الطبقات هندسياً (BOUNDARY / BUILDING) لضمان دقة الفحص الآلي في المرة القادمة."
            )

        if not violations:
            recommendations.append("الارتدادات الهندسية مستوفية للاشتراطات التنظيمية.")
        if front_setback is None:
            recommendations.append(
                "لم يُكتشف خط الشارع في المخطط — أضف طبقة ROAD لتحديد الارتداد الأمامي تلقائياً."
            )

    compliance_status = "Non-Compliant" if violations else "Compliant"

    return {
        "platform_metadata": {
            "engine": "Deterministic Spatial & Rule-Based Engine",
            "standard": "Saudi Building Code (SBC)",
        },
        "spatial_computations": {
            "setbacks_analysis": setbacks_analysis,
            "extracted_dimensions_count": dimensions_count,
            "property_area_sqm": round(property_poly.area, 1) if property_poly else None,
            "building_area_sqm": round(building_poly.area, 1) if building_poly else None,
            "units_applied": "m" if scale == 1.0 else "mm converted to m",
        },
        "regulatory_audit": {
            "compliance_status": compliance_status,
            "violations_detected": violations,
            "engineering_recommendations": recommendations,
            "applied_limits": {
                "side_rear_min_m": limits.get("side_rear_min"),
                "front_min_m": limits.get("front_min"),
                "coverage_gf_max_percent": limits.get("coverage_gf_max"),
            },
        },
    }
