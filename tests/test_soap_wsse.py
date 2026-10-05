import base64
import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from deforro_traces import soap
from deforro_traces.config import Credentials
from deforro_traces.wsse import make_token

from .conftest import PLOT, RECORD, loaded


def test_password_digest_matches_ws_security_formula():
    creds = Credentials("user", "secret-key", "client")
    now = datetime(2026, 5, 20, 9, 55, 1, 123000, tzinfo=timezone.utc)
    token = make_token(creds, ttl_seconds=60, now=now, nonce=b"0123456789abcdef")

    expected = base64.b64encode(
        hashlib.sha1(b"0123456789abcdef" + b"2026-05-20T09:55:01.123Z" + b"secret-key").digest()
    ).decode()
    assert token.created == "2026-05-20T09:55:01.123Z"
    assert token.expires == "2026-05-20T09:56:01.123Z"
    assert token.password_digest == expected
    assert base64.b64decode(token.nonce_b64) == b"0123456789abcdef"


def test_fresh_nonce_per_token():
    creds = Credentials("u", "k", "c")
    assert make_token(creds).nonce_b64 != make_token(creds).nonce_b64


def test_submit_envelope_follows_official_layout():
    stmt = loaded(RECORD).statement
    token = make_token(Credentials("u", "k", "my-client"))
    root = ET.fromstring(soap.envelope(token, "my-client", soap.submit_request(stmt)))

    ns = soap.NS
    header = root.find(f"{{{ns['soapenv']}}}Header")
    assert header.find(f"{{{ns['v4']}}}WebServiceClientId").text == "my-client"
    pw = header.find(f".//{{{ns['wsse']}}}Password")
    assert pw.get("Type").endswith("#PasswordDigest")

    req = root.find(f".//{{{ns['dds']}}}SubmitDdsRequest")
    assert [soap.local(c.tag) for c in req] == ["operatorRole", "statement"]
    st = req.find(f"{{{ns['dds']}}}statement")
    assert [soap.local(c.tag) for c in st] == [
        "internalReferenceNumber", "activityType", "countryOfActivity",
        "borderCrossCountry", "commodities", "geoLocationConfidential",
    ]
    com = st.find(f"{{{ns['dds']}}}commodities")
    assert [soap.local(c.tag) for c in com] == ["position", "descriptors", "hsHeading", "speciesInfo", "producers"]
    desc = com.find(f"{{{ns['dds']}}}descriptors")
    assert desc.find(f"{{{ns['eudrCommon']}}}descriptionOfGoods").text == "Cocoa beans, whole, raw"
    assert desc.find(f".//{{{ns['eudrCommon']}}}netWeight").text == "5000"
    geo = com.find(f".//{{{ns['dds']}}}geometryGeojson").text
    assert json.loads(base64.b64decode(geo)) == PLOT


def test_statement_roundtrip():
    stmt = loaded(dict(RECORD, grouped_declarations=["26FRYUI34JTQKB"])).statement
    req = soap.submit_request(stmt)
    parsed = soap.parse_statement(soap.child(req, "statement"))
    stmt.risk = None
    assert parsed.payload_hash() == stmt.payload_hash()


def test_fault_parsing():
    xml = b"""<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body><S:Fault>
      <faultcode>S:Client</faultcode><faultstring>Business rules validation failed</faultstring>
      <detail><x:Err xmlns:x="urn:x"><x:code>EUDR-HS</x:code></x:Err></detail>
    </S:Fault></S:Body></S:Envelope>"""
    with pytest.raises(soap.SoapFault) as exc:
        soap.parse_body(xml, 500)
    assert exc.value.code == "S:Client"
    assert exc.value.details == ["code=EUDR-HS"]


def test_parse_official_get_dds_sample():
    xml = b"""<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body>
      <ns5:GetDdsResponse xmlns:ns3="http://ec.europa.eu/tracesnt/certificate/eudr/common/v3"
                          xmlns:ns5="http://ec.europa.eu/tracesnt/certificate/eudr/due-diligence-statement/v3">
        <ns5:ddsOverviewList>
          <ns3:uuid>071874bd-8c62-4cac-8eb6-b2fbe003410c</ns3:uuid>
          <ns3:internalReferenceNumber>26BEDWNW9JD1TN</ns3:internalReferenceNumber>
          <ns3:referenceNumber>26BE7XTVCZAQ2S</ns3:referenceNumber>
          <ns3:verificationNumber>SFFCB4Y3</ns3:verificationNumber>
          <ns3:status>AVAILABLE</ns3:status>
          <ns3:date>2026-05-20T09:55:01.000Z</ns3:date>
          <ns3:updatedBy>User3 User3</ns3:updatedBy>
          <ns3:version>1</ns3:version>
        </ns5:ddsOverviewList>
      </ns5:GetDdsResponse></S:Body></S:Envelope>"""
    body = soap.parse_body(xml)
    [ov] = [soap.parse_overview(el) for el in soap.children(body, "ddsOverviewList")]
    assert ov.reference_number == "26BE7XTVCZAQ2S"
    assert ov.verification_number == "SFFCB4Y3"
    assert ov.status == "AVAILABLE"


def test_redact_masks_secrets():
    stmt = loaded(RECORD).statement
    token = make_token(Credentials("u", "very-secret", "c"))
    out = soap.redact(soap.envelope(token, "c", soap.submit_request(stmt)))
    assert token.password_digest not in out
    assert token.nonce_b64 not in out
