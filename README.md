# deforro-traces

Bulk submission of EUDR **Due Diligence Statements** from DEFORRO to the EU
Information System (TRACES NT), plus a **local sandbox** that speaks the same SOAP API.

```
DEFORRO export (JSONL / CSV + GeoJSON)
   │
   ├─ 1. load        one record per internal reference, bad lines don't stop the batch
   ├─ 2. validate    EUDR pre-flight: GeoJSON rings/ranges/points>4ha, HS heading, countries, measures
   ├─ 3. risk gate   only "negligible" (or mitigated) DEFORRO risk results go through
   ├─ 4. submit      EUDRDueDiligenceStatementServiceV3.submitDds  (WS-Security PasswordDigest)
   │                 or amendDds when content changed (--amend-changed)
   ├─ 5. poll        getDds → status + reference number + verification number
   └─ ledger (SQLite) every step recorded; reruns are idempotent and crash-safe
```

No runtime dependencies (Python ≥ 3.10 stdlib only).

## Quick start (sandbox, no EU credentials needed)

```bash
pip install -e '.[dev]'

# terminal 1: fake TRACES with 2s processing time, 5% async rejections, 10% HTTP 503s
deforro-traces mock-server --processing-delay 2 --reject-rate 0.05 --fault-rate 0.1

# terminal 2
deforro-traces sample --count 1000 --out shipments.jsonl     # synthetic DEFORRO export
deforro-traces validate shipments.jsonl                       # local checks only
deforro-traces submit shipments.jsonl --env mock --concurrency 8 --rate 20 --wait
deforro-traces report --env mock --csv results.csv            # uuid, reference, verification, status
deforro-traces report --env mock --ref DEF-000042             # full event history for one DDS
```

`results.csv` is the hand-back to the client: one row per statement with the official
**reference number + verification number** that goes on the customs declaration or
gets passed downstream.

## Moving to the EU environments

1. The client registers as an operator in TRACES NT (EU Login, validated operator profile).
2. In their profile they enable **Web Services Access** and get an API username,
   an authentication key and a client identifier.
3. Start in **acceptance**:

```bash
export TRACES_USERNAME=...  TRACES_AUTH_KEY=...  TRACES_CLIENT_ID=...
deforro-traces submit shipments.jsonl --env acceptance --ledger acme-acceptance.db --rate 2 --wait
```

4. Production has to be asked for explicitly:

```bash
deforro-traces submit shipments.jsonl --env production --confirm-production --ledger acme-prod.db
```

**Several DEFORRO clients:** each operator has its own credentials. Use `--profile`,
so `--profile acme` reads `ACME_TRACES_USERNAME`, `ACME_TRACES_AUTH_KEY` and `ACME_TRACES_CLIENT_ID`.
Keep one ledger per operator per environment. A ledger refuses to open under another
environment, because UUIDs from acceptance are meaningless in production.

## Commands

| command | what it does |
|---|---|
| `sample` | synthetic export (cocoa, coffee, palm, soya, rubber, wood; polygons + points; some risky / broken records) |
| `validate FILE` | pre-flight validation, exit code 1 if anything is invalid |
| `render FILE [--ref R]` | print the exact SOAP envelope for one statement, secrets masked |
| `mock-server` | run the sandbox (`GET /` shows request and status counters) |
| `submit FILE` | validate → gate → submit/amend, with `--concurrency`, `--rate` (req/s), `--dry-run`, `--amend-changed`, `--allow-risk LEVEL`, `--wait` |
| `poll [--wait]` | fetch status / reference / verification numbers for everything outstanding |
| `report [--csv F] [--ref R]` | ledger summary, CSV export, per-statement event history |
| `withdraw REF` | withdrawDds for a statement in the ledger |
| `get --uuid U \| --internal-ref R \| --reference REF VER` | direct lookups in TRACES |

## Input format

**JSONL**, one statement per line ([example](examples/shipments.example.jsonl)):

```json
{"internal_reference": "DEF-2026-0001", "activity_type": "IMPORT",
 "country_of_activity": "BE", "border_cross_country": "BE",
 "risk": {"level": "negligible", "score": 0.02, "assessment_id": "RA-1"},
 "commodities": [{
   "description": "Cocoa beans, whole, raw", "hs_heading": "1801", "net_weight_kg": 5000,
   "species": [{"scientific_name": "Theobroma cacao", "common_name": "Cacao"}],
   "producers": [{"country": "GH", "name": "Kofi Cooperative",
                  "geojson_file": "plots/ghana-kofi-coop.geojson"}]}]}
```

A producer takes either `geojson` (inline FeatureCollection) or `geojson_file`,
which is resolved relative to the input file. Optional fields: `supplementary_unit`,
`supplementary_unit_qualifier`, `operator_role` (default `OPERATOR`),
`geo_location_confidential`, `grouped_declarations` (DDS reference numbers).

**CSV**, one row per producer plot ([example](examples/shipments.example.csv)). Rows
that share `internal_reference` become one statement. Within a statement, rows that
share `hs_heading` and `description` become one commodity.

## How bulk runs stay safe

* **Idempotent:** `internal_reference` is the key. Rerunning the same file skips
  everything already submitted with identical content (compared by payload hash).
  When the content changed, the statement is reported as `changed` and is amended
  only with `--amend-changed`.
* **Crash-safe:** a row is marked `SUBMITTING` before the request goes out. If the
  process dies, or retries run out mid-request, the next run first calls
  `getDdsByInternalReference` to recover the UUID and only resubmits if TRACES has no
  record of it. This prevents duplicate DDSs.
* **Retries:** network errors and HTTP 408/429/5xx without a SOAP body back off
  exponentially with jitter. SOAP faults (validation, authentication, business rules)
  are final and are recorded as `REJECTED` along with the fault detail. Every attempt
  gets a fresh WS-Security nonce and timestamp.
* **Throughput:** `--rate` is a token bucket shared across all workers, so
  concurrency never exceeds what TRACES allows. Locally the sandbox handles about 200 DDS/s.
  Against the EU services, start low (1–2 req/s) and increase only within the limits
  you've been told.
* **Risk gate:** under EUDR a product can only be placed on the market when the
  risk is negligible or has been mitigated to that level. Anything else ends up in
  `BLOCKED_RISK` and is never sent. `--allow-risk` exists for test runs.

Ledger states: `INVALID`, `BLOCKED_RISK`, `SUBMITTING`, `SUBMITTED`, `DONE`
(AVAILABLE, with reference and verification numbers), `REJECTED`, `WITHDRAWN`.

## What the sandbox simulates

* WS-Security UsernameToken PasswordDigest check, timestamp window, nonce replay,
  `WebServiceClientId` (defaults: `sandbox` / `sandbox-auth-key` / `eudr-test`)
* submitDds / amendDds / withdrawDds / getDds / getDdsByInternalReference /
  getDdsByIdentifiers, with response shapes taken from the official samples
* SUBMITTED → AVAILABLE or REJECTED after `--processing-delay`, versions on amend,
  grouped declarations (referenced DDSs become `GROUPED` and can't be withdrawn)
* business-rule SOAP faults from the same validation rules, injected 503s
  (`--fault-rate`) and latency (`--latency`)

It is a simulator, not a conformance test. Before going live, confirm field
enumerations and limits against the official XSD and *Validation Rules* page, using
the acceptance environment. In particular, check operatorRole values, the maximum
GeoJSON size, the maximum UUIDs per getDds call (`poll_batch`, default 50) and rate limits.

## Browser sandbox

`web/` holds a single-page version of the sandbox for people who shouldn't need a
terminal. A DEFORRO export is loaded and processed entirely in the browser tab: pre-flight,
risk gate, WS-Security signing, filing to an in-page mock TRACES, polling and results.
Files are never uploaded anywhere.

* `web/engine.js`: JavaScript port of the loaders, validation, SOAP builder, mock and uploader
* `web/sandbox.html`: the page (the engine is inlined at build time)
* `python web/build.py` writes `web/dist/deforro-dds-sandbox.html`, which you can open
  from disk or publish

`tests/test_web_parity.py` runs the JavaScript engine under Node against the same inputs
as this package: validation outcomes on 30 edge cases and 400 generated records, the CSV
example, the password digest, and the SOAP body field by field. It fails if the two
implementations disagree, so change both together.

## Tests

```bash
python -m pytest -q
```

The tests cover the digest formula, envelope layout against the official sample,
parsing of the official getDds response, validation rules, CSV/JSONL loading, and the
full pipeline against the in-process sandbox: idempotency, amend, risk gate, crash
recovery, async rejection, withdraw, grouped declarations, authentication failures
and retry under injected faults.
