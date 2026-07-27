# M3-S02A-02 critical self-review

## Verdict

`BLOCKED_FORMAL_LIVE_EXECUTION`

The implementation and focused/adjacent behavior tests pass, and the
conservative effective-production line gate passes. The slice completion gate
does not pass because Zyra has no product-bound fresh-live execution owner
that can supply the required two-domain, seven-variant, three-repetition
formal matrix.

The protected M2-S05-02 archives are valid prior live evidence and
M2-S05-03 is a valid controlled workload/ablation result. They are not new
M3 live repetitions. Relabeling them would violate the slice, M3 README and
the preimplementation decision. The new admission runtime therefore rejects
replay, fixture, synthetic and protected-prior evidence at the formal task-run
boundary while still allowing protected M1 deployment/provider receipts to be
explicitly linked as prior evidence.

No execution-state completion update is authorized.

## Frozen revisions

- Baseline:
  `c01fcacf7cd30c37aa9629aed76a7df5256dad63`
- Preimplementation decision:
  `dc44781f12eea1cddef6b5a2953050033d15def3`
- Implementation:
  `a057335d43cb83209905750383340084ae044545`
- Evidence:
  filled by the commit containing this review

## Implemented product responsibilities

The implementation adds
`packages/evaluation/zyra_evaluation/live_benchmark/**` with:

1. exact two-domain x seven-variant x repetition planning and paired-input
   fairness;
2. exactly-one-capability ablation verification;
3. fresh input, clean state, exact commit/condition and live-source admission;
4. sealed policy, zero human/operator intervention and deny/replan checks;
5. canonical semantic-step classification and inflation rejection;
6. device/edge/cloud isolation and provider/model evidence admission;
7. privacy, SLA, tier/model split, failover and browser-independent
   disconnect degradation checks;
8. exception, worker/node/provider/edge/network/tool fault coverage,
   requirement changes, resume and MTTR;
9. deterministic software/research verifiers and isolated optional model
   judge disagreement;
10. complete raw metric blocks, P50/P95, dispersion, bootstrap confidence and
    paired deltas;
11. atomic campaign/cell custody, leases, resume and append-only hash journal;
12. evidence member/Merkle integrity, 100-point evidence index/report
    composition and freeze admission.

The live execution port is deliberately unbound by default. Disabling or
omitting a real owner produces `benchmark-live-run-port-unbound`; archive or
fixture receipts produce formal admission failures.

## Validation

Focused:

```text
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider \
  --basetemp .tmp\pytest-m3-live \
  tests\unit\test_m3_live_benchmark.py

5 passed in 0.77s
```

Adjacent M2 experiment/API plus focused M3 benchmark:

```text
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider \
  --basetemp .tmp\pytest-m3-adjacent2 \
  tests\scenarios\test_m2_s05_03_experiment_matrix.py \
  tests\integration\test_experiment_evidence_api_main_path.py \
  tests\unit\test_m3_live_benchmark.py

11 passed, 2 pre-existing duplicate-zip-member warnings in 135.66s
```

Compilation:

```text
python -m compileall -q
  packages/evaluation/zyra_evaluation/live_benchmark

passed
```

The first combined adjacent command hit the 120-second command timeout after
the five focused tests had completed. It was rerun with a 300-second timeout
and completed successfully as recorded above.

## Effective-code audit

Interval:

`c01fcacf7cd30c37aa9629aed76a7df5256dad63..a057335d43cb83209905750383340084ae044545`

Raw additions:

- production package/export: `7,456`
- direct tests: `349`
- total: `7,805`

Conservative production bucket:

- behavior-bearing production: `6,797`
- schema/type-only models: `410`
- package exports: `87`
- declarative metric/requirement/variant table portions and non-behavior
  declarations: `162`
- tests: `349`
- docs/review/evidence: excluded from implementation interval
- generated/data/vendor/source-pool/adapter-only/mock-only: `0`

Minimum: `6,000`

Result: `PASS` with `797` conservative effective-production lines of margin.

Files above 500 lines were individually reviewed:

- `admission.py`: accepting/rejecting fresh state, policy, time and artifacts;
- `canonical.py`: canonicalization, validation, hashing, path and atomic I/O;
- `deployment.py`: tier/provider/route/failover/disconnect semantic checks;
- `matrix.py`: complete paired matrix and isolated ablation checks;
- `semantic_steps.py`: semantic classification, ancestry and inflation checks;
- `store.py`: leases, transitions, recovery, immutable result and journal;
- `verifiers.py`: deterministic software/research and model-judge isolation.

No file is generated, vendor-like, a source pool, a mock owner or a thin
adapter. `models.py` is conservatively excluded as schema/type-only.

## Source/language and dependency review

- Migration mode remains `test_eval_hardening_only`.
- New production language is Python, matching the existing evaluation
  runtimes.
- No TypeScript/Rust owner was rewritten or transferred.
- No external dependency, dynamic import, MCP server, provider CLI, port,
  Docker context, plugin or root-source path was added.
- OpenClaw remains `excluded_forward_only`.
- No LangGraph dependency or StateGraph/Pregel/channel/ToolNode/store/stream
  path was added.
- Canonical session/event/artifact/permission/scheduler/memory/recovery/
  checkpoint/provider owners remain unchanged.

## Blocking evidence

Repository reachability inspection found
`DualDomainScenarioExecutor` production code but no product composition of
`DualDomainOwnerBindings`; the only bound composition is the
`LiveOwnerHarness` in
`tests/scenarios/test_m2_s05_02_live_scenarios.py`.

Using that harness for formal M3 evidence would make the provider/tier/owner
observations test-owned and therefore invalid. The M2-S05-03 formal runner
explicitly states that it uses controlled frozen evidence and performs no new
provider/model request. It cannot be reclassified as the required fresh live
matrix.

Consequently these required outputs do not exist and must not be fabricated:

- 42 fresh formal cell receipts;
- at least three distinct fresh repetitions per cell pairing;
- a current-commit live run at or above 2,000 effective transitions;
- current formal raw sample matrix and paired statistics;
- 100-point evidence index;
- formal benchmark report and evidence manifest;
- `PASS` freeze-gate receipt.

## Required remediation

Before resuming this slice, a product-owned live composition must bind:

- canonical task/event owner;
- artifact publisher;
- placement/tier owner;
- provider/model owner or exact still-valid protected provider receipts;
- fault/recovery/checkpoint owner;
- software and research domain executor.

That composition must produce fresh, clean-state run receipts for the formal
campaign without a test harness, replay, fixture or synthetic owner. Once it
exists, run the 42-cell campaign, generate/report/verify the evidence bundle,
integrate the freeze gate into `scripts/verify_m3.py`, run the parent M3-02A
cumulative effective-line and main-path closeout, then update
`execution-state.yaml`.

Because creating that composition would be the first product owner binding
for this live path, it crosses the current M3 evaluation-only boundary and
cannot be silently invented inside this evidence commit.
