import copy

import pytest

from deforro_traces import ledger as L
from deforro_traces.client import RetryPolicy, TracesClient, TransportError
from deforro_traces.config import Credentials, MOCK_CLIENT_ID, MOCK_USERNAME
from deforro_traces.pipeline import BulkUploader, PipelineConfig
from deforro_traces.samples import generate
from deforro_traces.soap import SoapFault

from .conftest import loaded, record


def test_submit_poll_gets_reference_and_verification(client, ledger):
    up = BulkUploader(client, ledger)
    counts = up.run([loaded(record())])
    assert counts == {"submitted": 1}
    up.poll()
    row = ledger.get("DEF-TEST-0001")
    assert row.state == L.DONE and row.traces_status == "AVAILABLE"
    assert row.reference_number and len(row.verification_number) == 8

    # The official identifiers resolve back to the statement we sent.
    stmt = client.get_dds_by_identifiers(row.reference_number, row.verification_number)
    assert stmt.commodities[0].hs_heading == "1801"


def test_rerun_is_idempotent(client, ledger, mock):
    up = BulkUploader(client, ledger)
    up.run([loaded(record())])
    again = BulkUploader(client, ledger).run([loaded(record())])
    assert again == {"unchanged": 1}
    assert len(mock.store) == 1


def test_changed_content_needs_amend_flag(client, ledger, mock):
    BulkUploader(client, ledger).run([loaded(record())])
    changed = record()
    changed["commodities"][0]["net_weight_kg"] = 4800
    assert BulkUploader(client, ledger).run([loaded(changed)]) == {"changed": 1}
    up = BulkUploader(client, ledger, PipelineConfig(amend_changed=True))
    assert up.run([loaded(changed)]) == {"amended": 1}
    (dds,) = mock.store.values()
    assert dds.version == 2 and str(dds.statement.commodities[0].net_weight) == "4800"
    up.poll()
    assert ledger.get("DEF-TEST-0001").version == "2"


def test_risk_gate_blocks_non_negligible(client, ledger, mock):
    risky = record(internal_reference="R-HIGH", risk={"level": "high", "score": 0.8})
    missing = record(internal_reference="R-NONE")
    missing.pop("risk")
    mitigated = record(internal_reference="R-MIT", risk={"level": "standard", "mitigated": True})
    counts = BulkUploader(client, ledger).run([loaded(risky), loaded(missing), loaded(mitigated)])
    assert counts == {"blocked_risk": 2, "submitted": 1}
    assert ledger.get("R-HIGH").state == L.BLOCKED_RISK
    assert len(mock.store) == 1


def test_invalid_records_never_reach_traces(client, ledger, mock):
    bad = record(border_cross_country=None)
    assert BulkUploader(client, ledger).run([loaded(bad)]) == {"invalid": 1}
    assert ledger.get("DEF-TEST-0001").state == L.INVALID
    assert mock.request_count == 0


def test_server_side_rejection_is_recorded(client, ledger, mock):
    # Bypass local validation to prove TRACES-side faults land in the ledger.
    bad = record(grouped_declarations=["26FRNOTEXIST01"])
    counts = BulkUploader(client, ledger).run([loaded(bad)])
    assert counts == {"rejected": 1}
    row = ledger.get("DEF-TEST-0001")
    assert row.state == L.REJECTED and "not AVAILABLE" in row.last_error
    # Same payload again is not resent.
    assert BulkUploader(client, ledger).run([loaded(bad)]) == {"unchanged_rejected": 1}


def test_crash_recovery_does_not_duplicate(client, ledger, mock):
    stmt = loaded(record()).statement
    uuid = client.submit_dds(stmt)  # TRACES got it...
    ledger.upsert(stmt.internal_reference, "submitting", state=L.SUBMITTING, payload_hash="x")  # ...we crashed
    counts = BulkUploader(client, ledger).run([loaded(record())])
    assert counts == {"recovered": 1}
    assert ledger.get(stmt.internal_reference).uuid == uuid
    assert len(mock.store) == 1


def test_duplicate_reference_in_input(client, ledger):
    counts = BulkUploader(client, ledger).run([loaded(record()), loaded(record())])
    assert counts == {"submitted": 1, "duplicate_in_input": 1}


def test_async_rejection_and_withdraw(client, ledger, mock):
    mock.config.reject_rate = 1.0
    up = BulkUploader(client, ledger)
    up.run([loaded(record(internal_reference="REJ"))])
    mock.config.reject_rate = 0.0
    up.run([loaded(record(internal_reference="OK"))])
    up.poll()
    assert ledger.get("REJ").state == L.REJECTED
    assert up.withdraw("OK") == "WITHDRAWN"
    assert ledger.get("OK").state == L.WITHDRAWN


def test_grouped_declaration_flow(client, ledger, mock):
    up = BulkUploader(client, ledger)
    up.run([loaded(record(internal_reference="CHILD"))])
    up.poll()
    child_ref = ledger.get("CHILD").reference_number
    up.run([loaded(record(internal_reference="PARENT", grouped_declarations=[child_ref]))])
    up.poll()
    assert ledger.get("PARENT").state == L.DONE
    child = next(d for d in mock.store.values() if d.reference_number == child_ref)
    assert child.status == "GROUPED"
    with pytest.raises(SoapFault):
        up.withdraw("CHILD")


def test_wrong_key_is_an_auth_fault(mock):
    bad = TracesClient(mock.url, Credentials(MOCK_USERNAME, "wrong", MOCK_CLIENT_ID), retry=RetryPolicy(1))
    with pytest.raises(SoapFault) as exc:
        bad.get_dds(["x"])
    assert "FailedAuthentication" in exc.value.code


def test_wrong_client_id_is_rejected(mock, client):
    other = TracesClient(mock.url, Credentials(MOCK_USERNAME, client.credentials.auth_key, "eudr-repository"))
    with pytest.raises(SoapFault):
        other.get_dds(["x"])


def test_transient_faults_are_retried(client, ledger, mock):
    mock.config.fault_rate = 0.3
    # 0.3**20 per record: exhausting retries is effectively impossible, so no flake.
    client.retry = RetryPolicy(attempts=20, base_delay=0.001, max_delay=0.005)
    counts = BulkUploader(client, ledger, PipelineConfig(concurrency=8)).run(
        loaded(r) for r in generate(60, invalid_rate=0, risky_rate=0)
    )
    assert counts == {"submitted": 60}
    assert len(mock.store) == 60


def test_exhausted_retries_leave_row_recoverable(client, ledger, mock):
    mock.config.fault_rate = 1.0
    client.retry = RetryPolicy(attempts=2, base_delay=0.001, max_delay=0.001)
    assert BulkUploader(client, ledger).run([loaded(record())]) == {"failed": 1}
    assert ledger.get("DEF-TEST-0001").state == L.SUBMITTING
    mock.config.fault_rate = 0.0
    assert BulkUploader(client, ledger).run([loaded(record())]) == {"submitted": 1}


def test_dry_run_sends_nothing(client, ledger, mock):
    up = BulkUploader(client, ledger, PipelineConfig(dry_run=True))
    assert up.run([loaded(record())]) == {"would_submit": 1}
    assert mock.request_count == 0


def test_ledger_is_bound_to_one_environment(tmp_path):
    L.Ledger(tmp_path / "x.db", "acceptance").close()
    with pytest.raises(L.LedgerError):
        L.Ledger(tmp_path / "x.db", "production")


def test_transport_error_on_dead_endpoint():
    c = TracesClient("http://127.0.0.1:9/nope", Credentials("u", "k", "c"),
                     timeout=1, retry=RetryPolicy(attempts=2, base_delay=0.001))
    with pytest.raises(TransportError):
        c.get_dds(["x"])
