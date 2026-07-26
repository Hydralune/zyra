# M3-S01A-02 State-owner / reachability / evidence audit decision

Date: 2026-07-26

## 1. Frozen implementation boundary

- Slice: `M3-S01A-02`
- Baseline commit:
  `ce799c7fe02d1f5fc1832bbbff1e76b5a73e8caa`
- Slice minimum effective production code: 4,500 lines
- Parent baseline commit:
  `1d19ea39a8313091dcfbc00f78c79fdfdeab7cf4`
- Parent minimum effective production code: 9,000 lines
- Migration mode: `audit_only_no_runtime_migration`
- Canonical runtime owner transfer: none
- Incompatible persistence, transaction, lease, idempotency, checkpoint, or
  restore change: none
- Global permission, scheduler, recovery, compact, provider, or fallback policy
  change: none
- New external dependency, service, subprocess, port, MCP server, plugin,
  Docker runtime, dynamic installer, binary, or opaque bundle: none

The product responsibility of this slice is a release/freeze auditor that joins
state-owner custody, executable default-entry reachability, event-to-mutation
causality, source/opaque-risk evidence, effective-line buckets, and competition
requirement evidence. Its findings, receipts, graphs, and work queues are
derived evidence. They do not become canonical task or runtime state.

The protected M3-S01A-01 source-role/dependency/custody implementation remains
the sole owner of source-role, dependency, process, package-annotation, NOTICE,
ledger, and upstream-boundary findings. This slice consumes its result through
a typed read-only bridge and must not duplicate or replace that owner.

## 2. Source and language decision

This slice migrates no upstream runtime code. The Zyra-owned auditor is Python.
Each audited language is represented independently; no input is labelled
`mixed` or `unknown`.

| Audited input | Slice role | Source language / format | Target runtime | Target language | Migration mode | Runtime owner |
| --- | --- | --- | --- | --- | --- | --- |
| Python owners, stores, write paths, recovery paths, API and worker entries | audit input | Python | `zyra_evaluation.freeze_audit` | Python | `audit_only_no_runtime_migration` | unchanged M1/M2 owner |
| TypeScript runtime, provider, permission, MCP, plugin and event modules | audit input | TypeScript | same audit runtime | Python | `audit_only_no_runtime_migration` | unchanged M1/M2 owner |
| TSX Web projection, controls, viewers and entry composition | audit input | TSX | same audit runtime | Python | `audit_only_no_runtime_migration` | `CanonicalProjectionStore` remains sole frontend truth |
| JavaScript build/runtime entries, when present | audit input | JavaScript | same audit runtime | Python | `audit_only_no_runtime_migration` | unchanged package owner |
| Rust/native build and binary declarations, when present | audit input | Rust/native metadata | same audit runtime | Python | `audit_only_no_runtime_migration` | unchanged selected native owner |
| owner/evidence catalogs and manifests | audit authority input | JSON, TOML, YAML and text | parsed validation state | Python | `audit_only_no_runtime_migration` | version-controlled catalog is evidence, not runtime truth |
| M3-S01A-01 result and work queue | typed read-only audit input | JSON receipt plus Python result | source-risk bridge | Python | `audit_only_no_runtime_migration` | M3-S01A-01 remains source/dependency audit owner |
| competition requirement matrix and frozen evidence | audit authority input | Markdown and JSON | requirement evidence graph | Python | `audit_only_no_runtime_migration` | requirement matrix and canonical runtime evidence retain authority |

No upstream repository receives a new primary or supplementary implementation
role. `claude-code-best`, OpenCode, browser-use, OpenHands, AgentScope, Agent
Framework, Hermes, LangGraph and Oh My Pi are audited only under their already
frozen roles. LangGraph remains limited to exact-resume/checkpoint semantics.
OpenClaw remains `excluded_forward_only`; the deleted repository is not read,
restored, compared, imported, or assigned a forward role.

## 3. State custody and uniqueness contract

The audited catalog must contain one and only one authoritative record for each
required state domain:

1. task/session and canonical event projector;
2. permission decision, pending request, exact permit, and sealed denial;
3. memory/index/curator/compact state;
4. scheduler, recovery plan, route, worker lease, and fault state;
5. artifact bytes, task membership, revision, and mutation receipts;
6. worker route/attempt/lease;
7. compact archive, restore checkpoint, and post-compact context;
8. dynamic graph topology plus narrow exact-resume checkpoint state;
9. provider catalog, credential metadata, attempt and failover;
10. MCP capability/auth/elicitation and plugin/skill registry;
11. workspace/sandbox gateway grant, busy ownership and cancellation;
12. terminal/browser physical session state and disposable Web projections.

Each record explicitly names the authoritative owner, durable store, write
path, permitted projections and caches, checkpoint/recovery path, default
entries, mutation/event links, tests, and disable probes. A projection, replay
cache, browser store, event log, test fixture, health response, demo, ledger, or
fallback cannot be promoted to a second owner.

The auditor fails closed for:

- duplicate owners or duplicate write custody for one state family;
- a projection/cache/fallback claiming canonical writes;
- a missing durable store, write path, recovery path, test or disable probe;
- provider/catalog/credential/failover, session/projector, plugin registry,
  graph checkpoint, or gateway busy ownership ambiguity;
- a catalog owner symbol that is missing, non-executable, or contradicted by
  reachable production writers.

## 4. Default reachability and semantic-effect contract

The reachability engine builds a multi-language module/symbol/call graph from
real product entrypoints. Roots include packaged CLI commands, API route
handlers, Web bootstrap/composition, and worker execution entries. A state
owner passes only when a root reaches its owner and write path without relying
solely on tests, fixtures, examples, demos, health checks, ledgers, source maps,
reports, import smoke tests, or replay-only artifacts.

The causality engine verifies:

```text
default entry
  -> owner/admission
  -> canonical event or receipt
  -> semantic mutation/effect
  -> durable state/artifact/metric
  -> recovery or verification evidence
```

An event without a verified mutation target is a fake-causation blocker.
Mutation without an event/receipt binding is an evidence-gap blocker. A
fallback may not take over when the selected owner is disabled.

The disable matrix must prove that disconnecting the selected owner, entry
edge, causality rule, source-risk bridge, requirement binding, or effective-line
classifier causes the corresponding valid case to fail or the matching
negative case to survive. Tests must cover duplicate owner, projection as
owner, dead production code, fake event causation, fallback takeover,
data-as-code/generated/source-pool line inflation, and a valid clean case.

## 5. Product module plan

The minimum is assigned only to executable audit behavior:

- `freeze_audit/model.py`: strict identities, typed findings, owner/evidence
  records, receipts, deterministic digests and policy switches;
- `freeze_audit/catalog.py`: state-custody and requirement-evidence parsing,
  normalization, cross-record validation and authority rules;
- `freeze_audit/python_graph.py`: Python AST module, symbol, call, write,
  route, event and entrypoint analysis;
- `freeze_audit/script_graph.py`: TypeScript/TSX/JavaScript lexical module,
  symbol, call, write, event and Web/worker entrypoint analysis;
- `freeze_audit/reachability.py`: typed cross-language graph construction,
  root classification, shortest evidence paths, dead/default-only detection and
  fallback analysis;
- `freeze_audit/ownership.py`: owner uniqueness, store/write/projection/cache/
  checkpoint/recovery/test/disable and alternate-writer validation;
- `freeze_audit/causality.py`: event/receipt-to-mutation/effect/artifact/metric
  verification and false-causation detection;
- `freeze_audit/source_bridge.py`: read-only M3-S01A-01 semantic-port,
  similarity, opaque/binary/archive/download and root-source risk admission;
- `freeze_audit/lines.py`: Git interval collection plus production, test,
  generated, data, docs, vendor-like, adapter-only, mock/fixture and runtime
  asset buckets with language-aware executable-line classification;
- `freeze_audit/requirements.py`: requirement-to-owner/default/live/event/
  mutation/artifact/metric/test/commit/config/M3-owner evidence validation;
- `freeze_audit/engine.py`: deterministic pipeline, policy, stable downstream
  inputs and checksum-bound receipt;
- `freeze_audit/cli.py`: packaged inventory/candidate/freeze CLI;
- `scripts/audit_state_owner_evidence.py`: non-counted repository launcher.

The state and requirement catalogs, generated receipts, reports, work queues,
tests, fixtures, mocks, schemas/DTO-only declarations, documentation, exports,
thin launchers, and source maps receive zero effective production-line credit.
Large files and files with more than 20% of credited lines are individually
reviewed. A line-count shortfall cannot be repaired with findings, generated
tables, wrappers, launchers, repetitive validators, or unused framework code.

## 6. Commit and validation boundary

- `baseline_commit` is the commit named above.
- This decision is committed before production implementation.
- `implementation_commit` will freeze code and real behavior tests.
- Effective-line `numstat` and per-file classification use only
  `baseline_commit..implementation_commit`.
- `evidence_commit` will contain the critical self-review and machine-readable
  verification records; evidence/docs are never added to the effective count.

This parent-final slice runs focused unit and real-repository integration tests,
mutation-rule disable tests, CLI inventory/candidate behavior, default
entry-to-owner and event-to-mutation traces, owner disconnect behavior,
source-repository disconnect checks, adjacent M3-S01A-01 regression, direct
parent effective-line recomputation, compile and diff checks. It does not claim
the M3-01 numeric-stage cleanroom or full repository regression that remains
owned by the final M3-01 sibling/aggregate unless this implementation triggers a
matching high-risk escalation.
