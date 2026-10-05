"""Load DEFORRO shipment exports (JSONL or CSV) into Statements.

JSONL: one statement per line (see examples/shipments.example.jsonl).
CSV:   one row per producer plot; rows sharing internal_reference form one statement,
       and rows sharing (internal_reference, hs_heading, description) one commodity.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

from .models import Commodity, Producer, Risk, Species, Statement


@dataclass
class LoadedRecord:
    key: str  # internal reference, or "line N" when that is missing
    statement: Statement | None
    errors: list[str] = field(default_factory=list)


def load(path: str | Path) -> Iterator[LoadedRecord]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        yield from load_csv(path)
    else:
        yield from load_jsonl(path)


def load_jsonl(path: Path) -> Iterator[LoadedRecord]:
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key = f"line {lineno}"
            try:
                data = json.loads(line)
                key = data.get("internal_reference") or key
                yield LoadedRecord(key, statement_from_dict(data, path.parent))
            except (ValueError, KeyError, TypeError, OSError, InvalidOperation) as e:
                yield LoadedRecord(key, None, [f"{type(e).__name__}: {e}"])


def statement_from_dict(data: dict[str, Any], base_dir: Path) -> Statement:
    risk = data.get("risk")
    return Statement(
        internal_reference=str(data["internal_reference"]),
        activity_type=str(data["activity_type"]).upper(),
        country_of_activity=data.get("country_of_activity"),
        border_cross_country=data.get("border_cross_country"),
        operator_role=data.get("operator_role", "OPERATOR"),
        geo_location_confidential=bool(data.get("geo_location_confidential", False)),
        grouped_declarations=list(data.get("grouped_declarations", [])),
        risk=Risk(
            level=str(risk["level"]).lower(),
            score=risk.get("score"),
            assessment_id=risk.get("assessment_id"),
            mitigated=bool(risk.get("mitigated", False)),
        )
        if risk
        else None,
        commodities=[_commodity(c, base_dir) for c in data["commodities"]],
    )


def _commodity(c: dict[str, Any], base_dir: Path) -> Commodity:
    return Commodity(
        description=c["description"],
        hs_heading=str(c["hs_heading"]),
        net_weight=_decimal(c.get("net_weight_kg")),
        supplementary_unit=_decimal(c.get("supplementary_unit")),
        supplementary_unit_qualifier=c.get("supplementary_unit_qualifier"),
        species=[Species(s["scientific_name"], s.get("common_name")) for s in c.get("species", [])],
        producers=[_producer(p, base_dir) for p in c.get("producers", [])],
    )


def _producer(p: dict[str, Any], base_dir: Path) -> Producer:
    geojson, source = p.get("geojson"), "inline" if "geojson" in p else None
    if p.get("geojson_file"):
        file = (base_dir / p["geojson_file"]).resolve()
        geojson, source = json.loads(file.read_text(encoding="utf-8")), str(file)
    return Producer(country=p["country"], name=p.get("name"), geojson=geojson, geojson_source=source)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


CSV_COLUMNS = [
    "internal_reference", "activity_type", "country_of_activity", "border_cross_country",
    "description", "hs_heading", "net_weight_kg", "supplementary_unit",
    "supplementary_unit_qualifier", "scientific_name", "common_name",
    "producer_country", "producer_name", "geojson_file", "risk_level", "risk_score",
]


def load_csv(path: Path) -> Iterator[LoadedRecord]:
    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), 2):
            ref = (row.get("internal_reference") or "").strip() or f"line {lineno}"
            groups.setdefault(ref, []).append((lineno, {k: (v or "").strip() for k, v in row.items() if k}))
    for ref, rows in groups.items():
        try:
            yield LoadedRecord(ref, _statement_from_rows(ref, [r for _, r in rows], path.parent))
        except (ValueError, KeyError, OSError, InvalidOperation) as e:
            lines = ", ".join(str(n) for n, _ in rows)
            yield LoadedRecord(ref, None, [f"rows {lines}: {type(e).__name__}: {e}"])


def _statement_from_rows(ref: str, rows: list[dict[str, str]], base_dir: Path) -> Statement:
    first = rows[0]
    commodities: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        ckey = (row["hs_heading"], row["description"])
        c = commodities.setdefault(
            ckey,
            {
                "description": row["description"],
                "hs_heading": row["hs_heading"],
                "net_weight_kg": row.get("net_weight_kg"),
                "supplementary_unit": row.get("supplementary_unit"),
                "supplementary_unit_qualifier": row.get("supplementary_unit_qualifier") or None,
                "species": [],
                "producers": [],
            },
        )
        if row.get("scientific_name"):
            sp = {"scientific_name": row["scientific_name"], "common_name": row.get("common_name") or None}
            if sp not in c["species"]:
                c["species"].append(sp)
        if row.get("producer_country"):
            c["producers"].append(
                {
                    "country": row["producer_country"],
                    "name": row.get("producer_name") or None,
                    "geojson_file": row.get("geojson_file") or None,
                }
            )
    data: dict[str, Any] = {
        "internal_reference": ref,
        "activity_type": first["activity_type"],
        "country_of_activity": first.get("country_of_activity") or None,
        "border_cross_country": first.get("border_cross_country") or None,
        "commodities": list(commodities.values()),
    }
    if first.get("risk_level"):
        score = first.get("risk_score")
        data["risk"] = {"level": first["risk_level"], "score": float(score) if score else None}
    return statement_from_dict(data, base_dir)
