import copy
import json

from deforro_traces.loader import load
from deforro_traces.samples import generate
from deforro_traces.validation import geojson_area_hectares, validate_statement

from .conftest import PLOT, loaded, record


def codes(data):
    return {i.code for i in validate_statement(loaded(data).statement).errors}


def test_valid_record_passes():
    assert codes(record()) == set()


def test_open_ring_rejected():
    data = record()
    data["commodities"][0]["producers"][0]["geojson"]["features"][0]["geometry"]["coordinates"][0].pop()
    assert "GEO_RING" in codes(data)


def test_swapped_lat_lon_out_of_range():
    data = record()
    geom = data["commodities"][0]["producers"][0]["geojson"]["features"][0]["geometry"]
    geom["coordinates"] = [[[lat, lon + 200] for lon, lat in geom["coordinates"][0]]]
    assert "GEO_RANGE" in codes(data)


def test_point_for_large_plot_rejected():
    data = record()
    data["commodities"][0]["producers"][0]["geojson"] = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"Area": 12},
                      "geometry": {"type": "Point", "coordinates": [-1.623451, 6.712345]}}],
    }
    assert "GEO_POINT_AREA" in codes(data)


def test_import_needs_border_country_and_measure():
    data = record(border_cross_country=None)
    data["commodities"][0]["net_weight_kg"] = None
    assert {"BORDER", "MEASURE"} <= codes(data)


def test_bad_hs_and_missing_geometry():
    data = record()
    data["commodities"][0]["hs_heading"] = "18A1"
    data["commodities"][0]["producers"][0].pop("geojson")
    assert {"HS", "GEO_MISSING"} <= codes(data)


def test_area_is_roughly_right():
    # ~370 m x ~293 m at 6.7N
    assert 10 < geojson_area_hectares(PLOT) < 12


def test_generated_samples_are_mostly_valid():
    recs = generate(200, invalid_rate=0)
    bad = [r["internal_reference"] for r in recs if codes(r)]
    assert bad == []


def test_csv_groups_rows_into_statements(tmp_path):
    (tmp_path / "plots").mkdir()
    (tmp_path / "plots" / "a.geojson").write_text(json.dumps(PLOT))
    (tmp_path / "in.csv").write_text(
        "internal_reference,activity_type,country_of_activity,border_cross_country,description,hs_heading,"
        "net_weight_kg,scientific_name,common_name,producer_country,producer_name,geojson_file,risk_level\n"
        "R1,IMPORT,BE,BE,Cocoa beans,1801,5000,Theobroma cacao,Cacao,GH,Farm A,plots/a.geojson,negligible\n"
        "R1,IMPORT,BE,BE,Cocoa beans,1801,5000,Theobroma cacao,Cacao,GH,Farm B,plots/a.geojson,negligible\n"
        "R2,IMPORT,NL,NL,Coffee,0901,1200,Coffea arabica,,ET,Farm C,plots/missing.geojson,negligible\n"
    )
    recs = {r.key: r for r in load(tmp_path / "in.csv")}
    stmt = recs["R1"].statement
    assert len(stmt.commodities) == 1
    assert [p.name for p in stmt.commodities[0].producers] == ["Farm A", "Farm B"]
    assert stmt.commodities[0].producers[0].geojson == PLOT
    assert stmt.risk.level == "negligible"
    assert recs["R2"].statement is None and "missing.geojson" in recs["R2"].errors[0]


def test_jsonl_bad_line_does_not_stop_loading(tmp_path):
    good = record()
    (tmp_path / "in.jsonl").write_text("{not json\n" + json.dumps(good) + "\n")
    recs = list(load(tmp_path / "in.jsonl"))
    assert recs[0].statement is None and recs[0].key == "line 1"
    assert recs[1].statement.internal_reference == "DEF-TEST-0001"


def test_payload_hash_ignores_risk_but_tracks_content():
    a = loaded(record()).statement
    b_data = record(risk={"level": "negligible", "score": 0.5})
    b = loaded(b_data).statement
    assert a.payload_hash() == b.payload_hash()
    c_data = copy.deepcopy(record())
    c_data["commodities"][0]["net_weight_kg"] = 5001
    assert loaded(c_data).statement.payload_hash() != a.payload_hash()
