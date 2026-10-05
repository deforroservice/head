"""HTTP client for EUDRDueDiligenceStatementServiceV3 with retries and rate limiting."""

from __future__ import annotations

import logging
import random
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import soap
from .config import Credentials
from .models import DdsOverview, Statement
from .wsse import make_token

log = logging.getLogger(__name__)

RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


class TransportError(Exception):
    """Network failure or non-SOAP HTTP error that survived all retries."""


@dataclass
class RetryPolicy:
    attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 30.0

    def delay(self, attempt: int) -> float:
        # Exponential backoff with full jitter.
        return random.uniform(0, min(self.max_delay, self.base_delay * 2 ** (attempt - 1)))


class RateLimiter:
    """Token bucket shared by all worker threads (requests per second)."""

    def __init__(self, rate: float, burst: int | None = None):
        self.rate = rate
        self.capacity = burst or max(1, int(rate))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        if self.rate <= 0:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


class TracesClient:
    def __init__(
        self,
        endpoint: str,
        credentials: Credentials,
        timeout: float = 60.0,
        retry: RetryPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        token_ttl: int = 60,
    ):
        self.endpoint = endpoint
        self.credentials = credentials
        self.timeout = timeout
        self.retry = retry or RetryPolicy()
        self.rate_limiter = rate_limiter
        self.token_ttl = token_ttl

    # ------------------------------------------------------------------ plumbing

    def build_envelope(self, body: ET.Element) -> bytes:
        token = make_token(self.credentials, self.token_ttl)
        return soap.envelope(token, self.credentials.client_id, body)

    def call(self, body: ET.Element) -> ET.Element:
        op = soap.local(body.tag)
        last_error: Exception | None = None
        for attempt in range(1, self.retry.attempts + 1):
            if self.rate_limiter:
                self.rate_limiter.acquire()
            # A fresh token (nonce + timestamp) per attempt, or replays get rejected.
            payload = self.build_envelope(body)
            request = urllib.request.Request(
                self.endpoint,
                data=payload,
                method="POST",
                headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    return soap.parse_body(resp.read(), resp.status)
            except urllib.error.HTTPError as e:
                data = e.read()
                if b"Fault" in data:
                    # SOAP faults come back as HTTP 500 and are final: validation,
                    # authentication and business-rule errors don't fix themselves.
                    return soap.parse_body(data, e.code)
                if e.code not in RETRYABLE_HTTP:
                    raise TransportError(f"{op}: HTTP {e.code} {data[:200]!r}") from None
                last_error = TransportError(f"{op}: HTTP {e.code}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_error = TransportError(f"{op}: {e}")
            if attempt < self.retry.attempts:
                delay = self.retry.delay(attempt)
                log.warning("%s failed (%s), retry %d in %.1fs", op, last_error, attempt, delay)
                time.sleep(delay)
        raise last_error or TransportError(op)

    # ------------------------------------------------------------------ operations

    def submit_dds(self, stmt: Statement) -> str:
        resp = self.call(soap.submit_request(stmt))
        uuid = soap.text(resp, "uuid")
        if not uuid:
            raise TransportError("submitDds: response has no uuid")
        return uuid

    def amend_dds(self, uuid: str, stmt: Statement) -> tuple[str, str | None]:
        resp = self.call(soap.amend_request(uuid, stmt))
        return soap.text(resp, "uuid") or uuid, soap.text(resp, "status")

    def withdraw_dds(self, uuid: str) -> str | None:
        resp = self.call(soap.withdraw_request(uuid))
        return soap.text(resp, "status")

    def get_dds(self, uuids: list[str]) -> list[DdsOverview]:
        resp = self.call(soap.get_dds_request(uuids))
        return [soap.parse_overview(el) for el in soap.children(resp, "ddsOverviewList")]

    def get_dds_by_internal_reference(self, ref: str) -> list[DdsOverview]:
        resp = self.call(soap.get_by_internal_reference_request(ref))
        return [soap.parse_overview(el) for el in soap.children(resp, "ddsOverviewList")]

    def get_dds_by_identifiers(self, reference_number: str, verification_number: str) -> Statement | None:
        resp = self.call(soap.get_by_identifiers_request(reference_number, verification_number))
        st = soap.child(resp, "statement")
        return soap.parse_statement(st) if st is not None else None
