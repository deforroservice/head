import copy

import pytest

from deforro_traces.client import RetryPolicy, TracesClient
from deforro_traces.config import Credentials, MOCK_AUTH_KEY, MOCK_CLIENT_ID, MOCK_USERNAME
from deforro_traces.ledger import Ledger
from deforro_traces.loader import LoadedRecord, statement_from_dict
from deforro_traces.mock_server import MockConfig, MockTraces, serve_in_background

PLOT = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"ProducerCountry": "GH", "Area": 6.5},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [-1.623451, 6.712345],
                    [-1.620112, 6.712345],
                    [-1.620112, 6.714987],
                    [-1.623451, 6.714987],
                    [-1.623451, 6.712345],
                ]],
            },
        }
    ],
}

RECORD = {
    "internal_reference": "DEF-TEST-0001",
    "activity_type": "IMPORT",
    "country_of_activity": "BE",
    "border_cross_country": "BE",
    "risk": {"level": "negligible", "score": 0.02, "assessment_id": "RA-1"},
    "commodities": [
        {
            "description": "Cocoa beans, whole, raw",
            "hs_heading": "1801",
            "net_weight_kg": 5000,
            "species": [{"scientific_name": "Theobroma cacao", "common_name": "Cacao"}],
            "producers": [{"country": "GH", "name": "Kofi Cooperative", "geojson": PLOT}],
        }
    ],
}


def record(**overrides):
    data = copy.deepcopy(RECORD)
    data.update(overrides)
    return data


def loaded(data):
    return LoadedRecord(data["internal_reference"], statement_from_dict(data, base_dir=None))


@pytest.fixture
def mock():
    m = MockTraces(MockConfig(processing_delay=0, seed=1))
    server, url = serve_in_background(m)
    m.url = url
    yield m
    server.shutdown()
    server.server_close()


@pytest.fixture
def client(mock):
    return TracesClient(
        mock.url,
        Credentials(MOCK_USERNAME, MOCK_AUTH_KEY, MOCK_CLIENT_ID),
        timeout=10,
        retry=RetryPolicy(attempts=5, base_delay=0.01, max_delay=0.05),
    )


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.db", "mock")
    yield led
    led.close()
