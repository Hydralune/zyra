# M3-S01A-02 state owner, reachability, and evidence audit

## Verdict

This slice is complete as an inventory, release-gate, and downstream-work-input
implementation. It does not claim that Zyra is freeze-ready. The frozen
implementation target is
`2de115f565667eda0ab9664f98af3181f45460e9`. Inventory mode is valid because it
successfully audited the repository and retained every defect. Candidate and
freeze policy remain invalid while the reported blockers are unresolved.

The final receipt digest is
`sha256:8d2e533a42681301cf5b5b142f0bbf391c0c78042ee9c06adcaeb25ba478cdec`.
Its exact-revision S01A-01 source-custody input digest is
`sha256:8bb2b63081bf8b7a755cbd0bf6a4124af611e8f1b4f537244e70d073f3f73948`.

M3-01A closes its implementation and effective-code contract at this slice:

- S01A-02 contributes 8,666 effective production lines against 4,500, a
  margin of 4,166.
- M3-01A contributes 16,934 effective production lines against 9,000, a
  margin of 7,934.
- both direct Git-interval recomputations report zero data-as-code, zero
  vendor/source-pool credit, and no effective-line finding.

M3 itself remains intentionally open. The work queue is the input to M3-01B,
M3-02A, M3-02B, and M3-03.

## What became Zyra-owned

The implementation lives in
`packages/evaluation/zyra_evaluation/freeze_audit/` and is invoked through
`zyra-freeze-audit` or `scripts/audit_state_owner_evidence.py`. It owns these
audit stages:

1. strict state-owner and competition-evidence catalog parsing;
2. Python AST and TypeScript/TSX/JavaScript lexical call graphs;
3. default CLI, API, Web, and worker entrypoint reachability;
4. owner, store, writer, projection, cache, checkpoint, recovery, and fallback
   role validation;
5. event-to-mutation, artifact, metric, and verifier causality;
6. read-only bridging of S01A-01 semantic-port, opaque, dynamic-download,
   source-similarity, and upstream-boundary risks;
7. production, test, generated, data, docs, vendor-like, adapter-only,
   mock/fixture, runtime-asset, and other line buckets;
8. requirement-to-owner, entry, live evidence, event, mutation, artifact,
   metric, test, commit, config, and M3-owner validation;
9. deterministic policy and checksum-bound downstream queues.

This slice did not acquire any audited runtime's canonical state. It did not
change a session, permission, memory, scheduler, artifact, graph, provider,
MCP, plugin, gateway, terminal, or browser owner. It added no external
dependency, process, port, Docker service, MCP server, dynamic import, or
network download.

## Language and migration boundary

The preimplementation decision fixed the implementation language as Python
and the migration mode as `audit_only_no_runtime_migration`. Every catalog
reference records an exact language. Python, TypeScript, and TSX references
are kept distinct; the catalog contains no `mixed` or `unknown` tag.

The audit reads TypeScript and TSX state owners without porting their control
flow. Rust/native line classification exists for repository-wide custody, but
no state-owner row falsely claims a Rust/native runtime when the selected
owner is Python or TypeScript.

## State custody catalog

The version-controlled catalog contains 11 required state domains, 11
canonical owners, 11 stores, 13 declared writers, 7 projections, 2 caches,
11 checkpoints, 11 recovery paths, and one explicit fallback:

| Domain | Canonical owner | Store or durable boundary |
| --- | --- | --- |
| session/event projection | `ClaudeRuntimeCore` | `CodeWorkerSessionStore` |
| permission | `PermissionApprovalRuntime` | `PermissionAuditRuntime` |
| memory/compact | `RetrievalIntegrationRuntime` | `SQLiteRetrievalIndex` |
| scheduler/recovery | `RecoveryApplication` | `RecoveryPlanStore` |
| artifact | `LocalArtifactStore` | `LocalArtifactStore` |
| worker route | `WorkerPoolFoundationRuntime` | `WorkerPoolStore` |
| graph checkpoint | `GraphStateCustody` | `GraphStateStore` |
| provider/credential/failover | `ProviderControlPlane` | `ProviderControlPlaneStore` |
| MCP/plugin registry | `McpRuntimeCoordinator` | `McpRequestJournal` |
| gateway lease/busy | `SandboxGatewayRuntime` | `GatewayStateStore` |
| terminal/browser session | `TerminalSessionRegistry` | `TerminalStateStore` |

All 67 required source references resolve to executable production source.
Catalog parsing reports no duplicate domain, entry, event link, requirement,
owner alias, or language mismatch.

The catalog is an audit assertion, not proof by itself. Reachability,
ownership, causality, requirement, and mutation audits independently test the
assertions and emit blockers when the runtime does not yet substantiate them.

## Default reachability inventory

The combined repository graph contains 83,917 nodes and 212,267 verified or
declared edges. It audits 11 default entries across all required surfaces:

- 1 API;
- 1 CLI;
- 2 Web;
- 7 worker.

Three entries currently satisfy the complete default-entry-to-owner/write-path
contract and eight do not. The audit emits 33 reachability findings, including
unverified trace edges, owner/write-path unreachability, dead catalog
references, and domains with no valid default entry.

This is a truthful freeze blocker, not a slice failure. The slice would fail
if those gaps were missing from the receipt or if candidate mode accepted
them. M3-01B owns the default rewiring and owner-evidence work.

## Ownership and causality inventory

All declared references exist, but the stricter semantic proof reports 60
ownership findings. The main classes are missing disable probes, tests not
bound to the selected owner token, undeclared owner-like candidates, and
canonical write signals that are not sufficiently demonstrated from the
declared default path.

The causality catalog contains 11 event-to-mutation links. Six event names are
found, all 11 mutation targets contain a semantic write signal, and two links
currently satisfy the complete producer-to-mutation path. Nine remain invalid.
The 54 causality findings include missing event identity attributes,
unobserved event emission, unreachable mutation paths, and semantic events
that lack a catalog link.

Heartbeat, repaint, replay, log-only, and no-op events do not satisfy semantic
effect. Artifact and metric effects additionally require a materialized
artifact or metric target.

## Source-risk bridge

S01A-01 ran against the same implementation revision and its receipt passed
schema, revision, rule-switch, risk-summary, repository-manifest, work-queue,
and digest validation.

The bridge converts 109 relevant source-custody risks into local findings:

| Category | Records |
| --- | ---: |
| dynamic download/process/import | 69 |
| upstream/root-source boundary | 31 |
| opaque bundle/binary/runtime | 8 |
| semantic port/source similarity | 1 |

There are 101 blocking records. The receipt also reports one similarity pair,
three opaque-runtime hits, and nine broad LangGraph hits. The bridge preserves
the original severity, disposition, remediation, owner unit, capability, and
fingerprint. A review correction was required here: the first implementation
placed these risks only in section records. The final implementation also
turns them into actionable local findings, so they reach M3-01B instead of
silently disappearing from the downstream queue.

OpenClaw was not restored, read as a source repository, or assigned a forward
role. Existing historical denial/provenance findings remain inputs from the
protected audit boundary.

## Competition evidence catalog

The catalog contains all 19 required identities: eight `REQ-*` rows and eleven
`SCORE-*` rows. Fifteen rows carry `verified` source status and four carry
`partial` source status. Each row binds the required owner domains, default
entries, live evidence, event links, mutation references, artifact or metric
paths, tests, commits, configs, and one of the allowed M3 owners.

The stricter current-runtime audit marks all 19 rows invalid because one or
more owner, default entry, causality, live-evidence, commit, or config checks
remain incomplete. It emits 78 findings rather than inheriting an earlier
100-point claim without revalidation.

Downstream assignment is:

- 11 rows to M3-02A;
- 2 rows to M3-02B;
- 6 rows to M3-03.

This slice proves that missing competition evidence cannot pass unnoticed. It
does not substitute catalog rows for live benchmarks.

## Downstream work inputs

The final policy normalizes 334 findings into 138 work items:

- 285 blocking findings and 49 warnings;
- 111 blocking work items;
- priorities: 29 P0, 82 P1, and 27 P3;
- actions: 71 source-risk dispositions, 28 causality repairs, 19 requirement
  evidence additions, 11 owner resolutions, and 9 default rewires.

Owner allocation is:

| Owner unit | Work items |
| --- | ---: |
| M3-01B | 119 |
| M3-02A | 11 |
| M3-02B | 2 |
| M3-03 | 6 |

Each downstream file is bound to the source slice, target revision, receipt
digest, owner unit, and its own digest. M3-01B must consume the cleanup file;
it must not reinterpret projections, caches, fallbacks, semantic ports, or
source-pool material as a second canonical owner.

## Effective-code audit

The gate uses direct Git intervals, not `numstat` as credit. For S01A-02 it
classifies 12,906 raw additions as follows:

| Bucket | Raw lines | Effective production |
| --- | ---: | ---: |
| production-shaped source | 10,644 | 8,666 |
| data/catalog | 1,341 | 0 |
| tests | 743 | 0 |
| docs/decision | 178 | 0 |

The effective classifier excludes imports, comments/blanks, schema/DTO fields,
signature continuation, protocol/interface content, generated markers,
adapter-only code, and data-shaped literals.

Two false-positive boundaries were corrected under mutation tests:

1. executable algorithm vocabularies such as write-method sets are not treated
   as inflated data payloads; named catalog/record/manifest/evidence tables and
   extremely large literals remain excluded and blocking;
2. `third_party/NOTICE.md`, `LICENSE`, and `COPYING` are documentation with zero
   code credit, while source under `vendor/**`, `third_party/**`, or
   `runtime-sources/**` remains vendor-like and blocks the interval.

The parent interval contains 16,934 effective lines. The 147,867 documentation
lines, 10,831 data lines, and 1,364 test lines receive zero production credit.
No single file exceeds the 20-percent concentration threshold.

## Adversarial and disconnect evidence

The final unit matrix covers:

- duplicate canonical owner;
- projection promoted to owner;
- test-only default entry;
- fallback takeover around the canonical owner;
- heartbeat/fake causation;
- catalog-shaped data-as-code inflation;
- algorithm vocabulary negative control;
- license-provenance versus vendor-source classification;
- disconnected S01A-01 source receipt;
- every rule switch disabled individually;
- one fully clean CLI-to-owner-to-writer and event-to-mutation repository.

The clean repository produces no reachability or causality finding. Every
mutated repository produces its dedicated blocker. Disabling a rule removes
its detection and therefore cannot be used as release evidence.

## Critical self-review

Five concrete defects were corrected before freezing the implementation:

1. source-custody `Finding` objects initially leaked into this package's
   policy model. An explicit immutable adapter now preserves their fields
   without coupling dataclass shapes.
2. Python and script symbol/event/mutation nodes initially defaulted to
   production even when their file was a test. Production status now
   propagates to every graph node, and a test-only default mutation fails.
3. the line classifier initially treated executable write vocabularies and
   path tuples as data-as-code. Named payload-shape and extreme-size rules now
   distinguish data inflation from algorithm constants.
4. parent closure initially treated the protected `third_party/NOTICE.md` as
   vendored source. Exact license-provenance names now receive documentation
   classification and zero credit; executable third-party source still
   blocks.
5. source-risk records initially did not enter the current work queue. The
   final bridge materializes all 109 relevant records as checksum-bound local
   findings while keeping receipt structural validity separate from release
   readiness.

No issue was hidden by editing a protected M1/M2 owner, removing an upstream
finding, weakening candidate policy, counting data/docs/tests, or restoring
OpenClaw.

## Verification

- `python -m pytest -q tests/unit/test_state_owner_evidence_audit.py
  tests/integration/test_state_owner_evidence_repository_audit.py`:
  25 passed.
- `python -m pytest -q tests/unit/test_source_custody_audit.py
  tests/integration/test_source_custody_repository_audit.py`:
  23 passed.
- `python -m compileall` passes for the audit package, launcher, and tests.
- `git diff --check` passes for implementation changes.
- the final vendor-inclusive inventory command returns exit 0 at the exact
  target revision and atomically writes the receipt, summary, work queue, and
  four downstream files.
- candidate policy rejects the same sections while blockers remain.
- no full-repository test suite was run: this ordinary audit slice changed no
  runtime owner or global default, and its targeted plus adjacent validation
  completed within the slice's proportional verification boundary.

## Handoff

M3-01B must consume
`docs/reviews/evidence/M3-S01A-02/downstream/m3_01b.json` and resolve or
explicitly disposition its 119 work items. M3-02A, M3-02B, and M3-03 must
consume their corresponding checksum-bound inputs.

M3-01A is closed as an audit implementation parent. Zyra is not yet
freeze-ready: default entry, owner proof, causality, source-risk disposition,
and requirement evidence remain visible blockers assigned to later units.
