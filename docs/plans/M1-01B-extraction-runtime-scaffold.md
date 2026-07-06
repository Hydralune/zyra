# M1-01B Extraction Runtime Scaffold Execution Record

## Scope

This record belongs to the new M1 execution-unit plan. It establishes the reusable source extraction path and runtime scaffold used by M1-02A and later migration units.

Baseline for this unit: `ad985a0e563bf8dc8f5529ebb96c0668b34da362`.

## Subagents Used

The unit was started with the three required `gpt-5.5 xhigh` subagents:

- `extraction-rule-designer`: selected the safe Claude Code pilot scope and exclude rules.
- `runtime-scaffold-designer`: reviewed the session/tool/permission/MCP/skill/subagent/worker bridge contracts.
- `audit-script-reviewer`: reviewed ledger, accounting, line-count, source-scan, and gate requirements.

## Delivered Surface

- Source extraction module: `packages/integrations/zyra_integrations/source_extraction.py`
- Extraction CLI: `scripts/zyra_source_extract.py`
- Runtime scaffold contracts: `packages/runtime/zyra_runtime/scaffold.py`
- Worker scaffold health/smoke: `packages/workers/zyra_workers/runtime_scaffold.py`
- Runtime scaffold verifier: `scripts/verify_extraction_runtime_scaffold.py`
- Unit tests: `tests/unit/test_source_extraction_runtime_scaffold.py`
- Integration tests: `tests/integration/test_m1_01b_extraction_runtime_scaffold_cli.py`
- Productized runtime area: `vendor-runtimes/claude-code-runtime`

## Pilot Extraction

The pilot extraction copied 33 source files from `claude-code-best` into:

```text
vendor-runtimes/claude-code-runtime/pilot/claude-code-best
```

The copied files cover the M1-02A handoff surface for Claude Code runtime extraction without claiming to complete M1-02A:

- `src/Tool.ts`
- `src/tools.ts`
- `src/context.ts`
- `src/cost-tracker.ts`
- `src/services/tools/*`
- `src/query/config.ts`, `deps.ts`, `stopHooks.ts`, `tokenBudget.ts`
- `src/types/*`
- `src/utils/sessionState.ts`, `abortController.ts`, `generators.ts`, `systemPromptType.ts`
- `src/tools/BashTool/*`
- `src/tools/AgentTool/*`
- `src/tools/SkillTool/prompt.ts`

The pilot generated:

- `vendor-runtimes/claude-code-runtime/src/zyra-pilot-manifest.mjs`
- `vendor-runtimes/claude-code-runtime/src/zyra-pilot-smoke.mjs`
- `vendor-runtimes/claude-code-runtime/metadata/source_inventory.json`
- `vendor-runtimes/claude-code-runtime/package.json`

The inventory is an audit artifact only. It must not be counted as effective source code.

## Ledger

The extraction registered 33 `M1-01B` ledger entries with:

- `migration_strategy = vendored_runtime`
- `main_path_status = worker_runtime_connected`
- `lifecycle = active`
- runtime module `zyra_runtime.scaffold`
- test entry `tests/unit/test_source_extraction_runtime_scaffold.py`
- target surface `vendor_runtime`

The bundled seed ledger was also updated so the M1-01B source-to-target map is reproducible after a clean checkout.

Post-review accounting status for `M1-01B`:

```text
total_entries=33
missing_source_evidence_entries=0
missing_target_entries=0
missing_runtime_entries=0
missing_test_entries=0
policy_error_entries=0
policy_warning_entries=0
```

## Critical Review

- This unit performs real source migration into `zyra`, not a root-workspace `../claude-code-best` dependency.
- The copied pilot files are not a full Claude Code runtime. `QueryEngine.ts`, `query.ts`, full session restore, full permission enforcement, MCP client, command runtime, and productized CodeWorker API remain for M1-02A through M1-03D.
- The runtime scaffold is intentionally a contract and health/smoke boundary. It is not allowed to be treated as completion of permission, MCP, skill, subagent, browser, scheduler, or recovery runtime implementation.
- `source_inventory.json` and the seed ledger update are useful for audit, but they are excluded from effective line-count evidence.
- The pilot runtime is counted only because it is copied into `zyra/vendor-runtimes`, registered in the ledger, exposed through a manifest/smoke entry, and verified without relying on parent source repository paths at runtime.

## Validation

Validated during implementation with:

```powershell
.\.venv\Scripts\python.exe scripts\zyra_source_extract.py pilot-claude-code --dry-run
.\.venv\Scripts\python.exe scripts\zyra_source_extract.py pilot-claude-code --write-scaffold --write-ledger --update-seed
.\.venv\Scripts\python.exe scripts\zyra_source_extract.py smoke --json
node vendor-runtimes\claude-code-runtime\src\zyra-pilot-smoke.mjs
.\.venv\Scripts\python.exe -m unittest tests.unit.test_source_extraction_runtime_scaffold
.\.venv\Scripts\python.exe -m unittest tests.integration.test_m1_01b_extraction_runtime_scaffold_cli
.\.venv\Scripts\python.exe scripts\verify_extraction_runtime_scaffold.py
```

Cached line-count and gate before commit:

```text
raw_added=22092
effective_added=17372
excluded_added=4720
minimum_effective_lines=10000
line_count_ok=True
```

Excluded lines were `packages/integrations/zyra_integrations/data/internalization_ledger_seed.json` and `vendor-runtimes/claude-code-runtime/metadata/source_inventory.json`. They are ledger/report data and are not counted as effective implementation.

Final post-commit validation should additionally run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts\verify_submission_boundary.py
.\.venv\Scripts\python.exe scripts\zyra_integration_ledger.py audit --strict --fail-on-error
.\.venv\Scripts\python.exe scripts\zyra_integration_ledger.py accounting --owner-unit M1-01B --no-entries --json
.\.venv\Scripts\python.exe scripts\zyra_integration_ledger.py gate --owner-unit M1-01B --base ad985a0e563bf8dc8f5529ebb96c0668b34da362 --minimum-effective-lines 10000 --json
.\.venv\Scripts\python.exe scripts\verify_internalization_ledger.py --base ad985a0e563bf8dc8f5529ebb96c0668b34da362 --unit M1-01B --minimum-effective-lines 10000 --fail-on-shortfall
```
