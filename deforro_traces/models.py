"""Domain model for a Due Diligence Statement as DEFORRO produces it."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class Species:
    scientific_name: str
    common_name: str | None = None


@dataclass
class Producer:
    country: str
    name: str | None = None
    geojson: dict[str, Any] | None = None
    # Where the geometry came from (file path, "inline"), for error messages only.
    geojson_source: str | None = None


@dataclass
class Commodity:
    description: str
    hs_heading: str
    net_weight: Decimal | None = None
    supplementary_unit: Decimal | None = None
    supplementary_unit_qualifier: str | None = None
    species: list[Species] = field(default_factory=list)
    producers: list[Producer] = field(default_factory=list)


@dataclass
class Risk:
    """Output of DEFORRO's risk analysis, used to gate submission."""

    level: str  # negligible | low | standard | high
    score: float | None = None
    assessment_id: str | None = None
    mitigated: bool = False


@dataclass
class Statement:
    internal_reference: str
    activity_type: str
    commodities: list[Commodity]
    country_of_activity: str | None = None
    border_cross_country: str | None = None
    operator_role: str = "OPERATOR"
    geo_location_confidential: bool = False
    grouped_declarations: list[str] = field(default_factory=list)
    risk: Risk | None = None

    def payload_hash(self) -> str:
        """Stable hash of everything that ends up in the TRACES payload.

        Used by the ledger to detect a re-uploaded statement whose content changed.
        """
        data = asdict(self)
        data.pop("risk", None)
        for commodity in data["commodities"]:
            for producer in commodity["producers"]:
                producer.pop("geojson_source", None)
        blob = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class DdsOverview:
    """One entry of a getDds / getDdsByInternalReference response."""

    uuid: str
    internal_reference: str | None = None
    reference_number: str | None = None
    verification_number: str | None = None
    status: str | None = None
    date: str | None = None
    updated_by: str | None = None
    version: str | None = None


def format_decimal(value: Decimal | float | int | str) -> str:
    """Render a number without exponent or trailing zeros (5000, 12.5)."""
    d = Decimal(str(value)).normalize()
    text = f"{d:f}"
    return text
