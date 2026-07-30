# P2-S04-01 MaAS operator selector review

- Slice: `P2-S04-01`
- Verdict: `PASS`
- Base commit: `ab02cb0fded584a799cfbecce996b837f99ebf8c`
- Implementation commit: `e5e6b85793c12ad5c0477a937f0a1bda43810f5a`
- Post-implementation fix target: `70fcf3570ee45dad6c6c15f04413b2944852fcc5`
- Mechanism version: `maas_operator_selector_v1`
- Configuration digest: `0cd8bca03e57b77a773038c864f4f5b185c1bc308003c7b66524fe496481dfdf`
- Readiness report digest: `8e7ec1a16249079071eca03869b370675a6a39b4a1c52d8c0a8995937e4838d8`

## Scope and ownership

This slice adds the deterministic MaAS operator-selection layer and stops at the typed selector-to-scheduler input boundary. It does not select physical placement, acquire a worker lease or create a physical execution attempt. `ResourceScheduler` remains the placement owner, `WorkerPoolFoundationRuntime` remains the lease owner, and the existing worker/deployment runtime remains the physical-attempt owner.

The scheduler accepts and validates `OperatorSchedulerInput`, records that the candidate contract was consumed, and deliberately leaves the selected manifest unchanged. Placement influence remains deferred to P2-S04-03.

## Dynamic operator catalog

`OperatorCatalog` builds immutable profiles from live Zyra registries rather than a selector-local enum. It covers registered skills, agent definitions, worker pool manifests, tools and provider/model definitions. Each profile carries stable identity and version, capabilities, input/output contracts, permissions, location/privacy constraints, estimated token/cost/latency, health/capacity, verifier/evidence requirements, cold-start confidence and source-registry provenance.

A registry generation and content digest define the catalog version. Registering a new skill changes the catalog from generation 1 to generation 2; adding a worker and model advances it to generation 4. All new entries are visible without changing selector source. Unknown worker health fails closed instead of being treated as healthy.

## Deterministic selection

The selector encodes query, phase, graph revision and signature, topology roles/capabilities, unresolved obligations, permission/privacy constraints and execution budget with deterministic lexical features. It applies hard filters before ranking:

- required capability and input/output contract
- allowed permission, placement and privacy class
- health and capacity
- verifier and minimum evidence contract
- token, cost, latency and cold-start reserve

Surviving candidates receive eight fixed, inspectable score components. Ordering uses score followed by operator type, identifier and version as a stable tie-break. Identical committed input and catalog snapshots produce identical proposal digests.

Breadth and depth are budget- and obligation-conditioned. A constrained example yields breadth/depth `1/1`, while a high-budget verification example yields `3/4`. The proposal preserves multilayer alternatives and verifier necessity without granting execution authority. Catalog-version drift and revoked operator references are rejected before the scheduler contract can be consumed.

## Readiness and fallback

The new readiness report advances MaAS from the original unavailable precheck to `input_precheck/deterministic_ready`, scoped only to explicit selector validation. The runtime verifies the repository report schema, digest, mechanism status and configured evidence reference before enabling validation.

The branches are explicit:

- `deterministic_ready`: produce a validation-only scheduler input
- `evidence_only`: retain a diagnostic ranking with no scheduler input
- `unavailable`, disabled, missing or unverified report: use the Phase 1 scheduler baseline

Diagnostic input is rejected if forcibly presented to `ResourceScheduler`. The report does not claim `implementation_validated` or `activation_ready`; the proposal-to-placement/lease causal chain is intentionally incomplete until P2-S04-03.

## Source decision and no-training boundary

MaAS `controller.py` and `utils.py` are recorded as `supplementary_implementation` sources. The selected mechanisms are cropped into a Python-to-Python deterministic adaptation: query/operator feature comparison, candidate scoring and multilayer breadth/depth. The target has no runtime dependency on `long-horizon-systems/`.

Training code, policy gradient, textual gradient, learned query parameters, stochastic sampling, provider framework and duplicate agent hosting were not migrated. There are no new datasets, checkpoints, mutable learned parameters, pretrained-model calls, random-selection entry points or machine-learning dependencies.

## Post-implementation self-check

The implementation was frozen before the repository-backed readiness self-check. That check found a missing `ContractHeader` import in the trusted-report loading path. The path already failed closed to the baseline, but it prevented a valid signed report from enabling explicit validation. Commit `70fcf3570ee45dad6c6c15f04413b2944852fcc5` adds the import and a regression test proving that the repository report is accepted while a forged report remains rejected.

## Implementation accounting

From the base through the post-implementation fix target:

- production: 3,012 added lines
- tests: 967 added lines
- configuration: 40 added lines
- runtime assets, generated code, source material, adapter-only and mock/fixture: 0
- production share of implementation additions: 0.74944016

Tests, configuration and evidence are not counted as production.

## Verification

- operator catalog unit suite: `5 passed`
- MaAS selector integration suite: `8 passed`
- scheduler, registry, worker pool, API, readiness rollback and topology adjacent regression: `69 passed`
- Python compile check: passed
- source-language custody: `ok=true`, `violations=0`, 3,012 Python production lines
- no-policy-training audit: passed
- diff checks: passed

One initial adjacent-regression command used stale test directory prefixes and therefore collected no tests. The command was corrected against the repository paths and the full intended suite passed. Ruff is not installed in the repository virtual environment.

## Handoff

P2-S04-01 is complete at selector-level `input_precheck/deterministic_ready`. Normal physical placement remains unchanged, and activation remains closed. P2-S04-02 is the next candidate and is not authorized by this completion.
