# M1-01A Ledger Schema And Audit Execution Record

## Scope

This record belongs to the new M1 execution-unit plan. It is not a new milestone by itself.

Implemented a runnable internalization ledger subsystem for source-to-target tracking across later M1/M2/M3 units.

## Delivered Surface

- Package: `packages/integrations/zyra_integrations`
- Seed ledger: `packages/integrations/zyra_integrations/data/internalization_ledger_seed.json`
- CLI: `scripts/zyra_integration_ledger.py`
- Seed builder: `scripts/build_integration_ledger_seed.py`
- Verification: `scripts/verify_internalization_ledger.py`
- API:
  - `GET /ledger`
  - `GET /ledger/{ledger_id}`
  - `GET /ledger/audit`
  - `POST /ledger/audit`
  - `POST /ledger/seed`
  - `POST /ledger/entries`
  - `GET /ledger/readiness`
  - `GET /ledger/report`
  - `GET /ledger/accounting`
  - `GET /ledger/linecount`
  - `GET /ledger/snapshots`
  - `POST /ledger/snapshots`
  - `POST /ledger/{ledger_id}/advance`
  - same aliases under `/integrations/ledger`

Strict supplement modules added after the initial 01A review:

- `ledger_policy.py`
- `ledger_linecount.py`
- `ledger_reports.py`
- `ledger_snapshots.py`
- `ledger_workflow.py`
- `ledger_source_scan.py`
- `ledger_matrix.py`
- `ledger_gate.py`
- `ledger_selectors.py`
- `ledger_remediation.py`
- `ledger_handoff.py`
- `ledger_health.py`
- `ledger_contracts.py`
- `ledger_accounting.py`
- `ledger_policy_matrix.py`

## Internalization Ledger

The seed ledger contains 802 records covering:

- `claude-code-best`
- `browser-use`
- `OpenHands`
- `openclaw`
- `agentscope`
- `agent-framework`
- `hermes-agent`
- `langgraph`

The ledger distinguishes planned source-mapped records from legacy M0 active records. Planned records are warnings when targets are not materialized; active/connected records fail audit if their targets, runtime entries, tests, main-path binding, or NOTICE state are invalid.

## Validation

Validated with:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.unit.test_internalization_ledger
.\.venv\Scripts\python.exe -m unittest tests.unit.test_internalization_ledger_policy_linecount
.\.venv\Scripts\python.exe -m unittest tests.unit.test_internalization_ledger_reports_workflow
.\.venv\Scripts\python.exe -m unittest tests.unit.test_internalization_ledger_handoff_health
.\.venv\Scripts\python.exe -m unittest tests.unit.test_internalization_ledger_contracts
.\.venv\Scripts\python.exe -m unittest tests.unit.test_internalization_ledger_accounting
.\.venv\Scripts\python.exe -m unittest tests.integration.test_internalization_ledger_cli
.\.venv\Scripts\python.exe -m unittest tests.integration.test_internalization_ledger_gate_cli
.\.venv\Scripts\python.exe -m unittest tests.integration.test_internalization_ledger_api
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts\verify_submission_boundary.py
.\.venv\Scripts\python.exe scripts\zyra_integration_ledger.py audit --strict --write-event --event-log tmp\ledger-audit-events.jsonl --fail-on-error
.\.venv\Scripts\python.exe scripts\verify_internalization_ledger.py --base 68587549447cacfdbf7992387823b5af6f7f9cf3 --cached --unit M1-01A --minimum-effective-lines 10000 --fail-on-shortfall
```

Audit status after implementation: `ok=True`, `errors=0`, `blockers=0`, with warnings for planned targets and M3 NOTICE finalization debt.

Strict line-count status after supplement: `effective_added=10049`, `raw_added=118210`, `excluded_added=108161`, `minimum=10000`, `line_count_ok=True`. The excluded lines are seed/list/index data and are not counted as effective implementation.

## Critical Review

- This unit deliberately does not mark future M1/M2 modules as already productized.
- The large seed file is used by the runtime ledger, CLI, API, audit, and tests, but it must be reported as ledger data scale, not as effective source-code internalization.
- The raw diff was inflated by roughly 108,161 lines of seed ledger data. Those lines cannot be counted as productized runtime code, cannot be presented as reused upstream source code, and cannot be used as evidence that heavy source-code internalization has already happened.
- Future units must convert planned ledger entries into real `zyra` modules through source migration, adapter encapsulation, runtime integration, tests, and main-path binding. Large data files, inventories, schemas, test volume, thin wrappers, or glue code cannot satisfy line-count requirements by themselves.
- The audit currently treats many planned entries as warnings. Later units must update those entries to active/internalized only after actual target paths, tests, runtime entries, and main-path bindings exist.
- M1-01A now includes strict line-count classification, completion gate, readiness report, source-scan, workflow guarded advance, contract summary, and source/unit/target accounting. These exist to prevent later units from claiming progress through unintegrated data files or thin records.
- Follow-up self-review fixed the completion-gate semantics for infrastructure units: `M1-01A` and other `requires_source_migration=False` units no longer fail solely because they have no source-to-target migration records, while migration units such as `M1-01B` still require ledger coverage.
- Final line-count audit after commit should use:

```powershell
.\.venv\Scripts\python.exe scripts\verify_internalization_ledger.py --base 68587549447cacfdbf7992387823b5af6f7f9cf3 --unit M1-01A --minimum-effective-lines 10000 --fail-on-shortfall
```

## 2026-07-07 Strict Revalidation

This pass added the policy matrix to accounting and the completion gate, so connected main-path claims backed by `vendor-runtimes`, source-pool, sidecar, planned, or candidate strategies are rejected before a unit can close.

Validated in this pass:

```text
accounting --owner-unit M1-01A: findings=0 errors=0 blockers=0
gate --owner-unit M1-01A: ok=True disposition=warning errors=0 blockers=0
verify_internalization_ledger.py: effective_added=31765 raw_added=320986 excluded_added=289221 line_count_ok=True
01A related unittest group: 40 tests OK
full unittest discover with failfast: 207 tests OK
verify_submission_boundary.py: passed
```

The remaining gate warnings are global ledger warnings and excluded data/source-pool dominance warnings. Seed, inventory, source-pool, and vendor-like physical lines remain excluded from effective implementation credit.

The `unit-review` API now treats `no_reports=1` as a lightweight evidence summary and avoids running full boundary/reachability/cleanroom/test-quality scans on the request path. Full strict report generation remains available through `--include-reports`.
