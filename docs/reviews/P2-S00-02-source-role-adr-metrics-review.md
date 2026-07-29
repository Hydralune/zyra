# P2-S00-02 source-role ADR, metrics, and activation-gate review

## Verdict

P2-S00-02 passes its incremental implementation, behavior-test,
fail-closed, and evidence boundary at implementation commit
`7cac95958c74aa96b4ec3d0082f6cf5108b57c95`.

The slice is bound to
`P2_BASE_COMMIT=e207b46ca690171139a718b8b85d808cb5a79c1e`.
It does not activate `phase2_strongest_v1`, migrate LoopX, or implement the
four paper mechanisms. P2-S00-03 remains responsible for the first real
mechanism input/evidence readiness audit.

## Delivered contracts

Seven mutually referenced ADRs are frozen under
`docs/architecture/phase2/adr/`:

1. P2-ADR-001: canonical owner and proposal-only boundary;
2. P2-ADR-002: LoopX pin, dual install profiles, workspace state, upgrade and rollback;
3. P2-ADR-003: the fixed strongest mechanism composition;
4. P2-ADR-004: baseline/default/validation/diagnostic/retired lifecycle, activation, fallback and rollback;
5. P2-ADR-005: minimal sufficient validation, metrics, diagnostic trigger and anti-tuning rule;
6. P2-ADR-006: required/optional decision-time input, effect, determinism and readiness;
7. P2-ADR-007: continuity, mechanism/model-assisted-to-symbolic, and physical dispatch evidence.

`config/phase2/contract-digests.json` binds all seven ADRs and the three
registries to the same P2 base commit. The runtime validator recomputes every
digest.

The machine-readable registries are:

- `source-roles.yaml`: 5 capability domains and 9 exact source entries;
- `state-owners.yaml`: 13 canonical domains in a Phase 2 constraint overlay;
- `activation-gates.yaml`: 5 lifecycle profiles, 5 mechanism readiness
  entries, 10 evidence contracts, 33 metrics, and 23 hard gates.

The `.yaml` files intentionally use the JSON-compatible YAML subset. The
runtime therefore needs no new YAML dependency and parses the files with the
Python standard library.

## Source role and language custody

| Domain | Primary | Supplementary | Migration |
|---|---|---|---|
| long-horizon control | Zyra baseline | LoopX 0.2.4 at `8e798437...` | Python pinned package integration |
| topology proposal | ARG-Designer at `c4de0931...` | CARD at `d5d1f682...`; AgentPrune at `c544dd6a...` | Python cropped same-language deterministic adaptation |
| operator selection | Zyra ResourceScheduler | MaAS at `987f3c1b...` | Python cropped same-language deterministic adaptation |
| readiness | Zyra only | none | Python Zyra-owned validator/report contract |
| continuity/symbolic/dispatch evidence | Zyra only | none | existing Python/TypeScript owners |

Every active entry records source/target languages, exact commit, migration
mode, analysis reference, target path, proposal-only boundary, and canonical
owner reference. OpenClaw has no role row and remains
`excluded_forward_only`.

The source-to-target plan excludes the paper training controllers, training
datasets/checkpoints, benchmark runtimes, provider frameworks, unsafe
executors, and duplicate Agent hosts. LoopX is a complete runtime asset rather
than deep-internalization line credit.

## State owner impact

No canonical owner moved. The Phase 2 owner registry checksum-binds the frozen
first-stage `state_owner_evidence_catalog.json`; it does not edit that protected
catalog or create a new state store.

Validation confirms:

- 13 domains and 13 single owner declarations;
- zero ARG/CARD/AgentPrune/MaAS canonical owners;
- GraphStateCustody remains graph owner;
- ResourceScheduler remains placement and execution-budget owner;
- existing task/session, permission, memory, recovery, lease, artifact,
  event, and provider owners remain selected;
- LoopX owns only `loopx_private_control`.

MaAS is explicitly forbidden from scheduler, lease, and execution-budget
ownership. LoopX is explicitly forbidden from Zyra task, graph, permission,
lease, placement, memory-fact, and execution-budget ownership.

## Activation and readiness

All five new mechanisms are intentionally recorded as `unavailable` at the
`input_precheck` stage with a pending P2-S00-03 report reference. This is not a
negative readiness finding; it is the truthful pre-audit state.

The current default remains `phase1_deterministic_baseline`.
`phase2_strongest_v1` is `validation / blocked_pending_readiness`.
An explicit strongest activation request exits 1 with
`strongest-profile-readiness-failed` and lists LoopX, ARG, CARD, AgentPrune,
and MaAS as blockers.

`evidence_only` and `unavailable` mutation tests prove they cannot enter the
strongest default. Missing registries, semantically valid file tampering,
owner-catalog tampering, duplicate domains, source-role overflow, training
entries, incomplete metric semantics, and frozen-threshold changes all fail
closed.

## Requirement, metric, and evidence trace

Every hard gate has:

- a unit, direction and threshold;
- an evidence unit and independent sample unit;
- `empty_sample_semantics=fail_closed`;
- at least one `REQ-*` or `SCORE-*` identity;
- at least one evidence contract.

The hard set covers sealed completion, zero human intervention, 2,000
effective transitions, graph custody, invalid proposal rejection,
idempotency, early exit, permission/privacy, owner uniqueness, cleanroom
source dependency, validation-before-spend, no-policy-training, readiness,
memory continuity, symbolic safety, local/edge/cloud receipts, physical
causality, and condition switching.

The optimization set freezes the communication, byte, utilization,
token/cost, wall-time, churn, oscillation, adaptation, LoopX restart, and
role/operator cold-start targets without claiming those targets are already
achieved.

## Verification

At the implementation commit:

- evaluation contract tests: 16 passed;
- owner registry tests: 12 passed;
- combined workspace-basetemp rerun: 28 passed in 0.40 seconds;
- policy contract verifier: `valid=true`;
- strongest activation negative command: expected exit 1;
- internalization ledger: `audit_ok=true`, zero errors, zero blockers, zero
  missing source repositories;
- compileall: passed;
- `git diff --check`: passed.

The first exact-commit combined pytest command produced 26 passes and two
setup errors because pytest could not enumerate the user temporary directory
on Windows. The identical suite passed with a newly created workspace-local
`--basetemp` and cache provider disabled. This environment diagnostic is
retained in the machine summary rather than reported as a repository test
failure.

The machine verification receipt is
`docs/reviews/evidence/P2-S00-02/policy-contract-verification.json`, SHA-256
`222eeea98acb0baf686517bb799863db40a840587f8496d763e77c47a77d6904`.

## Effective change buckets

Implementation-commit raw additions:

| Bucket | Lines | Treatment |
|---|---:|---|
| production validator and script | 1,280 | production/scripts |
| tests | 348 | test |
| registry/digest configuration | 1,132 | data |
| ADRs | 491 | docs |
| runtime assets | 0 | no credit |
| generated | 0 | no credit |
| adapter-only | 0 | no credit |
| mock/fixture | 0 | no credit |

The validator is the non-zero production responsibility. ADR, config, test,
and evidence volume is not counted as production implementation.

## Risk and deferred validation

This slice does not change a canonical owner, public persistence schema,
global default runtime, dependency, process, port, provider, or physical
dispatch path. High-risk escalation was not triggered.

Full repository, cleanroom, real LoopX install, live mechanism semantic
effect, long-run, and physical local/edge/cloud validation are intentionally
not run here. They belong to the later implementation, parent-close, and
P2-06 gates identified by the ADRs. No current hard gate is waived.

## Repository boundary

The root planning documents and `docs/milestones/execution-state.yaml` live
outside the Zyra Git repository. The Zyra evidence commit can record this
review but cannot automatically commit a root status update. The final slice
handoff must report that boundary explicitly.

## Handoff to P2-S00-03

P2-S00-03 must consume:

- the ADR and registry digests from `contract-digests.json`;
- `Phase2PolicyContractBundle` and `parse_mechanism_status`;
- the frozen readiness stages/statuses;
- each mechanism's pending input/effect/receipt/fallback contract;
- the immutable P2 baseline manifest and evidence inventory.

It must replace the pending readiness references with real
`input_precheck` reports. It must not activate strongest, generate training
data, or lower a frozen gate to obtain a favorable status.
