# Zyra First-Stage Freeze Evidence Report

- Target commit: `88b88e05aad14e1091f4536bcead02037622408f`
- Scope: M3-S03-01 report/evidence/archive generation
- Final freeze claimed: no; M3-S03-02 owns final critical freeze
- Evidence score: 100/100

## Architecture and canonical ownership

Zyra preserves one owner per state domain, an open-world dynamic topology, immutable commit/recovery, cohesive CodeWorker loops, and a single event/UI projection.

Evidence:

- `generated/algorithm-material.json`
- `generated/internalization-ledger.json`

- algorithm_count: `6`
- source_count: `13`

## Role-aware source custody and debt

Primary and supplementary rows carry owner, target, language, and behavior-test evidence. Inactive roles create no migration quota. OpenClaw remains excluded_forward_only.

Evidence:

- `generated/internalization-ledger.json`

- active_row_count: `41`
- inactive_row_count: `19`
- primary_capability_count: `26`
- row_count: `60`
- source_count: `13`
- supplementary_capability_count: `13`

## Dual-domain sealed live runtime evidence

Case studies originate from new-input live source archives, not replay. Each formal run preserves policy, transition, fault, artifact, verifier, configuration, and outcome receipts. Protected real tier/provider evidence is cross-linked without pretending the M3 case cells made new provider calls.

Evidence:

- `generated/case-studies.json`

- cross-source-research: `4003`
- software-delivery: `2394`
- deployment_evidence_same_run: `True`

## Metrics, baselines, and module ablations

Raw samples, P50/P95 distributions, paired comparisons, repetition counts, and the seven required variants remain checksum-linked.

Evidence:

- `generated/ablation-material.json`
- `docs/reviews/evidence/M3-S02A-02/formal-current-09e99cdc/raw-samples.json`

- raw_sample_count: `1554`
- comparison_count: `444`

## Core algorithms, pseudocode, and complexity

Every pseudocode block is linked to executable symbols and behavior tests, with time, space, and communication complexity.

Evidence:

- `generated/algorithm-material.json`

- dynamic-sparse-topology-routing: `O(W log K + E_delta)`
- low-entropy-structured-communication: `O(B + R log K)`
- distributed-memory-compact-restore: `O(N + Q log I + K log K)`
- neuro-symbolic-action-admission: `O(C + D + P + A)`
- device-edge-cloud-placement: `O(W * (C + M) + W log K)`
- fault-classification-exact-recovery: `O(L + P + W log W)`

## Device-edge-cloud and multi-model compatibility

Placement rows include actual protected receipts, network/privacy classes, credential state, task/model split, failover, and degradation outcomes; labels alone are rejected.

Evidence:

- `generated/compatibility-material.json`

- model_count: `3`
- profile_count: `3`
- provider_count: `3`
- receipt_digest: `c66249abdf32b5ac0c482c6038b597473d59bacea62d6f794a559702ab3dd5ae`
- schema: `zyra.first-stage-compatibility-verification/v1`
- split_count: `42`
- valid: `True`

## Application value and assumption boundaries

The two live domains expose measured runtime, cost, throughput, success, autonomy, and recovery facts. Labor or economic savings remain explicit adopter-supplied projections.

Evidence:

- `generated/application-value.json`

- application_count: `2`
- receipt_digest: `771c22563cb7b4286801356ebc189f05adc1f337f3cd55fce95825c13d8092ea`
- risk_count: `5`
- schema: `zyra.first-stage-application-value-verification/v1`
- unsupported_savings_claims: `0`
- valid: `True`

## LangGraph forward correction boundary

Only checkpoint identity/lineage, pending versus committed writes, side-effect fencing, and exact-resume semantics remain active. Open-world topology and immutable commit are Zyra-owned.

Evidence:

- `generated/langgraph-correction-matrix.json`

- active_semantic_count: `4`
- inactive_subsystem_count: `4`
- receipt_digest: `3fb70d21b70a7b4d9ddc85b46f5384db504b71d449451c22e9a6c8e600400d2d`
- schema: `zyra.langgraph-forward-correction-verification/v1`
- valid: `True`
- zyra_owned_count: `5`

## Causal trajectory, archive, and projection replay

The archive binds every member through SHA-256 and an ordered hash chain. Replay verifies projection consistency only and never redefines live task success.

Evidence:

- `first-stage-evidence.zip!/manifest.json`
- `archive-verification.json`
- `replay-verification.json`

- archive_manifest_digest: `None`
- replay_task_success_recomputed: `None`

## Remaining debt and non-blocking inactive sources

Inactive source rows are not defects by themselves. Only missing retained capability, active owner, main-path behavior, or first-stage evidence is a freeze blocker.

Evidence:

- `generated/internalization-ledger.json`
- `generated/compatibility-material.json`

- inactive_source_rows: `19`
- compatibility_valid: `True`

## Second-stage inputs

M3-S03-01 records evidence generation only. M3-S03-02 must classify first-stage blockers, CI hardening, and pure optimization separately and cannot downgrade a main-path internalization defect.

Evidence:

- `generated/internalization-ledger.json`

- first_stage_blockers: `0`
- final_classification_owner: `M3-S03-02`

## Integrity

- Input set: `41b69dbc5279238307ae803edd9ee1cfa09d73f303709c2f153a8b304135740a`
- Evidence index: `7189425ec6cabb38dc3b7069e2602efdc6a47735acbb5df40cd5fef11168cdc6`
- Internalization ledger: `b9393c13c43b356e48474bb69fab9d556d6d98a5b5a7879ad2773f00e71ce3f5`
- Report digest: `a13b259f5964dbcc44877de43aebceee2ca78b91eeefff1f026a1f6e01a7579b`
