"""The browser sandbox (web/engine.js) must reach the same conclusions as this package."""

import base64
import copy
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from deforro_traces import soap
from deforro_traces.loader import load
from deforro_traces.pipeline import BulkUploader
from deforro_traces.samples import generate, write_jsonl
from deforro_traces.validation import validate_statement
from deforro_traces.wsse import password_digest

from .conftest import record

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run_js(input_path, *extra):
    out = subprocess.run(
        ["node", str(ROOT / "web" / "parity_dump.js"), str(input_path), *extra],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def python_view(input_path):
    view = {}
    gate = BulkUploader(None, None)._risk_gate
    for rec in load(input_path):
        if rec.statement is None:
            view[rec.key] = {"loaded": False}
            continue
        r = validate_statement(rec.statement)
        view[rec.key] = {
            "loaded": True,
            "errors": sorted({i.code for i in r.errors}),
            "warnings": sorted({i.code for i in r.warnings}),
            "gate": gate(rec.statement) is not None,
        }
    return view


def edge_cases():
    cases = []

    def add(ref, mutate):
        data = record(internal_reference=ref)
        mutate(data)
        cases.append(data)

    def geom(d):
        return d["commodities"][0]["producers"][0]["geojson"]["features"][0]["geometry"]

    add("E-OK", lambda d: None)
    add("E-NOBORDER", lambda d: d.pop("border_cross_country"))
    add("E-BADCOUNTRY", lambda d: d.update(country_of_activity="Belgium"))
    add("E-DOMESTIC", lambda d: d.update(activity_type="domestic", border_cross_country=None))
    add("E-ACTIVITY", lambda d: d.update(activity_type="SHIP"))
    add("E-ROLE", lambda d: d.update(operator_role="TRADER"))
    add("E-GROUPED", lambda d: d.update(grouped_declarations=["bad ref"]))
    add("E-HS", lambda d: d["commodities"][0].update(hs_heading="18A1"))
    add("E-HSANNEX", lambda d: d["commodities"][0].update(hs_heading="8471"))
    add("E-ZERO", lambda d: d["commodities"][0].update(net_weight_kg=0))
    add("E-NOMEASURE", lambda d: d["commodities"][0].pop("net_weight_kg"))
    add("E-SUPPONLY", lambda d: d["commodities"][0].update(supplementary_unit=20))
    add("E-NOPRODUCER", lambda d: d["commodities"][0].update(producers=[]))
    add("E-NOGEO", lambda d: d["commodities"][0]["producers"][0].pop("geojson"))
    add("E-NOTFC", lambda d: d["commodities"][0]["producers"][0].update(geojson={"type": "Feature"}))
    add("E-EMPTY", lambda d: d["commodities"][0]["producers"][0].update(geojson={"type": "FeatureCollection", "features": []}))
    add("E-LINE", lambda d: geom(d).update(type="LineString"))
    add("E-OPEN", lambda d: geom(d)["coordinates"][0].pop())
    add("E-SHORT", lambda d: geom(d).update(coordinates=[geom(d)["coordinates"][0][:3]]))
    add("E-RANGE", lambda d: geom(d).update(coordinates=[[[lat, lon + 200] for lon, lat in geom(d)["coordinates"][0]]]))
    add("E-MALFORMED", lambda d: geom(d).update(coordinates=[["x", "y"]]))
    add("E-POINTBIG", lambda d: d["commodities"][0]["producers"][0]["geojson"]["features"][0].update(
        geometry={"type": "Point", "coordinates": [-1.623451, 6.712345]}, properties={"Area": 9}))
    add("E-PRECISION", lambda d: geom(d).update(coordinates=[[[-1.62, 6.71], [-1.6, 6.71], [-1.6, 6.73], [-1.62, 6.71]]]))
    add("E-MULTI", lambda d: geom(d).update(type="MultiPolygon", coordinates=[geom(d)["coordinates"]]))
    add("E-MULTIPOINT", lambda d: geom(d).update(type="MultiPoint", coordinates=[[-1.623451, 6.712345], [-1.6, 95.0]]))
    add("E-HIGHRISK", lambda d: d.update(risk={"level": "HIGH", "score": 0.9}))
    add("E-MITIGATED", lambda d: d.update(risk={"level": "standard", "mitigated": True}))
    add("E-NORISK", lambda d: d.pop("risk"))
    add("E-COORDS", lambda d: geom(d).update(type="Point", coordinates="abc"))
    add("E-ZEROAREA", lambda d: geom(d).update(coordinates=[[[-1.623451, 6.712345]] * 4]))
    return cases


def write_cases(path, cases, extra_lines=()):
    with path.open("w") as fh:
        for c in cases:
            fh.write(json.dumps(c) + "\n")
        for line in extra_lines:
            fh.write(line + "\n")


def compare(py, js):
    assert set(py) == set(js["records"]), "different records loaded"
    mismatches = {}
    for key, pv in py.items():
        jv = dict(js["records"][key])
        if "gate" in jv:
            jv["gate"] = jv["gate"] is not None
        if pv != jv:
            mismatches[key] = {"python": pv, "js": jv}
    assert not mismatches, json.dumps(mismatches, indent=2)


def test_validation_parity_on_edge_cases(tmp_path):
    path = tmp_path / "edge.jsonl"
    write_cases(path, edge_cases(), ['{"broken json', '{"internal_reference": "E-MISSING"}'])
    compare(python_view(path), run_js(path))


def test_validation_parity_on_generated_samples(tmp_path):
    path = tmp_path / "gen.jsonl"
    write_jsonl(generate(400, seed=5, invalid_rate=0.3, risky_rate=0.1), path)
    compare(python_view(path), run_js(path))


def test_csv_parity_with_example():
    path = ROOT / "examples" / "shipments.example.csv"
    compare(python_view(path), run_js(path))


def test_digest_parity():
    nonce = bytes(range(16))
    created = "2026-05-20T09:55:01.123Z"
    js = run_js(ROOT / "examples" / "shipments.example.jsonl", nonce.hex(), created, "sandbox-auth-key")
    assert js["digest"] == password_digest(nonce, created, "sandbox-auth-key")


def _flatten(el):
    out = []
    for node in el.iter():
        name = soap.local(node.tag)
        text = (node.text or "").strip()
        if name == "geometryGeojson":
            text = json.loads(base64.b64decode(text))
        out.append((name, text))
    return out


def test_soap_body_parity():
    path = ROOT / "examples" / "shipments.example.jsonl"
    js = run_js(path)
    for rec in load(path):
        decl = " ".join(f'xmlns:{p}="{u}"' for p, u in soap.NS.items())
        js_el = ET.fromstring(f"<root {decl}>{js['bodies'][rec.key]}</root>")[0]
        assert _flatten(js_el) == _flatten(soap.submit_request(rec.statement)), rec.key
