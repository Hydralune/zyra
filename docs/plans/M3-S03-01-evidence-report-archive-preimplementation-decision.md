# M3-S03-01 Evidence Report And Archive Preimplementation Decision

## Frozen boundary

- Slice: `M3-S03-01`
- Baseline commit: `944846fd484b465b3c4e2b4ec87565752b4baf67`
- Baseline tree: `2e39acf1fa7f7f7582f28c258a779b51bc4f7951`
- Decision timing: before the first production-code change
- Migration mode: `report_and_freeze_only`
- Canonical-state transfer: none
- Runtime success owner transfer: none

The slice consumes verified M3-01 custody findings, M3-02A formal live benchmark
evidence, and M3-02B release evidence.  It creates immutable evidence
projections and validators.  A report, index, archive, replay receipt, or
material document never becomes task-success truth and never mutates a
benchmark run.

The branch intentionally starts from the current M3-S02B-02 implementation
head while its aggregate review runs elsewhere.  M3-S03-01 completion is
conditional on the reviewed M3-S02B-02 evidence identity.  A later M3-S02B-02
fix requires rebase/reverification; this branch must not rewrite the
02B-02 review result.

## Product responsibilities frozen before implementation

| Responsibility | Product module | Default product entry | Failure semantics |
| --- | --- | --- | --- |
| input discovery and immutable binding | `zyra_evaluation.freeze_reporting.inputs` | `zyra-first-stage-evidence build` | missing, ambiguous, stale, or digest-invalid M3 input blocks |
| 40/25/20/15 evidence index | `freeze_reporting.evidence_index` | pipeline build/verify | a score item without default-entry, live mutation, artifact, metric, test, config, commit, and checksum refs blocks |
| tamper-evident archive | `freeze_reporting.archive` | pipeline archive/verify | member mismatch, path escape, duplicate path, broken chain, or bundle checksum mismatch blocks |
| projection replay | `freeze_reporting.replay` | pipeline verify | event/checkpoint/artifact projection inconsistency blocks; replay cannot assert task success |
| role-aware internalization ledger | `freeze_reporting.ledger` | pipeline materialize | invalid role, multiple primary owners, OpenClaw future role, or active item without owner/path/test blocks |
| algorithms and complexity linkage | `freeze_reporting.algorithms` | pipeline materialize | pseudocode without implementation/test anchors or complexity references blocks |
| dual-domain cases and ablation | `freeze_reporting.cases`, `freeze_reporting.ablation` | pipeline materialize | replay-only case, fewer than 2,000 effective transitions, nonzero human intervention, missing raw/P50/P95/config/verifier blocks |
| edge/cloud/model material | `freeze_reporting.compatibility` | pipeline materialize | label-only placement/model rows, missing credential/degradation result, or missing receipt blocks |
| freeze/material report | `freeze_reporting.report` | pipeline build | report sections are derived from admitted receipts; handwritten unsupported claims block |
| orchestration and atomic output | `freeze_reporting.pipeline` | console script and productized script | partial output is discarded; blocking verdict is machine readable |

The listed responsibilities are independent validators, resolvers, builders,
and transactional archive operations.  Report Markdown/PDF/JSON, raw metrics,
screenshots, copied event logs, archive members, schemas, DTO-only lines, tests,
and this decision record are excluded from the 4,500-line production minimum.

## Source, language, role, and migration decision

| Input/source role | Source path or evidence | source language | Zyra target | target language | migration mode | Canonical owner |
| --- | --- | --- | --- | --- | --- | --- |
| M3-01 active custody audit input | `zyra_evaluation.freeze_audit/**`, M3-01 receipts | Python | `zyra_evaluation.freeze_reporting.ledger` | Python | `report_and_freeze_only` | existing M3-01 source/state custody owners |
| M3-02A formal benchmark input | `zyra_evaluation.live_benchmark/**`, `docs/reviews/evidence/M3-S02A-02/**` | Python + JSON evidence | `freeze_reporting.inputs/evidence_index/cases/ablation` | Python | `report_and_freeze_only` | existing live benchmark/store and runtime owners |
| M3-02B release input | `zyra_productization.release/**`, reviewed release receipts | Python + package/runtime language inventory | `freeze_reporting.inputs/archive/compatibility` | Python | `report_and_freeze_only` | existing release/deployment owners |
| active source implementation facts | M1/M2/M3 custody ledgers and source-to-target evidence | Python, TypeScript/TSX, Rust/native as recorded | `freeze_reporting.ledger/report` projections | Python generator preserving per-row language | `report_and_freeze_only` | existing per-domain Zyra owners |
| `claude-code-best` | protected source-custody evidence | TypeScript/TSX | report role projection only | Python generator; emitted row retains TypeScript | `reference_to_existing_custody` | existing Claude-derived TypeScript runtime |
| `opencode`, browser-use, OpenHands, AgentScope, Hermes, OMP | protected source-custody evidence | TypeScript/TSX, Python, Rust/native as recorded | report role projection only | Python generator preserving source language | `reference_to_existing_custody` | existing selected Zyra owners |
| Agent Framework and LangGraph | protected conformance/reference evidence | Python/.NET and Python | conformance and LangGraph correction matrices | Python generator | `conformance_report_only` | existing Zyra owners; LangGraph only narrow exact-resume semantics |
| OpenClaw | historical provenance/license/no-runtime-dependency evidence only | historical TypeScript | excluded-forward-only report row | Python generator | `excluded_forward_only` | none after M1-S05D-02 |
| Claude auxiliary repositories | analysis references only | Markdown/diagram assets | report reference rows | Python generator | `reference_only` | none |

No primary or supplementary source runtime is migrated or rewritten in this
slice, so no retained-control-flow source-language quota is created.  Python is
the existing language of the evaluation/reporting product boundary and is used
to inspect multi-language frozen artifacts without translating their runtime
control flow.

## Cross-language exception decision

The generator reads TypeScript/TSX, Python, and Rust/native custody facts and
emits language-preserving rows.  This is not a semantic port: it neither
executes nor replaces those runtimes, changes their build, nor assumes their
state ownership.  The Python boundary is selected because the existing
`zyra_evaluation`, benchmark, source-custody, release admission, and packaging
interfaces are Python.  Equivalence is defined as checksum-bound projection of
existing receipts, with tamper/missing/broken-link/disable tests.

## Planned production modules and conservative effective budget

| Module | Product responsibility | Conservative effective production |
| --- | --- | ---: |
| `canonical.py`, `errors.py`, `contracts.py` | canonical serialization, validation, diagnostics, reference contracts | 450 |
| `inputs.py`, `links.py` | input discovery, digest/path/commit/reference admission | 550 |
| `evidence_index.py`, `scoring.py` | 100-point matrix and blocking rules | 650 |
| `archive.py`, `replay.py` | immutable manifest/hash chain and projection replay | 900 |
| `ledger.py`, `sources.py` | source roles, languages, owners, debt, LangGraph/OpenClaw rules | 650 |
| `algorithms.py`, `cases.py`, `ablation.py`, `compatibility.py` | material projections and validators | 950 |
| `report.py`, `pipeline.py`, `cli.py` | deterministic material generation, transactional entry, machine verdict | 750 |
| **Total planned** | reports/data/tests excluded | **4,900** |

If implementation shows these real responsibilities cannot naturally reach
4,500 effective production lines, work stops as a planning blocker.  Empty
managers, repetitive wrappers, pre-authored findings, duplicated DTOs, report
content, archive data, generated schemas, or tests may not close the gap.

## Dynamic reachability and disable evidence

The default entry is a new console command, `zyra-first-stage-evidence`, backed
by the package pipeline.  It discovers real reviewed evidence from the
repository, performs all admissions, writes outputs transactionally, verifies
the written archive, and returns a blocking/nonblocking verdict.

Tests must demonstrate:

1. complete real M3 inputs navigate from every score item to all eight required
   evidence classes;
2. missing score evidence, broken relative link, stale commit, bad schema, and
   tampered member fail closed;
3. event/checkpoint/artifact replay inconsistency fails without redefining
   task success;
4. inactive/reference sources are legal, while duplicate primary owners,
   active ownerless rows, and any future OpenClaw role fail;
5. disabling index or archive verification causes an injected invalid package
   to escape the pipeline, making the explicit disconnect tests fail.

## High-risk assessment

The slice adds no provider call, external package, MCP server, plugin, listener,
Docker dependency, dynamic import, canonical state migration, or runtime
fallback.  It does add a release/freeze CLI and archive format, but those are
the product responsibilities assigned by M3-03 and do not alter task runtime.
Normal slice-level focused behavior, failure, disconnect, source-path, and
incremental line audits apply.  Final clean-machine and submission exit remain
owned by M3-S03-02 unless the implementation unexpectedly changes release
packaging boundaries.
