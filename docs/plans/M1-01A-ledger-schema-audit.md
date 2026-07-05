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
  - same aliases under `/integrations/ledger`

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
.\.venv\Scripts\python.exe -m unittest tests.integration.test_internalization_ledger_cli
.\.venv\Scripts\python.exe -m unittest tests.integration.test_internalization_ledger_api
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts\verify_submission_boundary.py
.\.venv\Scripts\python.exe scripts\zyra_integration_ledger.py audit --strict --write-event --event-log tmp\ledger-audit-events.jsonl --fail-on-error
```

Audit status after implementation: `ok=True`, `errors=0`, `blockers=0`, with warnings for planned targets and M3 NOTICE finalization debt.

## Critical Review

- This unit deliberately does not mark future M1/M2 modules as already productized.
- The large seed file is used by the runtime ledger, CLI, API, audit, and tests; it is not documentation-only line padding.
- The audit currently treats many planned entries as warnings. Later units must update those entries to active/internalized only after actual target paths, tests, runtime entries, and main-path bindings exist.
- Final line-count audit should be run after commit with:

```powershell
git diff --numstat 68587549447cacfdbf7992387823b5af6f7f9cf3 HEAD -- apps packages tests scripts vendor-runtimes skills
```
