"""Pre-flight validation of statements and their GeoJSON, before anything hits TRACES.

These checks mirror the common EUDR rules so that bad records fail fast and locally.
They are not a substitute for the official "Validation Rules" page: TRACES remains
the authority, and its rejections are recorded in the ledger as-is.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

from .models import Statement

ACTIVITY_TYPES = {"IMPORT", "EXPORT", "DOMESTIC"}
KNOWN_OPERATOR_ROLES = {"OPERATOR"}
GEOMETRY_TYPES = {"Point", "MultiPoint", "Polygon", "MultiPolygon"}

# Annex I headings/chapters (prefix match). Used only for warnings: the regulation
# lists headings at 4-6 digit level and some are partial, so treat a miss as "check".
ANNEX_I_HS_PREFIXES = (
    # cattle
    "0102", "0201", "0202", "0206", "1602", "4101", "4104", "4107",
    # cocoa
    "1801", "1802", "1803", "1804", "1805", "1806",
    # coffee
    "0901",
    # oil palm
    "1207", "1511", "1513", "2306", "2905", "2915", "3401", "3823",
    # rubber
    "4001", "4005", "4006", "4007", "4008", "4010", "4011", "4012", "4013",
    "4015", "4016", "4017",
    # soya
    "1201", "1208", "1507", "2304",
    # wood
    "44", "47", "48", "49", "9401", "9403", "9406",
)

COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
HS_RE = re.compile(r"^\d{4}(\d{2}){0,2}$")
EARTH_RADIUS_M = 6_371_008.8
POINT_MAX_HECTARES = 4.0
MIN_DECIMALS = 6


@dataclass
class Issue:
    level: str  # "error" | "warning"
    code: str
    message: str
    path: str = ""

    def __str__(self) -> str:
        where = f"{self.path}: " if self.path else ""
        return f"[{self.level}] {self.code} {where}{self.message}"


@dataclass
class ValidationConfig:
    # Upper bound on the serialised GeoJSON per producer. Check the current limit in
    # the official validation rules; this default is deliberately conservative.
    max_geojson_bytes: int = 25 * 1024 * 1024
    require_geolocation: bool = True
    check_precision: bool = True


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, code: str, message: str, path: str = "") -> None:
        self.issues.append(Issue("error", code, message, path))

    def warn(self, code: str, message: str, path: str = "") -> None:
        self.issues.append(Issue("warning", code, message, path))


def validate_statement(stmt: Statement, config: ValidationConfig | None = None) -> ValidationResult:
    config = config or ValidationConfig()
    r = ValidationResult()

    if not stmt.internal_reference or len(stmt.internal_reference) > 50:
        r.error("REF", "internal reference is required (max 50 chars); it is the idempotency key")
    if stmt.activity_type not in ACTIVITY_TYPES:
        r.error("ACTIVITY", f"activityType must be one of {sorted(ACTIVITY_TYPES)}, got {stmt.activity_type!r}")
    if stmt.operator_role not in KNOWN_OPERATOR_ROLES:
        r.warn("ROLE", f"operatorRole {stmt.operator_role!r} is not one the sandbox knows; check the XSD")
    for label, value in (
        ("countryOfActivity", stmt.country_of_activity),
        ("borderCrossCountry", stmt.border_cross_country),
    ):
        if value is not None and not COUNTRY_RE.match(value):
            r.error("COUNTRY", f"{label} must be an ISO 3166-1 alpha-2 code, got {value!r}")
    if stmt.activity_type in ("IMPORT", "EXPORT") and not stmt.border_cross_country:
        r.error("BORDER", f"borderCrossCountry is required for {stmt.activity_type}")
    if not stmt.commodities:
        r.error("COMMODITY", "at least one commodity is required")
    for ref in stmt.grouped_declarations:
        if not re.match(r"^[A-Z0-9]{8,20}$", ref):
            r.error("GROUPED", f"grouped declaration {ref!r} does not look like a DDS reference number")

    for ci, c in enumerate(stmt.commodities, 1):
        cp = f"commodities[{ci}]"
        if not c.description:
            r.error("DESCRIPTION", "descriptionOfGoods is required", cp)
        if not HS_RE.match(c.hs_heading or ""):
            r.error("HS", f"hsHeading must be 4, 6 or 8 digits, got {c.hs_heading!r}", cp)
        elif not c.hs_heading.startswith(ANNEX_I_HS_PREFIXES):
            r.warn("HS_ANNEX", f"hsHeading {c.hs_heading} is not a recognised Annex I heading", cp)
        if c.net_weight is None and c.supplementary_unit is None:
            r.error("MEASURE", "netWeight or supplementaryUnit is required", cp)
        if c.net_weight is not None and c.net_weight <= 0:
            r.error("MEASURE", f"netWeight must be positive, got {c.net_weight}", cp)
        if (c.supplementary_unit is None) != (c.supplementary_unit_qualifier is None):
            r.error("MEASURE", "supplementaryUnit and supplementaryUnitQualifier go together", cp)
        for si, sp in enumerate(c.species, 1):
            if not sp.scientific_name:
                r.error("SPECIES", "scientificName is required", f"{cp}.species[{si}]")
        if config.require_geolocation and stmt.activity_type in ACTIVITY_TYPES and not c.producers:
            r.error("PRODUCER", "at least one producer with geolocation is required", cp)
        for pi, p in enumerate(c.producers, 1):
            pp = f"{cp}.producers[{pi}]"
            if not COUNTRY_RE.match(p.country or ""):
                r.error("COUNTRY", f"producer country must be ISO alpha-2, got {p.country!r}", pp)
            if p.geojson is None:
                if config.require_geolocation:
                    r.error("GEO_MISSING", "producer has no geometry", pp)
                continue
            size = len(json.dumps(p.geojson, separators=(",", ":")))
            if size > config.max_geojson_bytes:
                r.error("GEO_SIZE", f"GeoJSON is {size} bytes, limit {config.max_geojson_bytes}", pp)
            validate_geojson(p.geojson, r, pp, config)
    return r


def validate_geojson(doc: object, r: ValidationResult, path: str, config: ValidationConfig) -> None:
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection":
        r.error("GEO_TYPE", "GeoJSON must be a FeatureCollection", path)
        return
    features = doc.get("features")
    if not isinstance(features, list) or not features:
        r.error("GEO_EMPTY", "FeatureCollection has no features", path)
        return
    low_precision = False
    for fi, feature in enumerate(features, 1):
        precise = False  # any coordinate with >= MIN_DECIMALS (floats drop trailing zeros)
        fp = f"{path}.features[{fi}]"
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            r.error("GEO_FEATURE", "each entry must be a Feature", fp)
            continue
        geom = feature.get("geometry")
        if not isinstance(geom, dict) or geom.get("type") not in GEOMETRY_TYPES:
            r.error("GEO_GEOMETRY", f"geometry type must be one of {sorted(GEOMETRY_TYPES)}", fp)
            continue
        props = feature.get("properties") or {}
        coords = geom.get("coordinates")
        gtype = geom["type"]
        try:
            if gtype == "Point":
                precise |= _check_position(coords, r, fp)
                area = props.get("Area")
                if isinstance(area, (int, float)) and area > POINT_MAX_HECTARES:
                    r.error(
                        "GEO_POINT_AREA",
                        f"plot declares Area={area} ha; plots over {POINT_MAX_HECTARES} ha need a polygon",
                        fp,
                    )
            elif gtype == "MultiPoint":
                for pos in coords:
                    precise |= _check_position(pos, r, fp)
            elif gtype == "Polygon":
                precise |= _check_polygon(coords, r, fp)
            elif gtype == "MultiPolygon":
                for poly in coords:
                    precise |= _check_polygon(poly, r, fp)
        except (TypeError, ValueError, IndexError):
            r.error("GEO_COORDS", "malformed coordinates", fp)
            continue
        low_precision |= not precise
    if low_precision and config.check_precision:
        r.warn("GEO_PRECISION", f"a feature has no coordinate with {MIN_DECIMALS}+ decimal places", path)


def _check_position(pos: object, r: ValidationResult, path: str) -> bool:
    if not isinstance(pos, (list, tuple)) or len(pos) < 2:
        raise ValueError("position")
    lon, lat = float(pos[0]), float(pos[1])
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        r.error("GEO_RANGE", f"position {pos[:2]} out of range (expects [lon, lat])", path)
    return _decimals(pos[0]) >= MIN_DECIMALS or _decimals(pos[1]) >= MIN_DECIMALS


def _check_polygon(rings: object, r: ValidationResult, path: str) -> bool:
    if not isinstance(rings, list) or not rings:
        raise ValueError("polygon")
    precise = False
    for ri, ring in enumerate(rings):
        if len(ring) < 4:
            r.error("GEO_RING", f"ring {ri} needs at least 4 positions", path)
            continue
        if list(ring[0][:2]) != list(ring[-1][:2]):
            r.error("GEO_RING", f"ring {ri} is not closed (first != last position)", path)
        for pos in ring:
            precise |= _check_position(pos, r, path)
    if rings and len(rings[0]) >= 4 and ring_area_m2(rings[0]) == 0:
        r.error("GEO_RING", "outer ring has zero area", path)
    return precise


def _decimals(value: object) -> int:
    text = repr(value) if isinstance(value, float) else str(value)
    if "e" in text.lower():
        return MIN_DECIMALS  # scientific notation: don't second-guess precision
    return len(text.split(".", 1)[1]) if "." in text else 0


def ring_area_m2(ring: list) -> float:
    """Approximate geodesic area of a lon/lat ring (spherical excess formula)."""
    total = 0.0
    for (lon1, lat1, *_), (lon2, lat2, *_) in zip(ring, ring[1:]):
        total += math.radians(lon2 - lon1) * (
            2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2))
        )
    return abs(total) * EARTH_RADIUS_M**2 / 2


def geojson_area_hectares(doc: dict) -> float:
    total = 0.0
    for feature in doc.get("features", []):
        geom = feature.get("geometry") or {}
        if geom.get("type") == "Polygon":
            polys = [geom["coordinates"]]
        elif geom.get("type") == "MultiPolygon":
            polys = geom["coordinates"]
        else:
            continue
        for rings in polys:
            total += ring_area_m2(rings[0]) - sum(ring_area_m2(h) for h in rings[1:])
    return total / 10_000
