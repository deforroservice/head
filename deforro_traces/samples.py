"""Synthetic DEFORRO exports for sandbox and load testing."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

# commodity -> (hs heading, description, scientific name, common name, producer countries)
COMMODITIES = [
    ("1801", "Cocoa beans, whole, raw", "Theobroma cacao", "Cacao", ["GH", "CI", "CM"]),
    ("0901", "Coffee, not roasted", "Coffea arabica", "Arabica coffee", ["ET", "CO", "BR"]),
    ("1511", "Crude palm oil", "Elaeis guineensis", "Oil palm", ["ID", "MY"]),
    ("1201", "Soya beans", "Glycine max", "Soybean", ["BR"]),
    ("4001", "Natural rubber, smoked sheets", "Hevea brasiliensis", "Rubber tree", ["TH", "CI"]),
    ("4407", "Sawn wood, tropical", "Triplochiton scleroxylon", "Ayous", ["CM"]),
]
# Rough interior points to scatter plots around (lat, lon).
CENTROIDS = {
    "GH": (6.7, -1.6), "CI": (6.9, -5.3), "CM": (4.0, 11.5), "ET": (7.7, 36.8),
    "CO": (4.6, -75.7), "BR": (-12.5, -55.7), "ID": (0.5, 101.4), "MY": (3.8, 102.3),
    "TH": (7.9, 98.4),
}
EU_PORTS = ["BE", "NL", "DE", "FR", "IT", "ES"]


def _plot(rng: random.Random, country: str, hectares: float) -> dict[str, Any]:
    lat0, lon0 = CENTROIDS[country]
    lat = lat0 + rng.uniform(-1.0, 1.0)
    lon = lon0 + rng.uniform(-1.0, 1.0)
    props: dict[str, Any] = {"ProducerCountry": country, "Area": round(hectares, 4)}
    if hectares <= 4 and rng.random() < 0.3:
        geometry = {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}
    else:
        # Irregular hexagon with roughly the requested area.
        radius_m = math.sqrt(hectares * 10_000 / (1.5 * math.sqrt(3)))
        ring = []
        for k in range(6):
            angle = math.radians(60 * k + rng.uniform(-10, 10))
            r = radius_m * rng.uniform(0.85, 1.15)
            dlat = r * math.sin(angle) / 111_320
            dlon = r * math.cos(angle) / (111_320 * math.cos(math.radians(lat)))
            ring.append([round(lon + dlon, 6), round(lat + dlat, 6)])
        ring.append(ring[0])
        geometry = {"type": "Polygon", "coordinates": [ring]}
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": props, "geometry": geometry}]}


def generate(
    count: int,
    seed: int = 7,
    prefix: str = "DEF",
    risky_rate: float = 0.05,
    invalid_rate: float = 0.03,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    records = []
    for n in range(1, count + 1):
        hs, desc, sci, common, countries = rng.choice(COMMODITIES)
        origin = rng.choice(countries)
        port = rng.choice(EU_PORTS)
        producers = []
        for p in range(rng.randint(1, 5)):
            producers.append(
                {
                    "country": origin,
                    "name": f"Cooperative {origin}-{rng.randint(100, 999)}",
                    "geojson": _plot(rng, origin, rng.choice([0.8, 1.5, 3.2, 6.0, 12.5, 40.0])),
                }
            )
        roll = rng.random()
        level = "negligible"
        if roll < risky_rate:
            level = rng.choice(["standard", "high"])
        record: dict[str, Any] = {
            "internal_reference": f"{prefix}-{n:06d}",
            "activity_type": "IMPORT",
            "country_of_activity": port,
            "border_cross_country": port,
            "risk": {
                "level": level,
                "score": round(rng.uniform(0, 0.1) if level == "negligible" else rng.uniform(0.4, 0.95), 3),
                "assessment_id": f"RA-{n:06d}",
            },
            "commodities": [
                {
                    "description": desc,
                    "hs_heading": hs,
                    "net_weight_kg": rng.choice([500, 1200, 5000, 12000, 24000]),
                    "species": [{"scientific_name": sci, "common_name": common}],
                    "producers": producers,
                }
            ],
        }
        if rng.random() < invalid_rate:
            # Classic data-quality slips DEFORRO should catch before TRACES does.
            breakage = rng.choice(["open_ring", "latlon_swap", "no_border"])
            geom = producers[0]["geojson"]["features"][0]["geometry"]
            if breakage == "open_ring" and geom["type"] == "Polygon":
                geom["coordinates"][0].pop()
            elif breakage == "latlon_swap":
                geom["coordinates"] = _swap(geom["coordinates"], 200)
            else:
                record.pop("border_cross_country")
        records.append(record)
    return records


def _swap(coords: Any, bump: float) -> Any:
    if coords and isinstance(coords[0], (int, float)):
        return [coords[1], coords[0] + bump]
    return [_swap(c, bump) for c in coords]


def write_jsonl(records: list[dict[str, Any]], path: str | Path) -> None:
    with Path(path).open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
