# M3-S02A-01 default-path and security regression hardening decision

## Frozen interval

- Slice: `M3-S02A-01`
- Baseline commit:
  `42f760229588aba917c7283b841587afc7142b0b`
- Required predecessor:
  M3-01 independent aggregate verdict `PASS_AFTER_FIXES`
- Required predecessor receipt:
  `docs/reviews/evidence/M3-01-independent-review/aggregate-review-evidence.json`
- Required M3-02A work queue:
  `docs/reviews/evidence/M3-S01B-02/downstream/m3_02a.json`
- Slice minimum conservative effective production additions: `6,000`
- Parent M3-02A minimum after both slices: `12,000`

The implementation interval begins at the baseline above. This decision is
committed before production code. The implementation commit will be frozen
before evidence, receipts, review prose and execution-state updates are
committed.

Tests, fixtures, generated receipts, JSON evidence, documentation, source
maps, schemas or dataclasses without behavior, thin launch scripts, adapters,
mock-only probes, repetitive declarations, vendor-like content and cached
outputs receive zero effective-production credit.

## Protected starting facts

M3-01 already proves and protects:

1. eleven canonical state domains have one selected Zyra owner;
2. all eleven domains have a valid causal link and owner-loss proof;
3. fallback successes are zero when a selected owner is disabled;
4. clean default composition, config migration and restart are release-ready
   for the M3-01 boundary;
5. OpenClaw is `excluded_forward_only`;
6. LangGraph remains a narrow checkpoint/exact-resume semantic source and
   owns no default StateGraph/Pregel/channel/ToolNode/store/server path.

This slice does not reopen those source or owner decisions. It consumes their
exact-revision receipts and exercises the selected owners through generated
runtime inputs.

## Gap confirmed before implementation

The current evaluation package has a useful M1 hardening graph and M3-01
inventory engines, but the slice requirements are not yet closed:

1. the M1 cross-cutting gates primarily inspect source markers and supplied
   event arrays; they do not own a production-quality sharded regression
   execution runtime;
2. no common registry describes dependencies, timeouts, isolation, generated
   inputs, active-owner requirements and artifact contracts for the M3
   default-path and security cases;
3. default CLI/API/Web/worker traces are not normalized into one
   reachability/disable/mutation receipt;
4. causality checks do not reject every fake, late, orphaned, cross-run or
   semantically empty event in both event-to-effect and effect-to-event
   directions;
5. clean-state checks do not fingerprint cache, SQLite, index, artifact and
   build roots before and after a scenario, nor distinguish declared outputs
   from hidden pollution;
6. there is no executable boundary proof that LLM text cannot authorize,
   route, recover or compact without the corresponding deterministic Zyra
   control receipt;
7. Patch/Git, secret/injection, code-index and approval requirements do not
   share one generated-input security campaign and one failure-triage model;
8. the M3 verifier cannot consume a versioned M3-02A regression receipt, so an
   evaluator could remain a one-off script outside the freeze path.

These are genuine evaluation-runtime responsibilities. Removing the new
package must break generated-input regression execution, receipt validation
and the M3 freeze-gate check.

## Migration decision

The migration mode is `test_eval_hardening_only`.

The slice adds a Zyra-owned Python evaluation runtime under
`packages/evaluation/zyra_evaluation/regression_hardening/**`. It may invoke
or observe existing Python and TypeScript owners through their public
in-repository entrypoints. It does not copy their policy or become a fallback
owner.

The evaluation runtime owns only:

- regression case registration and dependency validation;
- shard planning, bounded execution, cancellation and timeout;
- per-case isolated workspace/state roots;
- generated-input mutation campaigns;
- normalized observations and assertions;
- artifact/receipt integrity, failure triage and report composition;
- admission of a completed regression receipt into the M3 verifier.

It does not own sessions, events, permission decisions, artifacts, memory,
scheduler routes, recovery plans, checkpoints, workspaces, code indexes,
provider credentials, terminal/browser sessions, patches or approvals.

## Source-language and target decision table

| Source repository/path | Source language | Target path | Target language | Migration mode | Runtime owner after slice |
| --- | --- | --- | --- | --- | --- |
| Zyra M1 hardening execution graph and cross-cutting gates | Python | `packages/evaluation/zyra_evaluation/regression_hardening/registry.py`, `orchestrator.py`, `triage.py` | Python | `test_eval_hardening_only` | evaluation registry/orchestration only |
| Zyra M3-01 freeze audit and protected receipts | Python/JSON evidence | `regression_hardening/default_path.py`, `freeze_gate.py` | Python | `test_eval_hardening_only` | derived reachability and receipt admission only |
| Zyra event/session/artifact/route/mutation owners | Python and TypeScript | `regression_hardening/causality.py` | Python | `test_eval_hardening_only` | existing owners; evaluator observes immutable facts |
| Zyra productization/config migration and owner registry | Python | `regression_hardening/clean_state.py`, `default_path.py` | Python | `test_eval_hardening_only` | existing productization and domain owners |
| Zyra permission/scheduler/recovery/compact boundaries | Python and TypeScript | `regression_hardening/control_boundary.py` | Python | `test_eval_hardening_only` | existing deterministic control owners |
| Zyra workspace/sandbox gateway patch and Git boundaries | Python and TypeScript | `regression_hardening/repository_security.py` | Python | `test_eval_hardening_only` | existing workspace/gateway/permission owners |
| Zyra secret redaction and Web/MCP/browser trust boundaries | Python and TypeScript | `regression_hardening/content_security.py` | Python | `test_eval_hardening_only` | existing redaction/trust/policy owners |
| Zyra CodeIndexRuntime and workspace revision owner | Python | `regression_hardening/code_index_security.py` | Python | `test_eval_hardening_only` | CodeIndex remains derived; workspace revision remains canonical |
| Zyra TypeScript approval runtime and approval ledger | TypeScript | `regression_hardening/approval_security.py` plus generated-input TypeScript probe entrypoint | Python + TypeScript probe | `test_eval_hardening_only` | existing TypeScript approval/policy owners |
| M3 verifier and package CLI entrypoints | Python | `scripts/verify_m3.py`, `pyproject.toml`, package CLI | Python | `test_eval_hardening_only` | evaluator receipt admission only |

There is no upstream production code migration quota and no cross-language
runtime-owner rewrite. Python is the orchestration/report language because the
existing evaluation and M3 audit packages are Python. TypeScript owners remain
TypeScript and are exercised through same-language generated-input probes or
their public process boundary. No `mixed` or `unknown` source decision exists.

## Runtime architecture

The planned evaluation path is:

```text
case catalog + suite profile
  -> dependency and capability validation
  -> deterministic shard plan
  -> isolated case context and generated input
  -> bounded concurrent executor
  -> normalized observations/assertions
  -> artifact integrity and failure triage
  -> suite receipt
  -> M3 freeze-gate admission
```

Every case declares:

- stable identity and version;
- tags, dependencies and required active-owner capabilities;
- timeout and isolation policy;
- generated-input strategy;
- produced observations and required artifacts;
- negative mutations and expected failure codes;
- whether disable, clean-state or security isolation is mandatory.

Unknown dependencies, dependency cycles, duplicate case identities, invalid
timeouts, undeclared artifacts, non-isolated mutations and success without
assertions fail closed.

## Required executable campaigns

The implementation will provide real campaign evaluators for:

1. default CLI/API/Web/worker reachability, active-owner disable and mutation;
2. bidirectional event/span/tool/artifact/route/mutation causality with fake,
   late, orphaned, cross-run and no-op negative inputs;
3. clean cache/SQLite/index/artifact/build roots, fresh inputs and pollution
   detection;
4. deterministic permission/scheduler/recovery/compact custody against
   adversarial LLM suggestions;
5. Patch/Git stale preconditions, atomicity, rollback/history, dirty worktree,
   destructive denial and progressive friction;
6. secret blocking/redaction and untrusted Web/MCP/browser injection;
7. code-index budget, permission, incremental rebuild, patch refresh and
   downstream selection effects;
8. approval identity/digest/policy/nonce/idempotency/timeout/stale,
   cross-session/race/crash-restore and sealed no-hang behavior.

Fixture/replay data may be used only as a negative input or parser unit test.
The accepting path must create fresh case input and observe a real owner or
public product boundary.

## Receipt and security contract

All suite and case receipts are canonical JSON with:

- exact Git target;
- suite/case version and generated-input digest;
- start/end monotonic and UTC time;
- isolation root identity;
- owner capability and entrypoint observations;
- assertions, mutations, disable outcomes and failure codes;
- produced artifact digests;
- redacted diagnostics;
- deterministic receipt digest.

Secrets and authorization values are never stored in the receipt. Diagnostic
values pass through the existing `SecretRedactor`; the evaluator additionally
scans its final JSON bytes for canary material. Artifact paths must remain
inside the declared evaluation root. Symlink escapes, absolute undeclared
paths and duplicate artifact names fail closed.

## Effective-code accounting commitment

The final review will compute the interval
`42f760229588aba917c7283b841587afc7142b0b..<implementation_commit>`.

Per-file buckets are:

- production evaluation/orchestration/report behavior;
- test;
- docs/review;
- generated evidence;
- configuration/data;
- schema/type-only;
- adapter/launcher-only;
- mock/fixture;
- vendor/source-pool.

Every file over 500 raw additions, over 20 percent of effective production or
over 30 percent excluded content receives an individual review. Repeated case
tables, assertion declarations, report text and test-only helper code will not
be counted as effective production.

If the real responsibilities above cannot naturally supply 6,000
conservative effective production lines, implementation stops with a planning
blocker before filler is added. At this checkpoint the responsibilities are
substantial and independently executable, so no planning blocker is declared.

## Validation and risk decision

Focused validation will cover registry/orchestrator behavior, every security
campaign, negative/mutation/disable outcomes, receipt integrity, CLI/freeze
admission and exact-revision line buckets. A fresh-state generated-input
scenario must be part of the accepting evidence.

The slice introduces no external dependency, port, Docker context, MCP server,
plugin, dynamic import or canonical state-owner transfer. It does add a
freeze-gate entry and subprocess-capable evaluation orchestration, so path,
environment, timeout, process cleanup, secret redaction and clean-state
behavior receive matching high-risk validation. The M3-02 numeric-stage
aggregate and full milestone-exit cleanroom remain owned by their later
documented layers.
