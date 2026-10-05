"""SOAP envelopes for EUDRDueDiligenceStatementServiceV3 (build + parse)."""

from __future__ import annotations

import base64
import json
import xml.etree.ElementTree as ET
from decimal import Decimal

from .models import Commodity, DdsOverview, Producer, Species, Statement, format_decimal
from .wsse import WsseToken

NS = {
    "soapenv": "http://schemas.xmlsoap.org/soap/envelope/",
    "v4": "http://ec.europa.eu/sanco/tracesnt/base/v4",
    "dds": "http://ec.europa.eu/tracesnt/certificate/eudr/due-diligence-statement/v3",
    "eudrCommon": "http://ec.europa.eu/tracesnt/certificate/eudr/common/v3",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
}
PASSWORD_DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
NONCE_ENCODING = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)

for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)


class SoapFault(Exception):
    """A SOAP Fault returned by the service (validation, auth, business rules)."""

    def __init__(self, code: str, message: str, details: list[str], http_status: int | None = None):
        self.code = code
        self.message = message
        self.details = details
        self.http_status = http_status
        super().__init__(self.__str__())

    def __str__(self) -> str:
        extra = f" ({'; '.join(self.details)})" if self.details else ""
        return f"{self.code}: {self.message}{extra}"


def q(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


def sub(parent: ET.Element, prefix: str, tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    el = ET.SubElement(parent, q(prefix, tag), attrib)
    if text is not None:
        el.text = text
    return el


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# --------------------------------------------------------------------------- build


def envelope(token: WsseToken | None, client_id: str, body_content: ET.Element) -> bytes:
    env = ET.Element(q("soapenv", "Envelope"))
    header = sub(env, "soapenv", "Header")
    if token is not None:
        security = sub(header, "wsse", "Security")
        ts = sub(security, "wsu", "Timestamp", **{q("wsu", "Id"): "TS-1"})
        sub(ts, "wsu", "Created", token.created)
        sub(ts, "wsu", "Expires", token.expires)
        ut = sub(security, "wsse", "UsernameToken", **{q("wsu", "Id"): "UT-1"})
        sub(ut, "wsse", "Username", token.username)
        sub(ut, "wsse", "Password", token.password_digest, Type=PASSWORD_DIGEST_TYPE)
        sub(ut, "wsse", "Nonce", token.nonce_b64, EncodingType=NONCE_ENCODING)
        sub(ut, "wsu", "Created", token.created)
    sub(header, "v4", "WebServiceClientId", client_id)
    body = sub(env, "soapenv", "Body")
    body.append(body_content)
    return ET.tostring(env, encoding="utf-8", xml_declaration=True)


def encode_geojson(doc: dict) -> str:
    raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def statement_element(stmt: Statement, parent: ET.Element) -> ET.Element:
    st = sub(parent, "dds", "statement")
    sub(st, "dds", "internalReferenceNumber", stmt.internal_reference)
    sub(st, "dds", "activityType", stmt.activity_type)
    if stmt.country_of_activity:
        sub(st, "dds", "countryOfActivity", stmt.country_of_activity)
    if stmt.border_cross_country:
        sub(st, "dds", "borderCrossCountry", stmt.border_cross_country)
    for ci, c in enumerate(stmt.commodities, 1):
        com = sub(st, "dds", "commodities")
        sub(com, "dds", "position", str(ci))
        desc = sub(com, "dds", "descriptors")
        sub(desc, "eudrCommon", "descriptionOfGoods", c.description)
        gm = sub(desc, "eudrCommon", "goodsMeasure")
        if c.net_weight is not None:
            sub(gm, "eudrCommon", "netWeight", format_decimal(c.net_weight))
        if c.supplementary_unit is not None:
            sub(gm, "eudrCommon", "supplementaryUnit", format_decimal(c.supplementary_unit))
        if c.supplementary_unit_qualifier:
            sub(gm, "eudrCommon", "supplementaryUnitQualifier", c.supplementary_unit_qualifier)
        sub(com, "dds", "hsHeading", c.hs_heading)
        for sp in c.species:
            si = sub(com, "dds", "speciesInfo")
            sub(si, "dds", "scientificName", sp.scientific_name)
            if sp.common_name:
                sub(si, "dds", "commonName", sp.common_name)
        for pi, p in enumerate(c.producers, 1):
            pr = sub(com, "dds", "producers")
            sub(pr, "dds", "position", str(pi))
            sub(pr, "dds", "country", p.country)
            if p.name:
                sub(pr, "dds", "name", p.name)
            if p.geojson is not None:
                sub(pr, "dds", "geometryGeojson", encode_geojson(p.geojson))
    sub(st, "dds", "geoLocationConfidential", "true" if stmt.geo_location_confidential else "false")
    for ref in stmt.grouped_declarations:
        gd = sub(st, "dds", "groupedDeclarations")
        sub(gd, "eudrCommon", "groupedDeclaration", ref)
    return st


def submit_request(stmt: Statement) -> ET.Element:
    req = ET.Element(q("dds", "SubmitDdsRequest"))
    sub(req, "dds", "operatorRole", stmt.operator_role)
    statement_element(stmt, req)
    return req


def amend_request(uuid: str, stmt: Statement) -> ET.Element:
    req = ET.Element(q("dds", "AmendDdsRequest"))
    sub(req, "dds", "uuid", uuid)
    statement_element(stmt, req)
    return req


def withdraw_request(uuid: str) -> ET.Element:
    req = ET.Element(q("dds", "WithdrawDdsRequest"))
    sub(req, "dds", "uuid", uuid)
    return req


def get_dds_request(uuids: list[str]) -> ET.Element:
    req = ET.Element(q("dds", "GetDdsRequest"))
    for uuid in uuids:
        sub(req, "dds", "uuidList", uuid)
    return req


def get_by_internal_reference_request(ref: str) -> ET.Element:
    req = ET.Element(q("dds", "GetDdsByInternalReferenceRequest"))
    sub(req, "dds", "internalReference", ref)
    return req


def get_by_identifiers_request(reference_number: str, verification_number: str) -> ET.Element:
    req = ET.Element(q("dds", "GetDdsByIdentifiersRequest"))
    rv = sub(req, "dds", "referenceAndVerificationNumber")
    sub(rv, "eudrCommon", "referenceNumber", reference_number)
    sub(rv, "eudrCommon", "verificationNumber", verification_number)
    return req


def redact(xml_bytes: bytes) -> str:
    """Envelope as text with WS-Security secrets masked, for logs and dry runs."""
    root = ET.fromstring(xml_bytes)
    for el in root.iter():
        if local(el.tag) in ("Password", "Nonce"):
            el.text = "***"
    ET.indent(root)
    return ET.tostring(root, encoding="unicode")


# --------------------------------------------------------------------------- parse


def child(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def children(el: ET.Element | None, name: str) -> list[ET.Element]:
    if el is None:
        return []
    return [c for c in el if local(c.tag) == name]


def text(el: ET.Element | None, name: str) -> str | None:
    c = child(el, name)
    return c.text.strip() if c is not None and c.text is not None else None


def parse_body(data: bytes, http_status: int | None = None) -> ET.Element:
    """Return the first element inside soap:Body, raising SoapFault for faults."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SoapFault("Client.Parse", f"response is not XML: {e}", [], http_status) from None
    body = child(root, "Body")
    if body is None or len(body) == 0:
        raise SoapFault("Client.Parse", "response has no SOAP body", [], http_status)
    first = body[0]
    if local(first.tag) == "Fault":
        code = text(first, "faultcode") or "Server"
        message = text(first, "faultstring") or ""
        details: list[str] = []
        detail = child(first, "detail")
        if detail is not None:
            for el in detail.iter():
                if len(el) == 0 and el.text and el.text.strip():
                    details.append(f"{local(el.tag)}={el.text.strip()}")
        raise SoapFault(code, message, details, http_status)
    return first


def parse_overview(el: ET.Element) -> DdsOverview:
    return DdsOverview(
        uuid=text(el, "uuid") or "",
        internal_reference=text(el, "internalReferenceNumber"),
        reference_number=text(el, "referenceNumber"),
        verification_number=text(el, "verificationNumber"),
        status=text(el, "status"),
        date=text(el, "date"),
        updated_by=text(el, "updatedBy"),
        version=text(el, "version"),
    )


def parse_statement(st: ET.Element, operator_role: str = "OPERATOR") -> Statement:
    commodities = []
    for com in children(st, "commodities"):
        desc = child(com, "descriptors")
        gm = child(desc, "goodsMeasure")
        species = [
            Species(text(si, "scientificName") or "", text(si, "commonName"))
            for si in children(com, "speciesInfo")
        ]
        producers = []
        for pr in children(com, "producers"):
            raw = text(pr, "geometryGeojson")
            geo = None
            if raw:
                try:
                    geo = json.loads(base64.b64decode(raw, validate=True))
                except ValueError:
                    geo = {"__invalid__": raw[:40]}
            producers.append(Producer(text(pr, "country") or "", text(pr, "name"), geo))
        nw, su = text(gm, "netWeight"), text(gm, "supplementaryUnit")
        commodities.append(
            Commodity(
                description=text(desc, "descriptionOfGoods") or "",
                hs_heading=text(com, "hsHeading") or "",
                net_weight=Decimal(nw) if nw else None,
                supplementary_unit=Decimal(su) if su else None,
                supplementary_unit_qualifier=text(gm, "supplementaryUnitQualifier"),
                species=species,
                producers=producers,
            )
        )
    grouped = [
        g
        for gd in children(st, "groupedDeclarations")
        for g in (text(gd, "groupedDeclaration"),)
        if g
    ]
    return Statement(
        internal_reference=text(st, "internalReferenceNumber") or "",
        activity_type=text(st, "activityType") or "",
        country_of_activity=text(st, "countryOfActivity"),
        border_cross_country=text(st, "borderCrossCountry"),
        commodities=commodities,
        operator_role=operator_role,
        geo_location_confidential=(text(st, "geoLocationConfidential") or "false") == "true",
        grouped_declarations=grouped,
    )
