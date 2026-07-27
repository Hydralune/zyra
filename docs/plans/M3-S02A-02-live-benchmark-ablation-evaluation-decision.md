# M3-S02A-02 live benchmark, ablation and evaluation decision

## Frozen interval

- Slice: `M3-S02A-02`
- Baseline commit:
  `c01fcacf7cd30c37aa9629aed76a7df5256dad63`
- Required predecessor:
  M3-S02A-01 default-path and security regression verdict `PASS`
- Required predecessor receipt:
  `docs/reviews/evidence/M3-S02A-01/verification-summary.json`
- Parent baseline:
  `42f760229588aba917c7283b841587afc7142b0b`
- Slice minimum conservative effective production additions: `6,000`
- Parent M3-02A minimum after both slices: `12,000`

This decision is committed before production implementation. The
implementation commit will be frozen before generated benchmark evidence,
review prose and execution-state updates are committed.

Tests, fixtures, generated benchmark results, raw samples, reports, JSON
evidence, documentation, source maps, declarative schemas, thin launchers,
mock-only probes, repeated declarations, vendored content and cached output
receive zero effective-production credit.

## Protected starting facts

The implementation consumes, and does not reopen, the following protected
facts:

1. M3-01 selected one canonical Zyra owner for every protected state domain
   and proved owner loss without fallback masking.
2. M3-S02A-01 admitted the default CLI, API, Web and worker paths together
   with security, clean-state, causality and deterministic-control
   regressions.
3. M2-S05-02 produced completed software-delivery and cross-source-research
   causal archives with `2,164` and `7,169` effective steps, zero human
   intervention, five recovered faults per domain and stable artifacts.
4. M2-S05-03 established the seven required baseline/ablation variants, the
   comparison envelope, raw metric extraction, distribution aggregation,
   requirement evidence mapping and tamper-evident bundles.
5. M1 exit evidence records non-simulated local, isolated edge and cloud
   execution plus Anthropic and OpenAI provider/model receipts. This slice
   may validate and bind those exact receipts but may not relabel a replay,
   profile label or synthetic observation as a new dispatch.
6. OpenClaw remains `excluded_forward_only`.
7. LangGraph remains a narrow checkpoint/exact-resume semantic source. No
   StateGraph, Pregel, channel/reducer, ToolNode, store, stream, SDK or server
   becomes a benchmark dependency or state owner.

The M2 archives remain admissible as protected source evidence and regression
inputs. They do not, by themselves, count as a fresh M3 live repetition.

## Product responsibility inventory

The current experiment runtime is substantial, but its M2 boundary is not a
complete M3 release benchmark:

1. it executes a controlled workload derived from one protected archive and
   explicitly forbids new provider calls; it does not admit independently
   executed fresh-run receipts or prove that repetitions are distinct;
2. it has no run-manifest contract tying each repetition to a fresh input,
   clean-state fingerprint, sealed policy hash, exact commit, environment,
   live-source status and canonical event archive;
3. it does not reject step inflation by duplicate event, heartbeat, log,
   token chunk, polling, no-op mutation, replayed transition or disconnected
   causal ancestry at the benchmark admission boundary;
4. it does not combine device/edge/cloud isolation, provider/model diversity,
   privacy/SLA decisions, placement failover and disconnected degradation
   into one fail-closed deployment receipt;
5. it lacks a benchmark-level fault and requirement-change coverage ledger
   with detection/recovery/resume times and per-fault MTTR;
6. its statistics are per-scenario distributions, not a paired,
   repetition-aware comparison that reports P50/P95, dispersion, confidence,
   effect direction, missingness and cross-domain stability;
7. it does not enforce matrix fairness across fresh live repetitions,
   including exactly-one-capability ablations and identical protected
   conditions;
8. it has no benchmark transaction store that atomically admits raw samples,
   resumes interrupted cells, prevents duplicate work and freezes a
   completed exact-revision campaign;
9. it has no independent verifier-disagreement protocol separating
   deterministic task verifiers from an optional isolated model judge;
10. it does not produce the M3-03 handoff manifest linking every competition
    requirement to runs, samples, artifacts, metrics, failure reasons and
    checksums;
11. `scripts/verify_m3.py` cannot yet require an exact-revision formal
    benchmark receipt.

These are independently executable product responsibilities. Removing the
new benchmark package must break fresh-run admission, matrix fairness,
statistical evaluation, integrity verification and the M3 freeze gate. The
planned implementation is therefore sufficient to support the fixed 6,000
effective-production-line budget without filler.

## Migration decision

The migration mode is `test_eval_hardening_only`.

The slice adds a Zyra-owned Python benchmark runtime under
`packages/evaluation/zyra_evaluation/live_benchmark/**`. It composes the
existing Python scenario and experiment packages through their public data
and verification contracts. It does not copy an upstream runtime, create a
parallel scenario state owner or translate a TypeScript/Rust owner into
Python.

The benchmark runtime owns only:

- campaign plans, live repetition admission and comparison fairness;
- semantic-step, autonomy, placement, provider and fault evidence validation;
- deterministic metric extraction and paired statistics;
- atomic benchmark work/result custody and resumability;
- raw-sample integrity, report/evidence-index composition and freeze
  admission.

It does not own sessions, events, artifacts, permissions, routes, memory,
recovery, checkpoints, workers, providers, credentials, browser/terminal
sessions, topology or scenario execution.

## Source-language and target decision table

| Source path or protected evidence | Source language | Target path | Target language | Migration mode | Runtime owner after slice |
| --- | --- | --- | --- | --- | --- |
| `zyra_evaluation.scenario_runner` live result/archive contracts | Python | `live_benchmark/admission.py`, `semantic_steps.py`, `faults.py` | Python | `test_eval_hardening_only` | scenario owners remain canonical; benchmark admits immutable receipts |
| `zyra_evaluation.experiment_runtime` variants, metrics, statistics and bundle contracts | Python | `live_benchmark/matrix.py`, `metrics.py`, `statistics.py`, `integrity.py` | Python | `test_eval_hardening_only` | experiment runtime remains reusable; benchmark owns M3 campaign evaluation only |
| M3-S02A-01 exact-revision regression receipt | Python/JSON evidence | `live_benchmark/preconditions.py`, `freeze_gate.py` | Python | `test_eval_hardening_only` | regression hardening remains owner of its receipt |
| M1 local/edge/cloud and provider/model evidence | Python/JSON evidence | `live_benchmark/deployment.py` | Python | `test_eval_hardening_only` | scheduler/provider/worker owners remain canonical |
| Software deterministic test/schema/checksum/artifact receipts | Python and TypeScript owner outputs | `live_benchmark/verifiers.py` | Python | `test_eval_hardening_only` | repository/test/artifact owners remain canonical |
| Research citation/source/checksum/artifact receipts | Python owner outputs | `live_benchmark/verifiers.py` | Python | `test_eval_hardening_only` | research/source/artifact owners remain canonical |
| Existing canonical event, route, memory and recovery facts | Python and TypeScript owner outputs | `live_benchmark/metrics.py` | Python | `test_eval_hardening_only` | existing state owners; evaluator reads immutable facts |
| M3 verifier and package entrypoints | Python | `live_benchmark/cli.py`, `scripts/verify_m3.py`, `pyproject.toml` | Python | `test_eval_hardening_only` | benchmark receipt admission only |

There is no upstream migration quota, no `mixed` or `unknown` source-language
decision and no language exception. Python is retained because both formal
evaluation runtimes are already Python. Existing TypeScript and Rust/native
owners remain in their original languages and are observed through their
public receipts.

## Runtime architecture

```text
exact commit + predecessor receipts + campaign definition
  -> source/evidence preconditions
  -> complete two-domain x seven-variant x repetition plan
  -> fresh live-run port or independently supplied live receipt
  -> sealed autonomy + semantic-step + deployment + fault admission
  -> deterministic domain verifiers
  -> raw metric samples and paired repetition statistics
  -> atomic campaign store and resumable completion
  -> tamper-evident evidence index + report + M3-03 handoff
  -> M3 freeze-gate admission
```

The live-run port is dependency injected and fail closed when unbound. It may
call an existing in-process owner or a declared product command, but the
benchmark package never fabricates owner receipts. Diagnostic archive/replay
adapters are labeled non-live and are rejected by the formal admission path.

## Formal campaign contract

The default formal matrix contains both domains and all seven variants:

- `single-agent`;
- `static-full-connect-multi-agent`;
- `dynamic-heterogeneous-swarm`;
- `no-scheduler`;
- `no-memory-compact`;
- `no-recovery`;
- `no-low-entropy-communication`.

Every cell has at least three distinct repetition seeds. Protected
conditions—task family, input revision, budget, verifier, failure schedule,
hardware envelope, deployment profiles, provider policy and sealed policy—
are identical within a paired repetition. Each ablation changes exactly one
capability relative to the dynamic swarm anchor. A missing, duplicate,
skipped or extra cell fails completion.

Each accepting repetition must prove:

- fresh input identity and clean-state fingerprints;
- a new live run rather than archive replay or fixture;
- exact Git commit, environment, hardware and policy digest;
- sealed autonomy, zero human/operator intervention and deterministic
  deny/replan for unknown/high-risk actions;
- canonical semantic transitions with at least one formal run at or above
  2,000 effective transitions;
- stable final artifact and task completion;
- deterministic verifier success;
- fault, requirement-change, recovery and resume facts;
- device/edge/cloud isolation and at least two provider/model capabilities,
  either as new dispatch receipts or as an explicitly linked still-valid
  protected receipt;
- privacy, SLA, placement, model split, failover and disconnected degradation
  results.

## Metrics and statistics

The metric catalog covers:

- task success, deterministic quality and final constraint satisfaction;
- artifact and requirement drift, coherence and verifier disagreement;
- memory hit/restore, compact ratio and context loss;
- communication entropy, useful-message ratio and duplicate ratio;
- topology sparsity, mutation/churn and active-role diversity;
- token, wall time, throughput and cost;
- tier/provider/model distribution, resource utilization, SLA and privacy;
- failure detection, recovery latency, MTTR and delivery-after-fault;
- human/operator intervention and model/provider mix.

Reports include raw samples, run count, missing count, P50, P95, mean,
standard deviation, median absolute deviation, interquartile range,
deterministic bootstrap confidence intervals, paired deltas and effect
direction. Missing metric blocks, non-finite values, mismatched units,
inconsistent sample cardinality or a metric defined only for the preferred
variant fail closed.

## Integrity and verifier policy

All manifests, events, samples, artifacts and report sections use canonical
JSON digests. The campaign store uses atomic replace and an append-only hash
chain. It rejects duplicate cell completion, stale leases, cross-campaign
receipts, path escape, undeclared artifacts, digest mismatch and modification
after freeze.

Software delivery is judged by deterministic tests, schema/checksum and
artifact invariants. Research delivery is judged by source fetch status,
citation mapping, claim support, checksum and artifact invariants. An optional
model judge must run in an isolated evidence namespace, can never override a
deterministic failure and reports uncertainty and disagreement explicitly.

Negative tests mutate step classification, intervention records, policy
hashes, placement receipts, provider receipts, artifacts, raw samples,
verifier outputs and report digests. All mutations must be rejected with a
stable failure code.

## Effective-code accounting commitment

The slice interval is:

`c01fcacf7cd30c37aa9629aed76a7df5256dad63..<implementation_commit>`.

The parent interval is recomputed directly as:

`42f760229588aba917c7283b841587afc7142b0b..<implementation_commit>`.

Per-file buckets are:

- production benchmark/admission/statistics/integrity behavior;
- tests;
- docs/review;
- generated evidence/report/raw samples;
- configuration/data;
- schema/type-only;
- adapter/launcher-only;
- mock/fixture;
- vendor/source-pool.

Every file over 500 raw additions, over 20 percent of effective production or
over 30 percent excluded content receives an individual review. Dataclass
fields, repeated metric/requirement tables, report prose and CLI argument
plumbing are excluded. If the behavior above cannot naturally provide 6,000
conservative production lines, implementation stops rather than adding
framework fill. At this checkpoint no planning blocker is declared.

## Validation and risk decision

Focused tests cover plan completeness, live/fresh admission, zero
intervention, step-inflation rejection, deployment/provider evidence,
fault/requirement-change coverage, deterministic verifier disagreement,
metric completeness, paired statistics, transaction resume, tamper
resistance, report/handoff generation and freeze-gate admission.

Adjacent regression covers M3-S02A-01, M2-S05-02, M2-S05-03 and M3 verifier
paths. The formal evidence run uses fresh working/state roots and records
exact commands, timings, raw samples and failure reasons.

The slice adds no external dependency, provider credential, port, Docker
context, MCP server, plugin, dynamic import, canonical owner transfer or
default runtime fallback. It adds a productized benchmark command and
subprocess-capable run port, so command allowlisting, environment custody,
timeout/cancellation, process cleanup, path confinement and secret redaction
receive matching tests. Full cleanroom and full dependency/process/source
custody audit remain owned by the M3-02 numeric-stage aggregate after
M3-02B.
