# M3-S02A-02 live benchmark final critical self-review

## Verdict

`PASS`

M3-S02A-02 and parent M3-02A are complete.  The formal current-commit
campaign executed and admitted all `42` cells, produced `1,554` raw metric
samples, generated a complete `100/100` report and passed the exact-revision
freeze gate.

The earlier `BLOCKED_FORMAL_LIVE_EXECUTION` review remains a historical
record.  Its product-owner inventory was corrected by the separately
committed resume decision: the product composition already existed under
`apps/api/zyra_api/live_scenario_owners.py` and
`apps/api/zyra_api/scenario_api.py`.  This slice binds that composition to the
formal benchmark without creating a second task, event, artifact, placement,
memory, recovery or checkpoint owner.

## Frozen revisions

- Slice baseline:
  `c01fcacf7cd30c37aa9629aed76a7df5256dad63`
- Initial preimplementation decision:
  `dc44781b4ef2a6c28e14dbccd645687fb25aaaf5`
- Product-owner correction/resume decision:
  `29d79eae3b74360f35625dbbebd9d6b52cb8bfca`
- Final benchmark implementation:
  `95fcf7aeaed5b5ec80fb2f7178b97fbdf8adbeb6`
- Evidence commit:
  the commit containing this review and
  `docs/reviews/evidence/M3-S02A-02/formal-live-95fcf7ae`

An unrelated user-owned commit, `9512c687114ea2d44a291a52e498e80f85f21b6e`,
arrived while the slice was being resumed.  It was preserved.  Its DeepSeek
profile and package-script changes are excluded from this slice's effective
production accounting and from the formal provider-call boundary.

## Product responsibility and main path

The completed main path is:

```text
exact commit + sealed campaign
  -> ProductLiveBenchmarkPort
  -> isolated ScenarioRunnerService source task
  -> Zyra task/event/artifact/worker/recovery/memory/delivery owners
  -> fresh causal archive
  -> seven EvidenceBackedWorkloadRuntime capability vectors
  -> live admission + deterministic domain verifier
  -> raw metrics + paired statistics
  -> report + 100-point index + Merkle manifest
  -> frozen campaign store + M3 freeze gate
```

The implementation adds or completes these Zyra-owned boundaries:

1. `apps/api/zyra_api/live_benchmark_port.py` composes existing product owners,
   starts isolated fresh source tasks and converts their immutable facts into
   benchmark receipts.
2. `live_benchmark/protected_evidence.py` admits only the exact protected M1
   deployment/provider facts under fixed SHA-256 and content digests.
3. `admission.py`, `deployment.py` and `faults.py` enforce six-source/42-cell
   identity, live route evidence, protected-prior non-relabeling, aggregate
   fault coverage and the observable no-recovery failure.
4. `runtime.py`, `metrics.py`, `statistics.py`, `reporting.py`, `integrity.py`
   and `freeze_gate.py` own campaign evaluation, complete sample custody,
   paired statistics, report/index composition and freeze admission.
5. `scenario_runner/research_delivery.py` retains deterministic verifier
   thresholds before ranking source fragments and applies bounded retries only
   to declared transient acquisition failures.
6. `scripts/run_m3_live_source.py` and
   `scripts/run_m3_s02a02_live_benchmark.py` provide reproducible isolated
   product execution and exact-commit evidence generation.
7. `scripts/verify_m3.py` now requires the current formal evidence pointer and
   repeat-verifies stable freeze fields, stored receipt integrity and all
   manifest members.

Removing or unbinding `ProductLiveBenchmarkPort` leaves
`UnboundLiveRunPort`, which fails with `benchmark-live-run-port-unbound`.
Changing a source/archive/run identity, relabeling protected evidence as a
current dispatch, removing a required metric/fault/variant, altering a
manifest member or changing the exact implementation commit causes formal
admission or freeze failure.

## Formal campaign evidence

Evidence root:

`docs/reviews/evidence/M3-S02A-02/formal-live-95fcf7ae`

Observed results:

- domains: `2`;
- variants: `7`;
- repetitions: `3`;
- admitted formal cells: `42/42`;
- fresh product source tasks: `6`;
- software effective transitions per cell: `2,310`;
- research effective transitions per cell: `4,003`;
- minimum/maximum effective transitions: `2,310 / 4,003`;
- raw metric samples: `1,554` (`42 * 37`);
- human interventions: `0`;
- operator interventions: `0`;
- score: `100/100`;
- report digest:
  `df0fb5203ece7a27b4acb4e1241508347b0cc2a4c0b59a8964c05f55c1de3293`;
- evidence-index digest:
  `7c69a994abc38ad4298537db4b63bb5f7326c1001f46bcbe39cfe81ba2782931`;
- manifest digest:
  `7e5e4e10bccc38739f544c7a0739b85c832dc5141ae2a7fe20d021c93564dec9`;
- freeze-admission digest:
  `7dc4368baf10e1a5d1d10b01248df4131379f71d10601a77fbe88257befc5e24`.

The evidence root contains the six fresh causal-archive ZIPs, campaign and
frozen-store receipts, 42 run projections, raw samples, paired statistical
evaluation, protected deployment facts, requirement evidence, benchmark
report, M3-03 index, implementation metadata, manifest, summary and freeze
receipt.  The bundle is approximately `6.3 MB`; archive/report/data output is
not counted as production code.

## No-new-paid-model boundary

No new provider or paid-model request was made.

- current provider/model: `none / none`;
- authenticated provider CLI invoked: `false`;
- external model request made: `false`;
- current route evidence: fresh product-local execution;
- provider/model count `2/2` and device/edge/cloud coverage: exact protected
  M1 facts only;
- protected evidence SHA-256:
  `ce9b659581625d0f7362a0c25221f622e6a237d7bc8703b48d5e952185b7a2da`;
- protected bundle digest:
  `e9954fa94ad5881373b2a11a93e710655439be219663490270d6640f91282e1c`.

No current endpoint, request ID, latency, cost or response was synthesized for
those protected providers.  The formal research sources were newly acquired
public RFC Editor, IANA and MDN documents, not model endpoints.

## Validation

Focused exact-commit validation:

```text
8 passed in 0.38s
```

Adjacent M2 experiment/API, M3 regression and M3 live benchmark validation:

```text
19 passed, 2 pre-existing duplicate-ZIP-member warnings
```

Formal campaign:

```text
42 results admitted
1,554 raw samples complete
6 fresh source archives
100/100 report
freeze gate PASS
wall time 1,328.6 seconds
```

Final integrated M3 entry:

```text
M3 runtime verification passed
M3 verification passed
```

The formal run exceeded a short unit-test duration because six fresh product
tasks, two public-source research acquisitions per repetition set, 42
behavioral variant executions and archive compression are the behavior under
test.  It completed within the documented campaign budget and cannot safely
be replaced by replay.

## Fail-closed findings encountered during execution

Three defects were found and corrected before the final campaign:

1. The first source succeeded but was rejected because the product port
   recomputed a scenario-owned outcome with the benchmark canonicalizer.
   Commit `21967e6f6c44c319383d945746849f30ed722e8f` uses the source owner's
   canonicalizer while retaining exact digest verification.
2. A software source hit the Windows nested-Git path limit.  Commit
   `9df7c591ec8a8dd4e241d4d1e5b18660720d9b57` uses a short collision-resistant
   physical source key while retaining the full input revision in receipts.
3. A complete 42-cell diagnostic campaign reached reporting and exposed that
   the metric-completeness receipt lacked its own digest.  Commit
   `95fcf7aeaed5b5ec80fb2f7178b97fbdf8adbeb6` adds and tests that receipt
   digest.  The completed diagnostic store was used only for a read-only
   report/freeze rehearsal; the final formal campaign reran all six sources
   and 42 cells from new roots on the final commit.

The failed and diagnostic roots remain ignored under `.tmp` and are absent
from formal evidence.

## Effective-code and parent closeout

Interval:

`c01fcacf7cd30c37aa9629aed76a7df5256dad63..95fcf7aeaed5b5ec80fb2f7178b97fbdf8adbeb6`

Conservative gate:

- raw additions: `12,744`;
- production-runtime bucket: `8,748`;
- accounted slice effective production: `8,594`;
- slice minimum: `6,000`;
- slice margin: `2,594`;
- parent M3-02A effective production: `17,155`;
- parent minimum: `12,000`;
- parent margin: `5,155`.

Excluded buckets:

- nonproduction runners/auditors: `1,844`;
- schema/DTO/data: `1,015`;
- tests/mock/fixture: `532`;
- type declarations: `146`;
- comments/docs/blank: `459`;
- generated: `0`;
- adapter-only: `0`;
- vendor/source-pool: `0`;
- formal JSON/ZIP/report evidence: excluded entirely from the implementation
  interval and production count.

All large production modules were included in the cohesion audit.  The
product port is not a thin black-box adapter: it owns fresh source isolation,
source/archive identity validation, behavioral variant execution,
normalization, deterministic verifiers, metric extraction and exact protected
evidence binding while leaving canonical runtime state with existing product
owners.

## Source, state and risk assessment

- Migration mode remains `test_eval_hardening_only`.
- No upstream repository, vendor tree or opaque external runtime was added.
- No new dependency, MCP server, plugin, listener, local port or provider CLI
  was added.
- OpenClaw remains `excluded_forward_only`.
- LangGraph remains narrow checkpoint/exact-resume conformance only.
- Canonical task, event, artifact, permission, scheduler, memory, recovery,
  checkpoint and provider owners did not move.
- The formal benchmark adds an isolated current-project Python subprocess.
  This is the planned fresh-source isolation boundary and therefore received
  risk-matched exact-commit validation, six clean-state roots, path/dependency
  scanning, real failure-path runs and integrated M3 freeze verification.
- The deterministic rules, not an LLM, decide permission, recovery,
  admission, metric completeness, report score and freeze.

The full repository test suite was not repeated for this ordinary parent
slice.  The changed/adjacent suites, exact-commit M3 runtime verification and
the directly relevant 42-cell formal campaign were prioritized.  The M3
numeric-stage aggregate remains mandatory after M3-02B, when the packaging
and clean-install sibling unit is also complete.

## Handoff

M3-02A is closed.  The formal evidence pointer is
`docs/reviews/evidence/M3-S02A-02/formal-current.json`; M3-02B and M3-03 may
consume the report, raw samples, statistical evaluation, source archives and
100-point evidence index without rerunning or rewriting protected M1/M2
facts.
