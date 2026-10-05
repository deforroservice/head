"""Bulk pipeline: validate -> risk gate -> submit/amend -> poll for reference numbers."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

from . import ledger as L
from .client import TracesClient, TransportError
from .loader import LoadedRecord
from .models import Statement
from .soap import SoapFault
from .validation import ValidationConfig, validate_statement

log = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    concurrency: int = 4
    dry_run: bool = False
    amend_changed: bool = False
    # Under EUDR goods may only be placed on the market when the risk is negligible
    # (or has been mitigated to that level).
    allowed_risk_levels: frozenset[str] = frozenset({"negligible"})
    allow_missing_risk: bool = False
    poll_batch: int = 50
    batch_id: str | None = None
    validation: ValidationConfig = field(default_factory=ValidationConfig)


Outcome = str  # submitted | amended | recovered | unchanged | invalid | blocked_risk | ...


class BulkUploader:
    def __init__(
        self,
        client: TracesClient | None,
        ledger: L.Ledger,
        config: PipelineConfig | None = None,
        on_result: Callable[[str, Outcome, str], None] | None = None,
    ):
        self.client = client
        self.ledger = ledger
        self.config = config or PipelineConfig()
        self.on_result = on_result or (lambda ref, outcome, detail: None)
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()

    # ------------------------------------------------------------------ submit

    def run(self, records: Iterable[LoadedRecord]) -> Counter:
        """Process records with bounded concurrency; returns outcome counts."""
        counts: Counter = Counter()
        max_in_flight = self.config.concurrency * 4
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as pool:
            in_flight: set[Future] = set()
            for record in records:
                in_flight.add(pool.submit(self._safe_process, record))
                if len(in_flight) >= max_in_flight:
                    done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                    counts.update(f.result() for f in done)
            done, _ = wait(in_flight)
            counts.update(f.result() for f in done)
        return counts

    def _safe_process(self, record: LoadedRecord) -> Outcome:
        try:
            outcome, detail = self.process(record)
        except Exception as e:  # never let one record kill the batch
            log.exception("unexpected error for %s", record.key)
            self.ledger.upsert(record.key, "crash", str(e), state=L.FAILED, last_error=str(e))
            outcome, detail = "failed", str(e)
        self.on_result(record.key, outcome, detail)
        return outcome

    def process(self, record: LoadedRecord) -> tuple[Outcome, str]:
        if record.statement is None:
            msg = "; ".join(record.errors)
            existing = self.ledger.get(record.key)
            if existing is None or not existing.uuid:
                self.ledger.upsert(record.key, "load_error", record.errors, state=L.INVALID, last_error=msg)
            return "invalid", msg

        stmt = record.statement
        ref = stmt.internal_reference
        with self._seen_lock:
            if ref in self._seen:
                return "duplicate_in_input", f"internal reference {ref} appears more than once"
            self._seen.add(ref)

        payload_hash = stmt.payload_hash()
        existing = self.ledger.get(ref)
        live = existing is not None and existing.uuid and existing.state in (L.SUBMITTED, L.DONE)

        if live and existing.payload_hash == payload_hash:
            return "unchanged", existing.reference_number or existing.uuid or ""

        result = validate_statement(stmt, self.config.validation)
        issues = json.dumps([asdict(i) for i in result.issues]) if result.issues else None
        if not result.ok:
            msg = "; ".join(str(i) for i in result.errors)
            if live:
                self.ledger.upsert(ref, "update_invalid", msg, issues=issues)
            else:
                self.ledger.upsert(
                    ref, "invalid", msg, state=L.INVALID, issues=issues, last_error=msg,
                    payload_hash=payload_hash, batch_id=self.config.batch_id,
                )
            return "invalid", msg

        gate = self._risk_gate(stmt)
        if gate:
            if not live:
                self.ledger.upsert(
                    ref, "blocked_risk", gate, state=L.BLOCKED_RISK, last_error=gate,
                    risk_level=stmt.risk.level if stmt.risk else None, issues=issues,
                    payload_hash=payload_hash, batch_id=self.config.batch_id,
                )
            return "blocked_risk", gate

        if live:
            return self._amend(stmt, existing, payload_hash, issues)

        if existing is not None and existing.state == L.WITHDRAWN:
            return "withdrawn", "statement was withdrawn; submit under a new internal reference"
        if existing is not None and existing.state == L.REJECTED and existing.payload_hash == payload_hash:
            return "unchanged_rejected", existing.last_error or "rejected by TRACES; fix the data first"

        if existing is not None and existing.state == L.SUBMITTING:
            recovered = self._recover(ref)
            if recovered:
                return "recovered", recovered

        if self.config.dry_run:
            return "would_submit", ""
        return self._submit(stmt, payload_hash, issues)

    def _risk_gate(self, stmt: Statement) -> str | None:
        risk = stmt.risk
        if risk is None:
            return None if self.config.allow_missing_risk else "no DEFORRO risk assessment attached"
        if risk.level in self.config.allowed_risk_levels or risk.mitigated:
            return None
        return f"risk level {risk.level!r} is not cleared for submission (score={risk.score})"

    def _recover(self, ref: str) -> str | None:
        """A previous run crashed mid-request: ask TRACES whether it got the DDS."""
        assert self.client is not None
        try:
            found = [o for o in self.client.get_dds_by_internal_reference(ref) if o.uuid]
        except (SoapFault, TransportError) as e:
            log.warning("recovery lookup for %s failed: %s", ref, e)
            return None
        if not found:
            return None
        latest = max(found, key=lambda o: (o.date or "", o.version or ""))
        self.ledger.upsert(
            ref, "recovered", asdict(latest), state=L.SUBMITTED, uuid=latest.uuid,
            traces_status=latest.status, last_error=None,
        )
        return latest.uuid

    def _submit(self, stmt: Statement, payload_hash: str, issues: str | None) -> tuple[Outcome, str]:
        assert self.client is not None
        ref = stmt.internal_reference
        self.ledger.upsert(
            ref, "submitting", None, state=L.SUBMITTING, payload_hash=payload_hash, issues=issues,
            risk_level=stmt.risk.level if stmt.risk else None, batch_id=self.config.batch_id,
            last_error=None,
        )
        self.ledger.bump_attempts(ref)
        try:
            uuid = self.client.submit_dds(stmt)
        except SoapFault as e:
            self.ledger.upsert(ref, "rejected", str(e), state=L.REJECTED, last_error=str(e))
            return "rejected", str(e)
        except TransportError as e:
            # Unknown whether TRACES received it, so stay SUBMITTING: the next run looks the
            # DDS up by internal reference before resubmitting, which avoids duplicates.
            self.ledger.upsert(ref, "transport_error", str(e), state=L.SUBMITTING, last_error=str(e))
            return "failed", str(e)
        self.ledger.upsert(ref, "submitted", {"uuid": uuid}, state=L.SUBMITTED, uuid=uuid, traces_status=None)
        return "submitted", uuid

    def _amend(self, stmt: Statement, existing: L.Row, payload_hash: str, issues: str | None) -> tuple[Outcome, str]:
        ref = stmt.internal_reference
        if not self.config.amend_changed:
            self.ledger.upsert(ref, "changed_not_amended", None)
            return "changed", "content differs from the submitted DDS; rerun with --amend-changed"
        if self.config.dry_run:
            return "would_amend", existing.uuid or ""
        assert self.client is not None and existing.uuid
        self.ledger.bump_attempts(ref)
        try:
            _, status = self.client.amend_dds(existing.uuid, stmt)
        except (SoapFault, TransportError) as e:
            self.ledger.upsert(ref, "amend_failed", str(e), last_error=str(e))
            return "amend_failed", str(e)
        # Back to SUBMITTED so the next poll refreshes status and version.
        self.ledger.upsert(
            ref, "amended", {"status": status}, state=L.SUBMITTED, payload_hash=payload_hash,
            traces_status=status, issues=issues, last_error=None,
        )
        return "amended", existing.uuid

    # ------------------------------------------------------------------ poll

    def poll_once(self) -> Counter:
        assert self.client is not None
        counts: Counter = Counter()
        rows = self.ledger.pending_poll()
        by_uuid = {r.uuid: r for r in rows if r.uuid}
        uuids = list(by_uuid)
        for i in range(0, len(uuids), self.config.poll_batch):
            chunk = uuids[i : i + self.config.poll_batch]
            try:
                overviews = self.client.get_dds(chunk)
            except (SoapFault, TransportError) as e:
                log.warning("getDds failed for %d uuids: %s", len(chunk), e)
                counts["poll_error"] += len(chunk)
                continue
            for ov in overviews:
                row = by_uuid.get(ov.uuid)
                if row is None:
                    continue
                state = L.SUBMITTED
                if ov.status == "AVAILABLE" and ov.reference_number:
                    state = L.DONE
                elif ov.status == "REJECTED":
                    state = L.REJECTED
                elif ov.status in ("WITHDRAWN", "CANCELLED"):
                    state = L.WITHDRAWN
                elif ov.status in L.FINAL_TRACES_STATUSES and ov.reference_number:
                    state = L.DONE
                if state != row.state or ov.status != row.traces_status:
                    self.ledger.upsert(
                        row.internal_reference, "status", asdict(ov), state=state,
                        traces_status=ov.status, reference_number=ov.reference_number,
                        verification_number=ov.verification_number, version=ov.version,
                        last_error=f"TRACES status {ov.status}" if state == L.REJECTED else None,
                    )
                counts[state] += 1
        return counts

    def poll(self, wait_for_final: bool = False, interval: float = 5.0, timeout: float = 900.0) -> Counter:
        deadline = time.monotonic() + timeout
        while True:
            counts = self.poll_once()
            if not wait_for_final or not self.ledger.pending_poll() or time.monotonic() > deadline:
                return counts
            time.sleep(interval)

    # ------------------------------------------------------------------ withdraw

    def withdraw(self, ref: str) -> str | None:
        assert self.client is not None
        row = self.ledger.get(ref)
        if row is None or not row.uuid:
            raise ValueError(f"{ref} has no submitted DDS in this ledger")
        status = self.client.withdraw_dds(row.uuid)
        self.ledger.upsert(ref, "withdrawn", {"status": status}, state=L.WITHDRAWN, traces_status=status)
        return status
