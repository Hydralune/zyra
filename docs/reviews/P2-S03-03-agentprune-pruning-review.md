# P2-S03-03 AgentPrune pruning review

- Slice: `P2-S03-03`
- Verdict: `PASS`
- Base commit: `321e1f7e5fbd65c6994c7e0ca9d7976fff3853d1`
- Implementation commit: `21b13bb33906a2c56c074421cf7423834808c400`
- Mechanism: `agentprune_spatiotemporal_v1`
- Configuration digest: `7af6228f24f738360f329cc6487b7bdb83a7dfb4d322195edf4e3afacd0a9e28`
- Readiness report digest: `6c6626536514289b2389e7d9ccffdd0ff8f730593785925cff597db2280967cc`

## Scope and ownership

This slice introduces one deterministic AgentPrune adaptation after the CARD residual. It consumes immutable candidate edges plus prior real communication outcomes, emits separate spatial and temporal keep/drop masks, and can gate calls into an injected existing communication owner for isolated validation.

AgentPrune does not mutate the canonical graph. Its topology output is a `TopologyProposalArtifact` containing `REMOVE_EDGE` operations. `GraphStateCustody`, the unified projector, the communication runtime, the event sink, permission, lease and scheduler owners remain unchanged. The integration proof keeps the canonical graph revision at `0 -> 0`.

## Outcome aggregation and deterministic pruning

The implementation aggregates prior delivery receipts per edge, including delivered messages, bytes, tokens, cost, evidence utilization, artifact contribution, verifier outcome, duplicates, failures, retries, malicious-source signals and permission denial. Spatial and temporal candidates remain separate; a temporal checkpoint/restore edge is not relabelled as same-round spatial communication.

The optimizer uses fixed, versioned weights and stable ordering. It has no learned logits, policy gradient, sampling or mutable learned parameters. Critical-path, unique-evidence, verifier, unresolved-obligation, recovery and continuity edges are hard protected. A budget that cannot retain protected edges fails closed.

A mask is frozen by run and mechanism epoch. The same epoch and input reuse the same digest; a changed budget or candidate input in that epoch fails with `agentprune_epoch_mask_drift`. A new recovery epoch permits one deterministic recomputation.

## Behavioral evidence

The representative prior window contains five messages, 480 bytes, 84 tokens and USD 0.041. The mask retains the critical spatial edge and temporal recovery edge, and drops a duplicate low-value edge plus a malicious-source edge. Expected savings from this historical window are explicitly labelled counterfactual.

The paired real-delivery run uses identical payloads and the same injected delivery owner:

| Mode | Messages | Bytes | Tokens | Cost (USD) | Verifier | Artifact |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| AgentPrune enabled | 2 | 43 | 30 | 0.01 | passed | complete |
| AgentPrune disabled | 4 | 81 | 60 | 0.02 | passed | complete |

The observed reduction is two delivered messages, 38 bytes, 30 tokens and USD 0.01. Both paths pass the verifier and produce a complete artifact. A forced verifier failure sets `efficiency_gain_valid=false`, so task failure cannot be reported as an efficiency gain.

## Readiness and fallback

`arg_designer`, `card` and `agentprune` are `deterministic_ready` at `implementation_validated`. AgentPrune resolves to validation mode with a real delivery mask, while `activation_allowed=false` and canonical mutation remains forbidden until the P2-S03-04 composer closes the unified path.

`evidence_only` computes a diagnostic would-prune mask but still delivers all messages. `unavailable` and explicit disable use the Phase 1 targeted-communication baseline. Unknown, missing, corrupt or digest-mismatched readiness reports fail closed to `unavailable`.

## Source custody and implementation accounting

The cropped AgentPrune mechanism remains Python-to-Python. The source graph executor, provider runtime, registry, learned logits, stochastic masks and direct graph mutation were rejected. Source-language custody passes with 3,090 production Python lines and no violations.

Implementation buckets:

- production: 3,090 added lines
- tests: 1,501 added lines
- configuration: 36 added lines
- runtime assets, generated code, source pool and adapter-only: 0
- training samples, datasets, checkpoints and runtime training entry points: 0

## Verification

- focused AgentPrune unit and integration: `8 passed`
- focused plus ARG/CARD/projector adjacent regression: `57 passed`
- owner registry, policy registry and diagnostic rollback: `41 passed`
- Python compile check: passed
- source-language custody: passed
- release-path and no-policy-training audits: passed
- staged diff check: passed

The first focused invocation used an inaccessible user-level pytest temp directory and produced four setup errors before test execution. Re-running the identical suite with a workspace-local `--basetemp` passed all eight tests; this is recorded as an environment limitation, not a product failure. Ruff was not installed in the repository environment.

## Handoff

P2-S03-03 is complete. P2-S03-04 is the sole next slice candidate and remains unauthorized. That slice owns the unified ARG -> CARD -> AgentPrune composer and any default-path activation decision.
