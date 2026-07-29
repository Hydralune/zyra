# P2-S00-03 algorithm and data feasibility review

## Verdict

P2-S00-03 passes its input-precheck, fail-closed, reproducibility, behavior-test,
and evidence boundary at implementation commit
`b2e0823427a3d35b4520100362ba5e21f9e13a0c`.

The result is deliberately not a claim that the four paper mechanisms are
implemented or usable. The formal frozen-evidence audit assigns all four
mechanisms `unavailable` at `input_precheck`:

| Mechanism | Required input coverage | Status |
|---|---:|---|
| ARG-Designer | 2/6 (`0.333333`) | `unavailable` |
| CARD | 1/9 (`0.111111`) | `unavailable` |
| AgentPrune | 1/8 (`0.125000`) | `unavailable` |
| MaAS | 4/9 (`0.444444`) | `unavailable` |

This is the correct completion result for the slice: the evidence gaps are now
machine readable, the runtime resolver enforces them, and
`phase2_strongest_v1` remains blocked.

## Delivered contracts and runtime surfaces

`config/phase2/mechanism-readiness.json` defines a distinct input, semantic
effect, causal receipt, failure path, scenario, fallback, instrumentation, and
allowed-follow-up contract for ARG, CARD, AgentPrune, and MaAS. All fields bind
to the frozen Phase 2 canonical-owner overlay; none of the four mechanisms
receives canonical graph, scheduler, lease, permission, memory, or artifact
ownership.

`evidence_index.py` constructs a read-only evidence index from the frozen Phase
2 baseline manifest. It verifies all manifest references and each source
archive member digest, de-duplicates by independent source run, preserves
source-run provenance, and labels event/sample volume as evidence volume only.

`mechanism_readiness.py` enforces:

- required and optional decision-time field contracts;
- owner, missingness, freshness, confidence, corruption, and conflict checks;
- strict future-information exclusion;
- deterministic canonical input snapshots and replay digests;
- scenario, failure-path, negative, and proposal-to-outcome causal coverage;
- a no-policy-training source/config audit;
- report schema, digest, registry binding, and fail-closed resolution.

The CLI audit produces
`docs/reviews/phase2/MechanismEvidenceReadinessReport.json`. Its content-derived
report digest is
`82ad9a84a55506223fce2133168b7df2f0ae487aa7288fe5ede5c58869c1bbde`;
the file SHA-256 is
`222955f643597a664201e845b9a1eecac1eb9039b6f6d0e47c4b57ae095f36d0`.
Repeated formal runs over the same implementation and frozen evidence produce
the same report digest and byte-identical file.

## Frozen evidence custody

The audit validates the immutable baseline manifest digest
`85fa3a4a05d3ce7906e7af8507b3e413b26a5c53042bf0073d8f3039b9e5db37`
and all 24 references. The derived read-only evidence-index digest is
`6fd591b5b0974b18877d082050357f17fc73f53d98a23cc9b39c62f76180a5b1`.

The evidence inventory contains:

- 6 independent source runs across the two frozen domains;
- 42 derived formal cells/run receipts, which do not increase independent
  readiness sample count;
- 19,191 canonical events;
- 1,554 raw metric samples;
- zero training samples.

Archive bytes are read and verified in place. The slice creates no copied
dataset, labels, holdout, probe, checkpoint, learned parameter, or training
entry point.

## Per-mechanism findings

ARG has usable decision-time permission and budget inputs. Unresolved
obligations, a capability registry, an immutable graph snapshot, and a memory
continuity verdict are absent or occur outside the permitted decision-time
window.

CARD has the ARG base-graph signal but lacks decision-time worker/provider
health, load/capacity, latency, cost, privacy, physical location, freshness,
and confidence inputs. Its required privacy scenario is also absent.

AgentPrune has spatial edges but lacks temporal edges, earlier-window delivery,
byte/token cost, redundancy, utilization, critical-path, and verifier-outcome
inputs.

MaAS has committed topology, permission, budget, and verifier-contract signals,
but lacks an operator catalog, capability registry, health, cost/latency, and
unresolved-obligation inputs. Its required privacy scenario is absent.

All four deterministic input snapshot replays match. Their mechanism-specific
proposal-to-symbolic-verdict-to-outcome causal completeness is `0.0`, because
the frozen first-stage evidence predates these Phase 2 mechanism proposals.
The baseline causal chains remain complete, but are not reused as proof of a
new mechanism's semantic effect.

Observed frozen scenarios include normal, requirement change, fault/degraded,
placement, and recovery. Observed failure signals include exception, invalid,
unavailable, reroute, and recovery. The missing projector-reject and
mechanism-specific disconnect receipts remain explicit gaps.

## Resolver and activation enforcement

The resolver maps only `activation_ready/deterministic_ready` to a default
mechanism path. Earlier deterministic readiness is validation-only;
`evidence_only` is diagnostic-only; missing, corrupt, unknown, stale, or
`unavailable` reports select the declared baseline.

The default registry-bound resolution selects a baseline for all four
mechanisms. It validates the report reference, report digest, activation
registry status/stage, baseline file, and complete Phase 2 contract bundle.
Synthetic explicit reports remain available only to tests of individual
resolver branches.

`activation-gates.yaml` now binds ARG, CARD, AgentPrune, and MaAS to the formal
report while retaining `unavailable/input_precheck`. LoopX remains independently
`unavailable` pending P2-01. The frozen activation-gate digest is
`c87b351fdd7bf76011bcd9285667772327eff073928c6090c2ecc8c649f1f238`.
An explicit strongest activation request exits 1 with
`strongest-profile-readiness-failed` and lists LoopX plus all four audited
mechanisms as blockers.

## Verification

At the final implementation commit and report binding:

- readiness unit tests: 9 passed;
- readiness enforcement integration tests: 9 passed;
- combined readiness and adjacent Phase 2 contract/owner suite: 46 passed in
  2.49 seconds;
- formal readiness audit: `valid=true`, four `unavailable` statuses,
  `training_sample_count=0`;
- Phase 2 baseline verifier: `valid=true`, 24 references, zero findings;
- policy contract verifier: `valid=true`, 13 single-owner domains and zero
  proposal owners;
- strongest activation negative command: expected exit 1;
- internalization ledger: `ok=true`, zero errors, zero blockers, and zero
  missing source repositories;
- compileall: passed;
- `git diff --check`: passed.

The policy-contract receipt is
`docs/reviews/evidence/P2-S00-03/policy-contract-verification.json`, SHA-256
`67c9caa65ab9d064ef7cd1869bb8db265cdc73a9fcc528b012b26af2db11d7c7`.

## Effective change buckets

Implementation-commit raw additions:

| Bucket | Lines | Treatment |
|---|---:|---|
| production validator, index, exports, and script | 3,186 | production/scripts |
| tests | 530 | test |
| readiness contract configuration | 632 | data |
| runtime assets | 0 | no credit |
| generated | 0 | no credit |
| adapter-only | 0 | no credit |
| mock/fixture | 0 | no credit |

The 2,443-line formal report and verification receipts are generated
data/evidence and receive no production credit. The tests use small synthetic
objects only to exercise mutations and resolver branches; the formal verdict
uses the frozen baseline evidence.

## Parent close and risk

P2-00 can close after this slice: the baseline manifest, ADR/owner/source-role
contracts, activation registry, and readiness report share the same
`P2_BASE_COMMIT=e207b46ca690171139a718b8b85d808cb5a79c1e`; their digests validate;
source roles do not conflict with state owners; and every follow-up mechanism
now has a machine-readable status, prohibited action set, gap list, and
fallback.

Insufficient evidence never triggers training. It produces an explicit
diagnostic or baseline path. No frozen threshold was lowered, no first-stage
history was rewritten, no dependency was added, and no canonical owner,
default runtime, provider, process, port, lease, or physical-dispatch path was
changed.

P2-01 and P2-02 are now eligible for explicit slice selection, but neither is
started by this completion. Later mechanism slices must add their missing
decision-time instrumentation and causal receipts before seeking
`implementation_validated` or `activation_ready`.

## Repository boundary

The root planning documents and `docs/milestones/execution-state.yaml` live
outside the Zyra Git repository. The Zyra evidence commit cannot automatically
commit those root status updates; the final handoff must report that boundary.
