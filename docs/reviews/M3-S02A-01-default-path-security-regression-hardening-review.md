# M3-S02A-01 Default Path Security Regression Hardening Review

## Verdict

`PASS_WITH_RECORDED_ADJACENT_TIMEOUT`

The slice is complete at implementation target
`c5fa87cccab900dfaa9b8a4148d8cbd896a04013`. The exact-revision
regression suite, real-owner live matrix, mutation/security assertions,
M3-01 prerequisite and freeze admission all pass. One older long-running
software-delivery scenario timed out twice at its existing 120-second HTTP
client limit; it produced no assertion failure, is not counted as passing
evidence, and remains an explicit M3 exit-review item.

## Frozen commit sequence

- Baseline:
  `42f760229588aba917c7283b841587afc7142b0b`
- Pre-implementation source/language/migration decision:
  `8d204c418d56970ba0b4bf53d9396229a953dc27`
- Main implementation:
  `3b85bbcd674882d967b9d1cd39f6c63f17de92a0`
- Receipt-at-rest integrity fix and final implementation target:
  `c5fa87cccab900dfaa9b8a4148d8cbd896a04013`
- Evidence commit: the commit containing this review and its evidence
  directory.

The second implementation commit was necessary because the first formal
freeze admission exposed a real defect: heuristic entropy redaction changed
the long evidence `artifact_root` to `G:[REDACTED]` after the suite digest
had been computed. The final implementation preserves the already-public,
bounded suite projection byte-for-byte while retaining exact secret-canary
rejection. A long-path regression test now closes that failure.

## Implemented Zyra boundaries

The new `zyra_evaluation.regression_hardening` package owns only evaluation
state:

- typed case/suite/attempt/observation/assertion receipts;
- dependency-aware registry, deterministic sharding and bounded process
  execution;
- isolated state roots, secure artifact admission and failure triage;
- default-path reachability, owner-disable and mutation evaluation;
- bidirectional event/effect causality and fake/late/cross-run/orphan
  rejection;
- clean cache/SQLite/index/artifact/build fingerprinting and pollution
  detection;
- deterministic LLM-control receipt checks for permission, scheduler,
  recovery and compact;
- Patch/Git transaction, stale, rollback, history, dirty/destructive and
  progressive-friction checks;
- secret/redaction and untrusted Web/MCP/browser-content checks;
- code-index permission, budget, generation, invalidation, patch refresh,
  context and test-selection checks;
- approval identity/action/policy/nonce/idempotency/timeout/stale,
  cross-session/race/crash-restore/sealed checks;
- a real-process Python/TypeScript/TSX live matrix and exact-revision freeze
  admission.

It does not own runtime session, permission, scheduler, recovery, workspace,
Git, index, approval or frontend projection state. Those owners remain in
their existing Python and TypeScript modules. Subject ports expose bounded
observations to the evaluator; missing ports block cases and never activate a
fallback owner.

## Main path and semantic effects

The formal live matrix executes ten fresh-process scenarios over the existing
product paths:

- CLI plus generated task execution through the default worker runtime;
- API task/workspace creation and code-index disable behavior;
- Web `CanonicalProjectionStore` event projection;
- Python and Web Patch/Git review paths;
- TypeScript `ApprovalLedger` race/restore behavior;
- sealed permission console behavior;
- code-index patch invalidation and selection refresh;
- browser worker permission/injection behavior;
- the evaluator's own fake-causality, pollution, digest-mutation and
  gate-disconnect tests.

The live receipt is bound into suite metadata by digest. The freeze gate
loads and verifies that separate receipt, requires Python and TypeScript plus
CLI/API/Web/worker coverage, then verifies all nine evaluator cases. A
passing synthetic mutation campaign therefore cannot replace or bypass the
real-owner matrix.

Disconnect and mutation evidence includes:

- missing subject port -> dependent case is `blocked`;
- disabled canonical owner -> explicit failure, no fallback owner and no
  state mutation;
- forged suite/case/live receipt digest -> freeze rejection;
- fake, late, cross-run, no-effect and orphan causality -> rejection;
- polluted clean-state output -> rejection;
- mock/fixture/legacy/source-repository fallback marker -> rejection;
- process timeout -> process-tree termination and failed receipt;
- forged approval identity/action/policy/nonce, cross-session, stale,
  late/duplicate terminal and restore-without-digest -> rejection.

## Clean state and lifecycle findings

The isolated matrix assigns fresh HOME/USERPROFILE/XDG/cache/temp roots,
scoped Git safe-directory configuration, bounded output and timeout handling.
It fingerprints the worktree before and after every live scenario and fails
on mutation.

This validation found two Windows file-lock leaks in existing M3 evaluation
stores. `ExperimentStore` and `ScenarioRunStore` used SQLite connection
context managers that committed or rolled back but did not close the
connection. Both now use explicit close-on-exit connection scopes. The
permission-console integration test also resets experiment and scenario
services explicitly. The clean isolated permission scenario passes after
these fixes.

## Source language and migration audit

Migration mode is `test_eval_hardening_only`.

- Python production was added for evaluation orchestration, campaigns,
  clean-state handling, real-process execution and freeze admission.
- TypeScript and TSX production owners were not translated or duplicated.
  Their existing Web, approval and Patch/Git paths are executed by Bun in the
  live matrix and by Web typecheck/build.
- No runtime dependency on a source repository, vendor tree, external
  service, new port or new package was introduced.
- OpenClaw remains `excluded_forward_only`.
- LangGraph remains limited to the previously adjudicated narrow
  checkpoint/exact-resume conformance role; this slice adds no LangGraph
  production owner.

## Effective-code gate

The audited interval is
`42f760229588aba917c7283b841587afc7142b0b..c5fa87cccab900dfaa9b8a4148d8cbd896a04013`.

| Bucket | Lines | Counts toward 6,000 |
| --- | ---: | --- |
| Production runtime in full range | 8,605 | Partly |
| Accounted slice evaluation production | 8,561 | Yes |
| Tests/mock/fixture | 916 | No |
| Non-production scripts | 324 | No |
| Schema/DTO/data | 1,845 | No |
| Type declarations | 243 | No |
| Docs/comments/blank | 772 | No |
| Raw additions | 12,705 | No |

The accounted result is 8,561 effective production lines against the 6,000
minimum, a margin of 2,561. The 43 production lines in adjacent SQLite
lifecycle fixes and one configuration line are deliberately excluded from
the slice minimum. Generated/data/vendor/adapter-only code contributes zero.

Thirteen cohesive evaluation modules exceed 500 physical lines. The
machine-readable line audit records their top-level symbols and rationale.
Each large file owns one campaign or orchestration domain; none is a bundled
upstream tree or data-as-code payload.

## Verification results

- Real-owner live matrix: 10 passed, 0 failed.
- Exact-revision evaluator suite: 9 passed, 0 failed.
- Freeze admission: valid, zero findings, 42 mutation assertions and two
  security observations.
- Final focused evaluator and permission tests: 7 passed.
- Adjacent sandbox/Git/code-index/patch/browser tests: 57 passed and six
  subtests passed.
- Web TypeScript typecheck: passed.
- Web build: passed.
- Existing M3 runtime plus regression freeze verification: passed.
- M3-01 aggregate prerequisite:
  `PASS_AFTER_FIXES`, verified by the freeze gate.

The broader experiment/scenario/API adjacency set produced 19 passes and one
120-second timeout. The timed-out scenario was rerun alone and timed out
again at the same client boundary. It is not silently converted to a pass,
not used by the slice gate, and should be rerun with the formal live-scenario
budget at the M3 exit review.

## Critical residual risks

1. The evaluator's typed mutation campaigns deliberately use generated
   observations so every negative invariant is deterministic. Their freeze
   eligibility depends on the separately executed and digest-bound live
   matrix. Removing that binding makes admission fail.
2. The live matrix currently runs serially by default because several
   product integration tests mutate process-wide environment. Sharding and
   bounded parallelism exist, but higher parallelism should be used only
   after those tests own per-process configuration completely.
3. The full Python repository and full Bun repository were not rerun for this
   ordinary slice. The changed and adjacent paths, real TypeScript owners,
   Web typecheck/build and M3 gate were run within the incremental review
   budget. Full-repository and long live-scenario execution remains mandatory
   at the numeric-stage or M3 exit review.
4. `python_lockfile_missing` remains the pre-existing M3-02B owner; this
   slice neither hides nor closes it.

## Evidence index

- `docs/reviews/evidence/M3-S02A-01/implementation-metadata.json`
- `docs/reviews/evidence/M3-S02A-01/verification-record.json`
- `docs/reviews/evidence/M3-S02A-01/effective-code-audit.json`
- `docs/reviews/evidence/M3-S02A-01/live-matrix-receipt.json`
- `docs/reviews/evidence/M3-S02A-01/freeze-admission.json`
- `docs/reviews/evidence/M3-S02A-01/runtime-artifacts/suite/suite-receipt.json`
- `docs/reviews/evidence/M3-S02A-01/runtime-artifacts/triage/triage-report.json`
- per-case runtime artifacts under
  `docs/reviews/evidence/M3-S02A-01/runtime-artifacts/`
