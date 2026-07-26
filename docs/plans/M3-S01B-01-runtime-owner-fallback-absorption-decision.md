# M3-S01B-01 runtime owner and fallback absorption decision

## Frozen interval

- Slice: `M3-S01B-01`
- Baseline commit:
  `4b6d0d1d81332886385adcd32ff6205f6403d0f0`
- Baseline source-audit revision:
  `2de115f565667eda0ab9664f98af3181f45460e9`
- Baseline source-audit evidence commit:
  `4b6d0d1d81332886385adcd32ff6205f6403d0f0`
- Required downstream input:
  `docs/reviews/evidence/M3-S01A-02/downstream/m3_01b.json`
- Required downstream-input digest:
  `sha256:f5b931f50c960856dfa7c8197d1c22d7c4ec3f6110832ed5114143ef4eb3d4d6`
- Required parent receipt digest:
  `sha256:8d2e533a42681301cf5b5b142f0bbf391c0c78042ee9c06adcaeb25ba478cdec`
- Frozen input population: `119` M3-01B work items: `100` blocking
  P0/P1 items and `19` non-blocking P3 items.
- Minimum conservative effective production additions: `4,500`.
- Parent minimum after both M3-01B slices: `9,000`.

The baseline is the completed M3-01A evidence commit. Documentation, generated
receipts, catalogs, tests, fixtures, adapters, schema-only declarations,
source-pool material and vendor-like content receive zero production credit.
The implementation interval begins at the baseline above even though this
decision is committed before implementation.

## Decision

The slice uses `owner_absorption_and_cleanup`. It does not create a new
canonical state owner. It adds a Zyra-owned productization control boundary
that consumes the checksum-bound M3-01A queue, proves the selected owners,
rejects duplicate or fallback ownership, validates default composition, and
binds canonical events to real mutation receipts. Existing owner modules are
repaired in their existing languages when the queue proves a real gap.

The productization control boundary owns only:

1. immutable owner-binding and subordinate-role descriptions;
2. owner availability, generation and fail-closed admission leases;
3. derived readiness and queue-disposition receipts;
4. canonical-event-to-mutation verification receipts;
5. runtime/source/process boundary classification;
6. deterministic handoff from the protected M3-01A input to M3-01B evidence.

It does not own session, permission, memory, scheduler, artifact, worker,
graph, provider, MCP/plugin, gateway, terminal or browser state. Derived
readiness and cleanup receipts cannot be replayed as a runtime mutation and
cannot authorize a fallback.

## Source-language and migration decisions

| Source repository and path family | Source language | Target path family | Target language | Migration mode | Canonical owner after this slice | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| Zyra `packages/runtime/zyra_runtime/**` | Python | same owner modules plus `productization/**` | Python | `owner_absorption_and_cleanup` | existing Python domain owners; productization is guard/evidence only | retain and repair in place |
| Zyra `packages/memory/zyra_memory/**` | Python | same memory owner modules | Python | `owner_absorption_and_cleanup` | `RetrievalIntegrationRuntime` / `SQLiteRetrievalIndex` / `MemoryCommitRuntime` | retain and repair in place |
| Zyra `packages/scheduler/zyra_scheduler/**` | Python | same worker/recovery owner modules | Python | `owner_absorption_and_cleanup` | `WorkerPoolFoundationRuntime`, `WorkerPoolStore`, `RecoveryApplication`, `RecoveryPlanStore` | retain and repair in place |
| Zyra `packages/orchestration/zyra_orchestration/graph_custody/**` | Python | same graph owner modules | Python | `owner_absorption_and_cleanup` | `GraphStateCustody` and `GraphStateStore` | retain narrow exact-resume owner |
| Zyra `packages/workers/zyra_workers/**` | Python | same terminal/browser owner modules | Python | `owner_absorption_and_cleanup` | `TerminalSessionRegistry`, `TerminalStateStore`, `BrowserSessionRuntime` and its subordinate store | retain and repair in place |
| Zyra `apps/api/zyra_api/**` | Python | same API composition plus productized readiness integration | Python | `owner_absorption_and_cleanup` | no API-owned duplicate state; API composes selected owners | rewire default composition |
| Zyra `packages/runtime/claude-runtime/**` | TypeScript | same QueryEngine/permission/skill/session modules | TypeScript | `owner_absorption_and_cleanup` | existing TypeScript QueryEngine and permission owners | retain and repair in place |
| Zyra `packages/runtime/provider-control-plane/**` | TypeScript | same provider owner modules | TypeScript | `owner_absorption_and_cleanup` | `ProviderControlPlane` and `ProviderControlPlaneStore` | retain and repair in place |
| Zyra `packages/integrations/claude-mcp/**` | TypeScript | same MCP owner modules | TypeScript | `owner_absorption_and_cleanup` | `McpRuntimeCoordinator` and `McpRequestJournal` | retain and repair in place |
| Zyra `apps/web/**` and TypeScript projections | TypeScript/TSX | same Web projections | TypeScript/TSX | `owner_absorption_and_cleanup` | no browser-local canonical owner | projection only; no fallback promotion |
| Zyra M3 audit packages | Python | forward precision and result-consumption changes only | Python | `owner_absorption_and_cleanup` | audit result remains derived evidence | refine structural classification; do not rewrite protected evidence |

There is no `mixed` or `unknown` language entry. Same-language owner repairs
must produce non-zero Python and TypeScript production changes if their
respective queue items remain real after structural inspection. A new
Zyra-owned Python productization guard is not a port of an upstream owner and
therefore needs no cross-language exception.

No new source repository is read or migrated in this slice. Mature
Claude/OpenCode/OpenHands/browser-use/Oh My Pi/Hermes mechanisms already
productized during M1/M2 remain protected implementation facts. Their names
in provenance, event normalizers or conformance tests are not themselves
runtime dependencies.

## Canonical state-owner freeze

| Domain | Canonical owner | Durable boundary | Forbidden absorption |
| --- | --- | --- | --- |
| session/event projection | TypeScript `ClaudeRuntimeCore`; durable task/session facts through the existing Zyra event/session boundary | existing task/event store and `CodeWorkerSessionStore` where assigned | Web projection, replay helper or productization guard |
| permission | TypeScript `PermissionApprovalRuntime` / permission continuation owner | `PermissionAuditRuntime` and existing Python transport custody where assigned | Web permission projection or Python decision fallback |
| memory/compact | `RetrievalIntegrationRuntime` / `MemoryCommitRuntime` | `SQLiteRetrievalIndex` and compact archive boundaries | retrieval cache or productization receipt |
| scheduler/recovery | `RecoveryApplication` | `RecoveryPlanStore` and existing checkpoint/route owners | audit runtime, causal projection or LLM fallback |
| artifact | `LocalArtifactStore` | content-addressed artifact root | browser cache, gateway receipt or cleanup archive |
| worker route | `WorkerPoolFoundationRuntime` | `WorkerPoolStore` | event projector, recovery projection or simulated label |
| graph checkpoint | `GraphStateCustody` | `GraphStateStore` | LangGraph StateGraph/Store/Pregel/channel runtime |
| provider/credential/failover | TypeScript `ProviderControlPlane` | `ProviderControlPlaneStore` | Python provider decision fallback or UI catalog |
| MCP/plugin registry | TypeScript `McpRuntimeCoordinator` | `McpRequestJournal` and admitted skill/plugin registry | source scanner, UI catalog or opaque MCP process |
| gateway lease/busy | `SandboxGatewayRuntime` | `GatewayStateStore` / lease owner | Hermes gateway/PTY black box or worker projection |
| terminal/browser session | `TerminalSessionRegistry` plus the assigned browser session owner | `TerminalStateStore` and subordinate browser store | Web tab store, OpenHands SDK state or replay fixture |

The slice may clarify a durable boundary or replace a false catalog assertion
with the already-selected owner. It may not transfer canonical custody,
transaction, lease, idempotency or restore semantics to the productization
guard.

## Frozen work-item classification

The 119 work items are consumed without changing their work IDs or source
fingerprints:

- `11` `resolve_owner` items must gain executable owner-loss/disable proof and
  must reject fallback success;
- `9` `rewire_default` items must prove a real default entry to the selected
  owner and canonical write path;
- `9` blocking `repair_causality` items must emit or validate the named event
  after a real effect with the required identities;
- `63` blocking `dispose_source_risk` items must be removed, made structurally
  non-runtime, or bound to a declared in-repository process/dependency
  contract;
- `8` non-blocking source risks must be explicitly externalized as evidence or
  retained with provenance;
- `19` non-blocking event findings must be classified as canonical mutation
  evidence or explicitly derived/non-semantic.

No new work item may be invented to reach the effective-code minimum.
Unresolved items remain blockers in the M3-01B result.

## Source-risk disposition rules

1. A process finding is resolved only from a parsed process call or package
   script. A source repository name beside the words `RPC`, `process`,
   `runtime` or `CLI` is provenance, not proof of a launched process.
2. `npm`, `bun`, `pnpm` and `yarn` are dynamic installers only for install,
   add, update, exec/dlx or equivalent dependency-acquisition commands.
   Frozen test/build invocations do not acquire code at runtime.
3. Non-literal Python imports in production require a closed registry and
   allowlist. Test-only fixture loaders and audit-only scanners remain
   non-runtime and cannot satisfy default reachability.
4. Parent-source paths are blockers in production/runtime commands. Exact
   deny-list literals used by submission-boundary tests and historical
   provenance are non-runtime evidence and receive no product capability
   credit.
5. Common declared interpreters such as the pinned Python/Node/Bun toolchain
   are not opaque decision owners. An executable native bundle remains
   blocking when it owns behavior without source/build checksum and semantic
   health.
6. Checksum-bound evidence archives under `docs/reviews/evidence/**` are
   externalized evidence and must be unreachable from runtime imports,
   process commands and package assets.
7. The browser serializer similarity record is accepted only with cropped
   same-language provenance plus Zyra-owned state, error, restore and behavior
   evidence.

These are structural rules, not path/fingerprint allowlists. Mutating a test
or evidence file into a production entry must restore the blocker.

## Explicit dependency/source decisions

- OpenHands SDK and its runtime state do not enter the default path. Existing
  OpenHands-derived UI/product patterns remain Zyra code; no OpenHands package
  or process is added.
- Hermes gateway/PTY remains conformance/reference input only. No Hermes
  token, gateway, PTY process or state database is accepted as an owner.
- OpenCode desktop runtime, dynamic plugin loader and job process remain
  outside the default runtime. Existing cropped TypeScript product code keeps
  its Zyra build and owner boundaries.
- LangGraph remains narrow checkpoint/exact-resume conformance. There is no
  `StateGraph`, channel/reducer, Pregel, ToolNode, server, SDK, Store or broad
  LangGraph production dependency.
- Oh My Pi RPC/event mapping and cropped TypeScript worker mechanisms are
  in-repository Zyra code. They do not launch an Oh My Pi CLI/process or own
  an Oh My Pi database. Any actual process launch would require an explicit
  Zyra process profile and semantic health and remains blocking otherwise.
- OpenClaw remains `excluded_forward_only`: no source restoration, source
  read, runtime path, package, process, test quota or new ledger row.

## Runtime and evidence path

The target path is:

```text
M3-01A checksum-bound queue
  -> strict queue/digest admission
  -> structural source/process disposition
  -> canonical owner registry and owner-loss guard
  -> default composition probes
  -> causal mutation receipt verification
  -> API/CLI runtime readiness and M3-01B receipt
```

Readiness is derived from real probes and is fail-closed. Unknown,
unregistered, duplicate, disabled, unavailable, stale-generation or
fallback-owned domains cannot return ready. A cleanup receipt never changes
runtime state.

## Validation commitment

The implementation will run:

1. focused Python unit tests for queue admission, structural source
   classification, owner uniqueness, disable/loss, fallback rejection,
   default reachability and causal receipts;
2. TypeScript behavior tests for repaired permission, MCP and provider event
   identities plus adjacent runtime tests;
3. real API readiness integration from a fresh temporary state root;
4. source-owner disconnect and Zyra productization-module disable tests;
5. M3-01A inventory/candidate reruns at the implementation revision, retaining
   unresolved downstream evidence outside this slice;
6. incremental dependency/process/path, OpenClaw, LangGraph and submission
   boundary scans;
7. direct baseline-to-implementation effective-line buckets and individual
   review of every file over 500 additions, over 20 percent of effective
   production, or over 30 percent excluded content;
8. `git diff --check`, Python compile, TypeScript typecheck and affected API,
   runtime, worker and Web regressions.

Changing derived readiness and adding the explicitly assigned fail-closed
composition guard does not transfer a canonical owner or add an external
dependency/process/port. If implementation changes a global default
permission/scheduler/recovery/compact policy, adds a process/dependency, or
changes a public persistence contract, the slice will record and complete the
matching high-risk escalation.
