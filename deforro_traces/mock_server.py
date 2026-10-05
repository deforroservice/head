"""Local stand-in for EUDRDueDiligenceStatementServiceV3.

It speaks the same SOAP as TRACES so the whole pipeline can be exercised (and load
tested) without EU Login credentials. It checks the WS-Security digest, timestamp,
nonce replay and WebServiceClientId; runs the local validation rules; issues UUIDs;
moves statements from SUBMITTED to AVAILABLE (or REJECTED) after a delay; and can
inject faults and latency. It is a simulator: the acceptance environment remains the
place to prove conformance with the real service.
"""

from __future__ import annotations

import base64
import logging
import random
import string
import threading
import time
import uuid as uuidlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import soap
from .config import MOCK_AUTH_KEY, MOCK_CLIENT_ID, MOCK_USERNAME, SERVICE_PATH
from .models import Statement
from .soap import NS, child, children, local, q, sub, text
from .validation import validate_statement
from .wsse import format_timestamp, parse_timestamp, password_digest

log = logging.getLogger(__name__)


@dataclass
class MockConfig:
    accounts: dict[str, str] = field(default_factory=lambda: {MOCK_USERNAME: MOCK_AUTH_KEY})
    client_id: str = MOCK_CLIENT_ID
    processing_delay: float = 2.0     # seconds before SUBMITTED becomes final
    reject_rate: float = 0.0          # share of valid statements later REJECTED
    fault_rate: float = 0.0           # share of requests answered with HTTP 503
    latency: float = 0.0              # extra seconds per request
    clock_skew: float = 300.0         # tolerated WSSE timestamp skew
    seed: int | None = None


@dataclass
class StoredDds:
    uuid: str
    statement: Statement
    country: str
    submitted_at: float
    reference_number: str
    verification_number: str
    final_status: str
    status: str = "SUBMITTED"
    version: int = 1
    updated_by: str = ""
    grouped_by: str | None = None


class MockTraces:
    def __init__(self, config: MockConfig | None = None):
        self.config = config or MockConfig()
        self.rng = random.Random(self.config.seed)
        self.lock = threading.Lock()
        self.store: dict[str, StoredDds] = {}
        self.nonces: dict[str, float] = {}
        self.request_count = 0

    # ------------------------------------------------------------------ helpers

    def _code(self, n: int) -> str:
        return "".join(self.rng.choice(string.ascii_uppercase + string.digits) for _ in range(n))

    def _refresh(self, dds: StoredDds) -> None:
        if dds.status == "SUBMITTED" and time.time() - dds.submitted_at >= self.config.processing_delay:
            dds.status = dds.final_status

    def _by_reference(self, reference: str) -> StoredDds | None:
        return next((d for d in self.store.values() if d.reference_number == reference), None)

    # ------------------------------------------------------------------ dispatch

    def handle(self, data: bytes) -> tuple[int, bytes]:
        with self.lock:
            self.request_count += 1
            inject_fault = self.rng.random() < self.config.fault_rate
        if self.config.latency:
            time.sleep(self.config.latency)
        if inject_fault:
            return 503, b"Service Unavailable (injected by sandbox)"
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            return fault("soapenv:Client", f"Malformed XML: {e}")
        header, body = child(root, "Header"), child(root, "Body")
        if body is None or len(body) == 0:
            return fault("soapenv:Client", "Missing SOAP Body")
        username = self._authenticate(header)
        if isinstance(username, tuple):
            return username
        request = body[0]
        op = local(request.tag)
        handler = {
            "SubmitDdsRequest": self.submit,
            "AmendDdsRequest": self.amend,
            "WithdrawDdsRequest": self.withdraw,
            "GetDdsRequest": self.get,
            "GetDdsByInternalReferenceRequest": self.get_by_internal_reference,
            "GetDdsByIdentifiersRequest": self.get_by_identifiers,
        }.get(op)
        if handler is None:
            return fault("soapenv:Client", f"Unknown operation {op}")
        with self.lock:
            return handler(request, username)

    def _authenticate(self, header: ET.Element | None) -> str | tuple[int, bytes]:
        security = child(header, "Security")
        token = child(security, "UsernameToken")
        if token is None:
            return fault("wsse:InvalidSecurity", "Missing WS-Security UsernameToken")
        if text(header, "WebServiceClientId") != self.config.client_id:
            return fault("soapenv:Client", "Invalid or missing WebServiceClientId")
        username = text(token, "Username") or ""
        password = text(token, "Password") or ""
        nonce_b64 = text(token, "Nonce") or ""
        created = text(token, "Created") or ""
        key = self.config.accounts.get(username)
        try:
            nonce = base64.b64decode(nonce_b64, validate=True)
            created_at = parse_timestamp(created)
        except ValueError:
            return fault("wsse:InvalidSecurity", "Malformed Nonce or Created")
        if key is None or password != password_digest(nonce, created, key):
            return fault("wsse:FailedAuthentication", "The security token could not be authenticated")
        now = datetime.now(timezone.utc)
        if abs((now - created_at).total_seconds()) > self.config.clock_skew:
            return fault("wsse:MessageExpired", "The message has expired (check the clock)")
        expires = text(child(security, "Timestamp"), "Expires")
        if expires and parse_timestamp(expires) < now - timedelta(seconds=self.config.clock_skew):
            return fault("wsse:MessageExpired", "The message has expired")
        with self.lock:
            cutoff = time.time() - 2 * self.config.clock_skew
            for n, t in list(self.nonces.items()):
                if t < cutoff:
                    del self.nonces[n]
            if nonce_b64 in self.nonces:
                return fault("wsse:InvalidSecurity", "Nonce has already been used")
            self.nonces[nonce_b64] = time.time()
        return username

    # ------------------------------------------------------------------ operations

    def _validate(self, statement_el: ET.Element | None) -> Statement | tuple[int, bytes]:
        if statement_el is None:
            return fault("soapenv:Client", "Missing statement")
        stmt = soap.parse_statement(statement_el)
        for c in stmt.commodities:
            for p in c.producers:
                if isinstance(p.geojson, dict) and "__invalid__" in p.geojson:
                    return business_fault([("EUDR-GEO-BASE64", "geometryGeojson is not Base64 GeoJSON")])
        result = validate_statement(stmt)
        if not result.ok:
            return business_fault([(f"EUDR-{i.code}", f"{i.path} {i.message}".strip()) for i in result.errors])
        for ref in stmt.grouped_declarations:
            target = self._by_reference(ref)
            if target is not None:
                self._refresh(target)
            if target is None or target.status != "AVAILABLE":
                return business_fault([("EUDR-GROUPED", f"referenced statement {ref} is not AVAILABLE")])
        return stmt

    def submit(self, req: ET.Element, username: str) -> tuple[int, bytes]:
        stmt = self._validate(child(req, "statement"))
        if isinstance(stmt, tuple):
            return stmt
        stmt.operator_role = text(req, "operatorRole") or "OPERATOR"
        country = stmt.country_of_activity or "BE"
        dds = StoredDds(
            uuid=str(uuidlib.uuid4()),
            statement=stmt,
            country=country,
            submitted_at=time.time(),
            reference_number=datetime.now(timezone.utc).strftime("%y") + country + self._code(10),
            verification_number=self._code(8),
            final_status="REJECTED" if self.rng.random() < self.config.reject_rate else "AVAILABLE",
            updated_by=username,
        )
        self.store[dds.uuid] = dds
        for ref in stmt.grouped_declarations:
            target = self._by_reference(ref)
            if target:
                target.status, target.grouped_by = "GROUPED", dds.uuid
        resp = ET.Element(q("dds", "SubmitDdsResponse"))
        sub(resp, "dds", "uuid", dds.uuid)
        return ok(resp)

    def amend(self, req: ET.Element, username: str) -> tuple[int, bytes]:
        dds = self.store.get(text(req, "uuid") or "")
        if dds is None:
            return business_fault([("EUDR-NOT-FOUND", "No statement with this UUID")])
        self._refresh(dds)
        if dds.status not in ("SUBMITTED", "AVAILABLE"):
            return business_fault([("EUDR-STATUS", f"Statement in status {dds.status} cannot be amended")])
        stmt = self._validate(child(req, "statement"))
        if isinstance(stmt, tuple):
            return stmt
        stmt.operator_role = dds.statement.operator_role
        dds.statement, dds.version, dds.updated_by = stmt, dds.version + 1, username
        resp = ET.Element(q("dds", "AmendDdsResponse"))
        sub(resp, "dds", "uuid", dds.uuid)
        sub(resp, "dds", "status", dds.status)
        return ok(resp)

    def withdraw(self, req: ET.Element, username: str) -> tuple[int, bytes]:
        dds = self.store.get(text(req, "uuid") or "")
        if dds is None:
            return business_fault([("EUDR-NOT-FOUND", "No statement with this UUID")])
        self._refresh(dds)
        if dds.status in ("GROUPED", "WITHDRAWN", "REJECTED"):
            return business_fault([("EUDR-STATUS", f"Statement in status {dds.status} cannot be withdrawn")])
        dds.status, dds.updated_by = "WITHDRAWN", username
        resp = ET.Element(q("dds", "WithdrawDdsResponse"))
        sub(resp, "dds", "uuid", dds.uuid)
        sub(resp, "dds", "status", dds.status)
        return ok(resp)

    def _overview_list(self, tag: str, items: list[StoredDds]) -> tuple[int, bytes]:
        resp = ET.Element(q("dds", tag))
        for dds in items:
            self._refresh(dds)
            ov = sub(resp, "dds", "ddsOverviewList")
            sub(ov, "eudrCommon", "uuid", dds.uuid)
            sub(ov, "eudrCommon", "internalReferenceNumber", dds.statement.internal_reference)
            if dds.status not in ("SUBMITTED", "REJECTED"):
                sub(ov, "eudrCommon", "referenceNumber", dds.reference_number)
                sub(ov, "eudrCommon", "verificationNumber", dds.verification_number)
            sub(ov, "eudrCommon", "status", dds.status)
            sub(ov, "eudrCommon", "date", format_timestamp(datetime.fromtimestamp(dds.submitted_at, timezone.utc)))
            sub(ov, "eudrCommon", "updatedBy", dds.updated_by)
            sub(ov, "eudrCommon", "version", str(dds.version))
        return ok(resp)

    def get(self, req: ET.Element, username: str) -> tuple[int, bytes]:
        uuids = [el.text.strip() for el in children(req, "uuidList") if el.text]
        return self._overview_list("GetDdsResponse", [self.store[u] for u in uuids if u in self.store])

    def get_by_internal_reference(self, req: ET.Element, username: str) -> tuple[int, bytes]:
        ref = text(req, "internalReference")
        items = [d for d in self.store.values() if d.statement.internal_reference == ref]
        return self._overview_list("GetDdsByInternalReferenceResponse", items)

    def get_by_identifiers(self, req: ET.Element, username: str) -> tuple[int, bytes]:
        rv = child(req, "referenceAndVerificationNumber")
        dds = self._by_reference(text(rv, "referenceNumber") or "")
        if dds is not None:
            self._refresh(dds)
        if dds is None or dds.verification_number != text(rv, "verificationNumber") or dds.status == "SUBMITTED":
            return business_fault([("EUDR-NOT-FOUND", "No statement matches these identifiers")])
        resp = ET.Element(q("dds", "GetDdsByIdentifiersResponse"))
        soap.statement_element(dds.statement, resp)
        return ok(resp)


# ---------------------------------------------------------------------- responses


def _wrap(body_content: ET.Element) -> bytes:
    env = ET.Element(q("soapenv", "Envelope"))
    header = sub(env, "soapenv", "Header")
    sec = sub(header, "wsse", "Security")
    ts = sub(sec, "wsu", "Timestamp")
    now = datetime.now(timezone.utc)
    sub(ts, "wsu", "Created", format_timestamp(now))
    sub(ts, "wsu", "Expires", format_timestamp(now + timedelta(seconds=5)))
    sub(env, "soapenv", "Body").append(body_content)
    return ET.tostring(env, encoding="utf-8", xml_declaration=True)


def ok(body_content: ET.Element) -> tuple[int, bytes]:
    return 200, _wrap(body_content)


def fault(code: str, message: str, detail: ET.Element | None = None) -> tuple[int, bytes]:
    f = ET.Element(q("soapenv", "Fault"))
    ET.SubElement(f, "faultcode").text = code
    ET.SubElement(f, "faultstring").text = message
    if detail is not None:
        ET.SubElement(f, "detail").append(detail)
    return 500, _wrap(f)


def business_fault(errors: list[tuple[str, str]]) -> tuple[int, bytes]:
    exc = ET.Element(f"{{{NS['dds']}}}BusinessRulesValidationException")
    for code, message in errors:
        err = ET.SubElement(exc, f"{{{NS['dds']}}}error")
        ET.SubElement(err, f"{{{NS['dds']}}}code").text = code
        ET.SubElement(err, f"{{{NS['dds']}}}message").text = message
    return fault("soapenv:Client", "Business rules validation failed", exc)


# ---------------------------------------------------------------------- HTTP


def make_server(host: str, port: int, mock: MockTraces) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != SERVICE_PATH:
                self._send(404, b"Not Found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            status, payload = mock.handle(self.rfile.read(length))
            ctype = "text/xml; charset=utf-8" if payload.startswith(b"<?xml") else "text/plain"
            self._send(status, payload, ctype)

        def do_GET(self) -> None:  # noqa: N802
            counts: dict[str, int] = {}
            with mock.lock:
                for d in mock.store.values():
                    mock._refresh(d)
                    counts[d.status] = counts.get(d.status, 0) + 1
                body = f"DEFORRO TRACES sandbox: {mock.request_count} requests, statements {counts}\n"
            self._send(200, body.encode(), "text/plain")

        def _send(self, status: int, payload: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            log.debug("%s " + fmt, self.address_string(), *args)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve_in_background(mock: MockTraces, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    server = make_server(host, port, mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}{SERVICE_PATH}"
