# P2-S06-01 strongest profile freeze and low-cost preflight review

## Decision

`PASS` for admission to `P2-S06-02`.

The final immutable preflight is
`preflight_deafc65877387ab74bbef93c`, bound to runner/config implementation
commit `bf8591914065f229970508470d967d90c8312094`. It completed every
preflight hard gate, promoted ARG, CARD, AgentPrune, and MaAS to
`activation_ready + deterministic_ready`, and returned
`admit_to_P2-S06-02`.

This decision does not activate `phase2_strongest_v1` as the normal default.
The resolver was `phase1_deterministic_baseline` both before and after the
preflight. A later explicit activation transition remains required.

## Frozen identity

| Field | Value |
| --- | --- |
| Slice base / P2 eval base | `ff460ab7c3c138ad021b64edc303fc282effb995` |
| Implementation commit | `bf8591914065f229970508470d967d90c8312094` |
| Preflight id | `preflight_deafc65877387ab74bbef93c` |
| Manifest digest | `602a7c9bc3be65ac8557848350c2ed22a97ef28f44e2df7293bf06210a033ce1` |
| Preflight report digest | `a14756cbde482b0c71ddf52172c7d22faba5ad37bb4fbc769a46a32561067dd2` |
| Readiness report digest | `8818445291a7e0c23fe6dbc92ae8d9201ded5de797ec9e2959ecb23a3accb0f7` |
| Admission report digest | `c1e7faa2f5cfedb9dbfbdd90d876b81381a4096d95ff5ac5f13c927ddccd3d39` |
| Inventory digest | `a6986f75606c087190f3280604ce8294b03da43fa46d4f1bd0cab894e1ef2e68` |

The frozen identity covers the only allowed mechanism chain, two distinct
domain tasks and verifiers, seed list, budgets, provider/model/hardware
profile, failure schedule, metric/activation versions, readiness report
bindings, supporting LoopX/physical-dispatch evidence, command probes, and
all prohibited operations. Changing any of these values changes the
preflight id.

## Gate and receipt result

- All 15 ordered hard gates passed. Completion and safety are evaluated
  before efficiency observations.
- The raw set contains 30 retained receipts: deterministic proposal,
  residual, mask, operator-selection and receipt digests for both tasks and
  both seeds; missing/stale/corrupt fail-closed receipts; diagnostic
  zero-influence receipt; Phase 1 read-only replay; no-training audit; and
  four real command-probe receipts.
- Failed receipt count, warning count, and final outlier count are all zero.
  The report still retains the full failure/outlier schema and mutation tests
  prove that dropped receipts fail the gate.
- Diagnostic influence is zero for graph mutation, route, lease, and side
  effect.
- Training sample count is zero. Transition count is labelled evidence
  volume only and is not used as training data or statistical confidence.
- No external provider smoke was required. Existing real physical-dispatch
  evidence was digest-revalidated, while the new runtime probe exercised a
  real local scheduler/lease/tool/artifact/verifier path.

## Runtime validation

The immutable command receipts record:

| Probe | Result | Scope |
| --- | --- | --- |
| `strongest_runtime` | 4 passed | deterministic ARG/CARD/AgentPrune outputs, projector/delta/canonical commit/replay, MaAS/scheduler/lease/tool/artifact/verifier, worker fault reroute |
| `continuity_restart` | 2 passed | compact/restore, process restart, node replacement, checkpoint continuity |
| `loopx_restart_outbox` | 2 passed | apply-before-ack restart replay and durable outbox admission |
| `phase1_read_only_replay` | 1 passed | frozen Phase 1 reference replay without a live-improvement claim |

Focused unit validation passed 10 tests, including mutation coverage for
readiness enforcement, determinism-check presence, no-training audit,
failure retention, warning retention, immutable output, and current-HEAD
binding. The adjacent readiness/activation regression selection passed
46 tests. Phase 2 frozen contract validation remained valid and continued
to report the normal strongest activation gate as closed.

All report digests, receipt-set digest, inventory digest, and inventory file
SHA-256 values were independently recomputed after the final run.

## Retained superseded executions

Two earlier immutable outputs remain under the same evidence parent and are
excluded from the final decision:

1. `preflight_c34f22170d1b29f60696e895` completed on `539e6bfb...`,
   but its pytest cache warnings were retained only in raw stdout and not
   promoted into the outlier summary. It is superseded by the warning
   retention fix.
2. `preflight_a26163647ae31a5ef2b9b282` contains a manually supplied
   implementation commit with one mistyped character. It is invalid for
   activation attribution. The runner now fails closed unless the supplied
   implementation commit equals the current Git HEAD.

Neither directory was overwritten or deleted. Only
`preflight_deafc65877387ab74bbef93c` is authoritative.

## Incremental self-review

| Bucket | Added lines | Treatment |
| --- | ---: | --- |
| Production evaluation runtime | 1,582 | manifest validation, deterministic/fail-closed/replay probes, raw retention, readiness/admission reporting |
| Validation script | 153 | immutable CLI runner and current-HEAD binding |
| Test | 403 | unit mutation tests and real integration path |
| Config/data | 270 | frozen strongest-profile manifest |
| Runtime assets / generated algorithm / training data | 0 | none added |

Canonical graph, memory, scheduler, permission, lease, event, artifact, and
LoopX private-state ownership did not move. The preflight owns only its
frozen validation configuration and result index.

## Handoff boundary

`P2-S06-02` may use the final strongest profile and baseline identities above
for two formal sealed long runs. It is only a next candidate; this review
does not authorize it. Default activation remains forbidden until the later
explicit transition and its required sealed evidence.
