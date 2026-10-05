"""deforro-traces: bulk DDS upload to the EUDR Information System (TRACES NT)."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone

from . import ledger as L
from . import soap
from .client import RateLimiter, RetryPolicy, TracesClient, TransportError
from .config import ConfigError, credentials_from_env, resolve_endpoint
from .loader import load
from .mock_server import MockConfig, MockTraces, make_server
from .pipeline import BulkUploader, PipelineConfig
from .samples import generate, write_jsonl
from .soap import SoapFault
from .validation import validate_statement


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except (ConfigError, L.LedgerError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (SoapFault, TransportError) as e:
        print(f"TRACES error: {e}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deforro-traces", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sp = p.add_subparsers(required=True, metavar="command")

    def env_args(cmd: argparse.ArgumentParser, ledger: bool = True) -> None:
        cmd.add_argument("--env", choices=["mock", "acceptance", "production"], default="mock")
        cmd.add_argument("--endpoint", help="override the service URL (e.g. a mock on another port)")
        cmd.add_argument("--profile", help="credential prefix, e.g. ACME reads ACME_TRACES_USERNAME")
        cmd.add_argument("--confirm-production", action="store_true", help="required with --env production")
        cmd.add_argument("--rate", type=float, default=2.0, help="max requests per second (default 2)")
        cmd.add_argument("--timeout", type=float, default=60.0)
        cmd.add_argument("--retries", type=int, default=5)
        if ledger:
            cmd.add_argument("--ledger", default="deforro-ledger.db")

    c = sp.add_parser("sample", help="generate a synthetic DEFORRO export")
    c.add_argument("--count", type=int, default=100)
    c.add_argument("--seed", type=int, default=7)
    c.add_argument("--prefix", default="DEF")
    c.add_argument("--risky-rate", type=float, default=0.05)
    c.add_argument("--invalid-rate", type=float, default=0.03)
    c.add_argument("--out", default="shipments.jsonl")
    c.set_defaults(func=cmd_sample)

    c = sp.add_parser("validate", help="pre-flight check an export without contacting TRACES")
    c.add_argument("input")
    c.add_argument("--show-warnings", action="store_true")
    c.set_defaults(func=cmd_validate)

    c = sp.add_parser("render", help="print the SOAP request for one statement (secrets masked)")
    c.add_argument("input")
    c.add_argument("--ref", help="internal reference (default: first record)")
    c.set_defaults(func=cmd_render)

    c = sp.add_parser("mock-server", help="run the local TRACES sandbox")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8085)
    c.add_argument("--processing-delay", type=float, default=2.0)
    c.add_argument("--reject-rate", type=float, default=0.0)
    c.add_argument("--fault-rate", type=float, default=0.0)
    c.add_argument("--latency", type=float, default=0.0)
    c.add_argument("--seed", type=int)
    c.set_defaults(func=cmd_mock_server)

    c = sp.add_parser("submit", help="bulk submit (or amend) statements from an export")
    c.add_argument("input")
    env_args(c)
    c.add_argument("--concurrency", type=int, default=4)
    c.add_argument("--dry-run", action="store_true", help="validate + gate + ledger lookups only")
    c.add_argument("--amend-changed", action="store_true", help="amend DDSs whose content changed")
    c.add_argument("--allow-risk", action="append", default=[], metavar="LEVEL",
                   help="also submit this risk level (repeatable); default only 'negligible'")
    c.add_argument("--allow-missing-risk", action="store_true")
    c.add_argument("--batch-id")
    c.add_argument("--wait", action="store_true", help="poll until every DDS has a final status")
    c.add_argument("--quiet", action="store_true")
    c.set_defaults(func=cmd_submit)

    c = sp.add_parser("poll", help="fetch status, reference and verification numbers")
    env_args(c)
    c.add_argument("--wait", action="store_true")
    c.add_argument("--interval", type=float, default=5.0)
    c.add_argument("--max-wait", type=float, default=900.0)
    c.set_defaults(func=cmd_poll)

    c = sp.add_parser("report", help="summarise the ledger, optionally export CSV")
    c.add_argument("--ledger", default="deforro-ledger.db")
    c.add_argument("--env", choices=["mock", "acceptance", "production"], default="mock")
    c.add_argument("--csv", help="write one row per statement to this file")
    c.add_argument("--ref", help="show the event history for one internal reference")
    c.set_defaults(func=cmd_report)

    c = sp.add_parser("withdraw", help="withdraw a submitted DDS by internal reference")
    c.add_argument("ref")
    env_args(c)
    c.set_defaults(func=cmd_withdraw)

    c = sp.add_parser("get", help="look a DDS up directly in TRACES")
    env_args(c, ledger=False)
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--uuid")
    g.add_argument("--internal-ref")
    g.add_argument("--reference", nargs=2, metavar=("REFERENCE", "VERIFICATION"))
    c.set_defaults(func=cmd_get)
    return p


# ---------------------------------------------------------------------- helpers


def make_client(args: argparse.Namespace) -> TracesClient:
    if args.env == "production" and not args.confirm_production:
        raise ConfigError("refusing to talk to production without --confirm-production")
    creds = credentials_from_env(args.env, args.profile)
    return TracesClient(
        resolve_endpoint(args.env, args.endpoint),
        creds,
        timeout=args.timeout,
        retry=RetryPolicy(attempts=args.retries),
        rate_limiter=RateLimiter(args.rate),
    )


def print_counts(title: str, counts: Counter) -> None:
    print(f"{title}: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do"))


# ---------------------------------------------------------------------- commands


def cmd_sample(args: argparse.Namespace) -> int:
    records = generate(args.count, args.seed, args.prefix, args.risky_rate, args.invalid_rate)
    write_jsonl(records, args.out)
    print(f"wrote {len(records)} statements to {args.out}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    counts: Counter = Counter()
    for rec in load(args.input):
        if rec.statement is None:
            counts["load_error"] += 1
            print(f"{rec.key}: LOAD ERROR {'; '.join(rec.errors)}")
            continue
        result = validate_statement(rec.statement)
        counts["invalid" if not result.ok else "ok"] += 1
        counts["warnings"] += len(result.warnings)
        for issue in result.errors + (result.warnings if args.show_warnings else []):
            print(f"{rec.key}: {issue}")
    print_counts("validation", counts)
    return 1 if counts["invalid"] or counts["load_error"] else 0


def cmd_render(args: argparse.Namespace) -> int:
    from .config import MOCK_AUTH_KEY, MOCK_CLIENT_ID, MOCK_USERNAME, Credentials
    from .wsse import make_token

    for rec in load(args.input):
        if rec.statement and (args.ref is None or rec.key == args.ref):
            token = make_token(Credentials(MOCK_USERNAME, MOCK_AUTH_KEY, MOCK_CLIENT_ID))
            xml = soap.envelope(token, "YOUR_CLIENT_ID", soap.submit_request(rec.statement))
            print(soap.redact(xml))
            return 0
    print("no matching statement", file=sys.stderr)
    return 1


def cmd_mock_server(args: argparse.Namespace) -> int:
    mock = MockTraces(
        MockConfig(
            processing_delay=args.processing_delay,
            reject_rate=args.reject_rate,
            fault_rate=args.fault_rate,
            latency=args.latency,
            seed=args.seed,
        )
    )
    server = make_server(args.host, args.port, mock)
    from .config import MOCK_AUTH_KEY, MOCK_CLIENT_ID, MOCK_USERNAME, SERVICE_PATH

    print(f"TRACES sandbox on http://{args.host}:{args.port}{SERVICE_PATH}")
    print(f"credentials: username={MOCK_USERNAME} auth key={MOCK_AUTH_KEY} client id={MOCK_CLIENT_ID}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    client = make_client(args)
    ledger = L.Ledger(args.ledger, args.env)
    batch_id = args.batch_id or datetime.now(timezone.utc).strftime("batch-%Y%m%dT%H%M%SZ")

    def progress(ref: str, outcome: str, detail: str) -> None:
        if not args.quiet:
            print(f"{ref:<24} {outcome:<20} {detail[:120]}")

    uploader = BulkUploader(
        client,
        ledger,
        PipelineConfig(
            concurrency=args.concurrency,
            dry_run=args.dry_run,
            amend_changed=args.amend_changed,
            allowed_risk_levels=frozenset({"negligible", *map(str.lower, args.allow_risk)}),
            allow_missing_risk=args.allow_missing_risk,
            batch_id=batch_id,
        ),
        on_result=progress,
    )
    started = time.monotonic()
    counts = uploader.run(load(args.input))
    elapsed = time.monotonic() - started
    print_counts(f"{batch_id} ({elapsed:.1f}s)", counts)
    if args.wait and not args.dry_run:
        print_counts("poll", uploader.poll(wait_for_final=True))
    print_counts("ledger", Counter(ledger.counts()))
    return 0 if not counts.get("failed") else 1


def cmd_poll(args: argparse.Namespace) -> int:
    uploader = BulkUploader(make_client(args), L.Ledger(args.ledger, args.env))
    counts = uploader.poll(args.wait, args.interval, args.max_wait)
    print_counts("poll", counts)
    print_counts("ledger", Counter(uploader.ledger.counts()))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    ledger = L.Ledger(args.ledger, args.env)
    if args.ref:
        row = ledger.get(args.ref)
        if row is None:
            print("not in ledger", file=sys.stderr)
            return 1
        print(json.dumps(row.__dict__, indent=2))
        for ev in ledger.events(args.ref):
            print(f"{ev['at']}  {ev['event']:<20} {ev['detail'] or ''}")
        return 0
    print_counts("ledger", Counter(ledger.counts()))
    if args.csv:
        rows = ledger.rows()
        cols = [
            "internal_reference", "state", "traces_status", "uuid", "reference_number",
            "verification_number", "version", "risk_level", "attempts", "batch_id",
            "last_error", "updated_at",
        ]
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in rows:
                w.writerow([getattr(r, c) for c in cols])
        print(f"wrote {len(rows)} rows to {args.csv}")
    return 0


def cmd_withdraw(args: argparse.Namespace) -> int:
    uploader = BulkUploader(make_client(args), L.Ledger(args.ledger, args.env))
    print(f"{args.ref}: {uploader.withdraw(args.ref)}")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    client = make_client(args)
    if args.uuid:
        result = [o.__dict__ for o in client.get_dds([args.uuid])]
    elif args.internal_ref:
        result = [o.__dict__ for o in client.get_dds_by_internal_reference(args.internal_ref)]
    else:
        stmt = client.get_dds_by_identifiers(*args.reference)
        result = stmt.__dict__ if stmt else None
    print(json.dumps(result, indent=2, default=lambda o: getattr(o, "__dict__", str(o))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
