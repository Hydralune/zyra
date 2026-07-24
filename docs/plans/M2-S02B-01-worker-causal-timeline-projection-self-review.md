# M2-S02B-01 worker causal timeline projection self-review

Date: 2026-07-24

Authority:
`docs/milestones/M2-console-demo/slice-02b-01-worker-causal-timeline-projection.md`

## Frozen commit interval

- slice baseline:
  `0980c52e1a0624a06501ccd0c39a723e624c95e3`
- preimplementation decision:
  `9ce8748e05705809e40f581f8761f76dc26d007b`
- production implementation:
  `139f22db6ae51850ffcc12bc474f5a06ee57af28`
- implementation target including the required TaskDetail reachability test:
  `636ba5dc9887c9d563fb0ba2949f50b6d0fcbe42`
- evidence commit: the commit containing this review, the exact auditor,
  machine-readable evidence and synchronized source ledger; its hash is
  recorded in `docs/milestones/execution-state.yaml`.

No production code changed after `139f22d`. The later implementation-target
commit adds only the explicit assertion that the production TaskDetail route
imports and mounts the workbench after the existing topology panel. The exact
line audit is bound to the final implementation target and gives that test zero
production credit.

## Verdict

M2-S02B-01 is complete. Parent M2-02B remains open for
M2-S02B-02 recovery/operator interaction.

The real task-detail path now contains a read-only worker causal timeline over
the immutable M2-01B canonical projection. It provides:

- `queued`, `admitted`, `starting`, `running`, `waiting-tool`,
  `waiting-policy`, `recovering`, `completed`, `failed` and `cancelled`
  phase handling;
- stable rows joining worker, lease, span, tool, policy, artifact, mutation,
  route/placement, failure, recovery, browser-step and background-job facts;
- an explicit and implicit causal graph with missing-target diagnostics,
  bounded closure and deterministic cycle handling;
- immutable worker/lease epochs and explicit replacement lineage;
- failure/recovery attempt chains with idempotent terminal collapse;
- a deterministic critical path over the unfiltered causal graph;
- worker, phase, kind, time, search, failure and critical-path filters;
- hidden causal-gap summaries retaining range and boundary evidence;
- stable bounded windows, selection reveal and follow-latest behavior;
- topology, tool, permission, artifact, failure, recovery, event, span,
  checkpoint, mutation, route and worker drill-down descriptors;
- browser action/result/state step grouping and error/partial handling;
- background job park/revive lineage; and
- loading, reconnect, partial evidence, error and fail-closed disabled states.

The workbench does not submit retry, restart, cancel, reroute, permission or
recovery commands. Those remain assigned to M2-S02B-02.

## Source role and language result

| Source | Role | Language result | Effective lines |
| --- | --- | --- | ---: |
| OpenCode | `primary_implementation` | 5,623 raw TypeScript/TSX lines were cropped and integrated against Zyra canonical selectors; 4,344 conservative effective lines | 4,344 |
| OpenHands | `supplementary_implementation` | 1,552 raw same-language TypeScript lines for status precedence, late reconciliation and causal filtering; 1,425 effective | 1,425 |
| browser-use | `supplementary_implementation` | bounded Python-to-TypeScript semantic port of history-step grouping only; 323 raw / 290 effective | 290 |
| oh-my-pi | `supplementary_implementation` | 855 raw same-language TypeScript lines for background park/revive and recovery lineage; 777 effective | 777 |
| Agent Framework | `conformance_only` | lifecycle vocabulary and terminal priority tests only | 0 |
| LangGraph | `conformance_only` | narrow interrupt/resume and exact-lineage behavior tests only | 0 |

The source-language custody check reports:

- OpenCode: 5,609 raw additions across its explicitly listed production
  custody paths;
- OpenHands: 1,552;
- browser-use semantic port: 323;
- oh-my-pi: 855; and
- no language-custody violations.

The slight OpenCode raw difference between the role auditor and custody report
is the 14-line export index: the role auditor attributes that feature export
to OpenCode while the custody policy intentionally lists only behavioral
production modules.

OpenClaw remains `excluded_forward_only`. It was not read, restored, compared,
listed as a decision source or added as a runtime/build dependency.

## Cross-language exception

`browser-step-adapter.ts` is the only cross-language production item. It ports
the bounded observable semantics of browser-use `AgentHistory`:

- actions, result and state sharing a step identity form one inspectable step;
- action order is deterministic;
- a missing result stays partial;
- errors remain attached to the corresponding step; and
- state and artifact evidence is represented by safe canonical references.

It does not port or invoke the browser controller, DOM owner, action executor,
retry loop, history persistence, agent loop or Python process. A browser-side
TypeScript projection is necessary because the canonical inputs already reach
the web store; a Python sidecar would add an external process and a second
presentation state owner.

The focused tests cover grouped action/result/state facts, a late active event
after settled evidence, missing-result partial status, error status and the
disabled projector. The real M1 integration reaches the same canonical
projector without a browser-use runtime dependency.

## Canonical owners and data flow

| State | Canonical owner | Slice behavior |
| --- | --- | --- |
| frontend event/entity facts, revision, cursor and reconnect state | M2-01B TypeScript `CanonicalProjectionStore` | immutable read input |
| worker, lease and physical worker lifecycle | existing M1 worker runtime / `WorkerPoolStore` | projected into immutable epochs |
| failure/watchdog facts | existing M1 watchdog/fault runtime | displayed and causally joined |
| recovery plan, attempt idempotency and checkpoint truth | existing M1 recovery planner/stores | displayed and grouped, never replanned |
| scheduler route and placement | existing scheduler/provider owners | displayed, never rescored |
| tools, permissions and artifacts | existing M1 owners | linked by canonical identity |
| derived timeline rows, graph and critical path | `selectWorkerCausalTimeline` | pure revisioned projection |
| filter, window, selection and expansion | `TimelineWorkbenchController` | transient browser view state |

Read path:

`RuntimeEventSpine -> TaskEventTransport/EventIngressCoordinator ->
CanonicalProjectionStore -> selectWorkerCausalTimeline ->
TimelineWorkbenchController -> WorkerCausalTimelineWorkbench`.

There is no mutation path in this slice.

## Internalization boundary

The upstream mechanisms were decomposed into Zyra modules rather than kept in
source-shaped directories:

- `event-reader.ts`, `phase-machine.ts` and `worker-epochs.ts` map canonical
  state to historical event-local phase facts;
- `causal-graph.ts` owns only derived event relations and bounded closure;
- `event-reconciliation.ts` owns stable semantic row identity and late/partial
  fold behavior;
- `recovery-chain.ts`, `background-lifecycle.ts` and
  `browser-step-adapter.ts` own independent, testable derived groupings;
- `critical-path.ts`, `filtering.ts`, `drilldown.ts` and `windowing.ts` expose
  operator-visible semantics;
- `projector.ts` is the sole rich selector/projector entry;
- the controller owns transient view state; and
- the TSX workbench is mounted by the production TaskDetail route.

The preimplementation plan named separate `event-classification.ts` and
`tool-artifact-join.ts` candidates. During implementation their responsibility
proved inseparable from safe attribute normalization and drill-down/evidence
construction. They were folded into `event-reader.ts` and `drilldown.ts`
instead of adding disconnected helpers. All declared behaviors remain covered;
the source-to-target ledger records actual files.

Deleting or disconnecting these modules changes real behavior:

- disabling `WorkerCausalTimelineProjectionEngine` throws
  `timeline_projection_disabled`;
- removing phase/reconciliation logic fails lifecycle, late-event and
  failure/recovery tests;
- removing causal graph or critical path logic fails edge and path assertions;
- removing filter/window logic fails gap and reveal assertions;
- removing browser/background adapters fails step and revival assertions;
- removing the TaskDetail mount fails the explicit main-path test and the web
  production build; and
- the real M1 Python integration fails if the canonical-store-to-projector
  path is disconnected.

## Historical-event corrections found by real integration

The real M1 integration exposed two errors that fixture-only tests did not make
obvious:

1. the canonical worker entity contains the latest lifecycle, so consulting it
   before event-local `worker.running` and `worker.completed` tokens rewrote
   historical phases after a later failure; and
2. a current recovery entity points back to its triggering failure, so
   prioritizing that entity caused the failure row to be folded into the
   recovery row.

The final projector gives explicit historical event transitions precedence
over the latest entity snapshot and retains a failure boundary before the
recovery attempt. Focused tests and the real integration both pass after the
correction.

## Real behavior and failure-path evidence

`apps/web/test/worker-causal-timeline.test.ts` has 12 tests and 100
assertions covering:

- all lifecycle, tool, policy, browser, failure, recovery and terminal phases;
- causal joins and reversible event/target indexes;
- worker replacement, restart and lease epochs;
- duplicate recovery terminals versus distinct attempts;
- critical-path determinism;
- hidden causal gaps under worker, phase, type, time and search filters;
- partial and late-event reconciliation with stable row keys;
- background park/revive lineage;
- bounded windows and selected-row reveal;
- revision-aware selector and engine caching;
- fail-closed disabled projection;
- transient controller behavior without canonical mutation; and
- the production TaskDetail mount.

`tests/integration/test_worker_causal_timeline_projection.py` does not replay a
fixture. It runs:

1. `zyra_orchestration.run_task_graph`;
2. a real `FAILURE_INJECTED` event through
   `zyra_symbolic.apply_failure_injection`;
3. the resulting topology/resource decisions, execute running/completed
   updates, `NODE_FAILED` and `RECOVERY_PLANNED` facts through
   `normalizeEventFrame`;
4. `CanonicalProjectionStore.apply`; and
5. `buildWorkerCausalTimeline`.

It proves the CodeWorkerRuntime failure, BrowserWorker replacement route,
recovery plan, distinct failure/recovery rows, worker epochs, critical path,
causal edges, caught-up diagnostics and disabled-projector failure.

## Verification

Passed against the final implementation target:

```text
node_modules/.bin/bun.exe test ./apps/web/test/worker-causal-timeline.test.ts
12 pass, 0 fail, 100 expect() calls

node_modules/.bin/bun.exe test \
  ./apps/web/test/canonical-projection-store.test.ts \
  ./apps/web/test/topology-projection.test.ts \
  ./apps/web/test/topology-interaction.test.ts \
  ./apps/web/test/workbench-shell.test.tsx
69 pass, 0 fail, 382 expect() calls

.venv/Scripts/python.exe -m pytest \
  tests/integration/test_worker_causal_timeline_projection.py \
  tests/scenarios/test_m5_scheduler_fault_recovery.py -q
3 passed

node_modules/.bin/bun.exe run typecheck:web
passed

node_modules/.bin/bun.exe run build:web
153 modules bundled

node_modules/.bin/bun.exe scripts/audit_m2_s02b_01_effective_lines.mjs --summary
effective_production=6836
minimum=6000
line_count_ok=true

.venv/Scripts/python.exe \
  scripts/sync_m2_worker_causal_timeline_source_ledger.py --check
6 decisions, 6 entries, 0 missing targets

.venv/Scripts/python.exe scripts/verify_source_language_custody.py \
  --evidence \
  docs/reviews/evidence/M2-S02B-01-worker-causal-timeline-projection.json \
  --base 0980c52e1a0624a06501ccd0c39a723e624c95e3 \
  --target 636ba5dc9887c9d563fb0ba2949f50b6d0fcbe42
ok=true, violations=[]
```

The focused and adjacent suites fit the ordinary-slice validation budget.
Full cleanroom and full-repository tests remain assigned to the M2-02 numeric
stage aggregate after M2-S02B-02.

## Exact effective-line accounting

The auditor uses exact Git added-line sets. TypeScript compiler AST and scanner
classification exclude imports, interfaces, type aliases, export-only
declarations, static contract constants, JSX presentation, comments and blank
lines. Tests, probes, docs, CSS, data, generated material, adapters and
vendor/source-pool content receive no production credit.

| Bucket | Lines |
| --- | ---: |
| raw additions / deletions | 10,999 / 0 |
| production runtime | 6,564 |
| active UI behavior | 272 |
| UI presentation excluded | 1,040 |
| type declarations excluded | 913 |
| schema/DTO/data excluded | 50 |
| tests/fixtures/probes excluded | 1,760 |
| docs/comments/blank excluded | 400 |
| adapter/generated/vendor/source-pool | 0 |
| effective production | **6,836** |
| slice minimum | **6,000** |
| margin | **836** |

No file contributes more than 20 percent of effective production. The largest
is `event-reader.ts` at 944 effective lines, 13.81 percent.

## Large-file and exclusion audit

Production files over 500 raw additions:

- `causal-graph.ts` (785 raw / 750 effective) constructs typed explicit and
  implicit edges, closure, row bindings and diagnostics.
- `drilldown.ts` (571 / 562) constructs safe canonical targets and evidence.
- `event-reader.ts` (982 / 944) performs safe canonical normalization and
  event-local phase/entity identity extraction.
- `event-reconciliation.ts` (652 / 597) provides stable identity, late/partial
  handling and row construction.
- `recovery-chain.ts` (588 / 539) groups attempts and collapses duplicate
  terminals.
- `controller.ts` (503 / 443) owns transient filtering, selection, reveal,
  follow-latest and announcements.
- `timeline-workbench.tsx` (751 / 272) contains active UI orchestration; 479
  JSX/type/presentation/comment lines are excluded.

Large excluded files:

- `contracts.ts` (566 / 47) is deliberately dominated by interfaces and static
  schema vocabulary.
- `styles.css` (586 / 0) is presentation only.
- `worker-causal-timeline-probe.ts` (538 / 0) is integration evidence only.
- `worker-causal-timeline.test.ts` (1,065 / 0) is behavior evidence only.

Files with more than 30 percent exclusion were also reviewed:

- `task-detail.tsx` and `state/index.ts` are mount/export glue and receive zero;
- `projection/index.ts` is export-only and receives zero;
- the TSX workbench counts only executable UI behavior;
- CSS, the preimplementation decision, Bun tests, the probe and Python
  integration receive zero; and
- `contracts.ts` counts only its small executable normalization functions.

No declarations, tests or presentation volume mask the runtime quota.

## Dependency, path and repository audit

The final implementation diff contains no:

- parent-source runtime path, absolute workspace path or OpenClaw reference;
- package or lockfile change, npm link or pip editable parent path;
- external Docker context;
- production subprocess, local port, MCP server or dynamic package import;
- credential/provider configuration; or
- cache, SQLite, build-product or pre-recorded trace dependency.

The slice-scoped ledger synchronizer reports six source decisions, six entries
and zero missing implementation targets.

The repository-wide generic ledger verifier still reports the same three
pre-existing blockers in unchanged
`packages/evaluation/zyra_evaluation/m1_hardening/long_horizon_runtime.py` for
historical `../OpenHands`, `../browser-use` and `../claude-code-best` strings.
That protected file is outside this slice and unchanged. The slice-specific
path scan and source ledger introduce no finding. The generic verifier's
coarse line counter reports the slice above 6,000 but is not used for the
conservative line claim.

## Risk and remaining work

No high-risk escalation trigger occurred:

- no incompatible public schema/event/persistence migration;
- no canonical owner transfer;
- no transaction, lease, idempotency or restore semantic change;
- no global permission, scheduler, recovery, compact or fallback change; and
- no external dependency, subprocess, port, MCP server, plugin or dynamic
  import.

M2-02B is not closed. This slice contributes 6,836 effective production lines
to the parent 12,000 minimum, leaving a conservative residual target of 5,164
for M2-S02B-02. The next slice owns recovery/operator controls, not this
read-only projection.

The next execution entry is `M2-S02B-02`.
