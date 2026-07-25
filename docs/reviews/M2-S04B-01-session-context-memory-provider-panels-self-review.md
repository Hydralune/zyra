# M2-S04B-01 Session / Context / Memory / Provider Panels Self-Review

Status: passed incremental critical review

Date: 2026-07-25

Baseline: `af6f6dc0c66a846c739b73e8c6006c67de219ba8`

Decision commit: `26cb8f5`

Implementation commits: `08280a9`, `3604915`, `9cb8272`

Evidence target: `9cb8272`

## 1. Outcome

The slice adds a production-reachable session console whose session lineage,
checkpoint state, context/compact accounting, memory retrieval/curation,
provider/model routing, and device/edge/cloud placement views are derived from
the existing `CanonicalProjectionStore`.

All mutations use the existing `CommandSurfaceRuntime`. The new
`ControlEffectLedger` prevents compact, curator, model-route, resume, and rewind
operations from settling merely because a receipt exists: the later canonical
projection must show the expected context, memory, route, or checkpoint effect.
Read-only commands still require a canonical receipt event.

The implementation does not add a frontend durable store, Python state owner,
external process, port, dependency, MCP server, plugin, dynamic import, Docker
context, or sibling-repository runtime path.

## 2. Source role, language, and custody decision

| Source | Role | Source language | Target language | Migration mode | Bounded result |
| --- | --- | --- | --- | --- | --- |
| `claude-code-best@c57f5a...` | primary implementation | TypeScript/TSX | TypeScript/TSX | retained control-flow adapt plus cropped same-language migration | Session lineage, context categories, compact/restore accounting, retrieval projection, command-to-receipt control flow |
| `opencode@adf178...` | supplementary implementation | TypeScript/TSX | TypeScript/TSX | cropped same-language migration | Reconnect invalidation, provider catalog/fallback presentation, placement explanation |
| `hermes-agent@44ddc...` | supplementary implementation | Python/TypeScript | TypeScript | bounded cross-language typed supplement plus cropped migration | Deterministic curator receipts, provenance/veracity and selected-memory context export |
| LangGraph section 12 | conformance only | Python | tests only | conformance only | Pending/committed write separation, identity, stale/conflict and exact-resume checks |
| browser-use | reference only | Python/TypeScript | tests/review only | reference only | Disconnect, failover and secret-redaction challenge |
| OpenClaw | excluded forward only | none | none | none | No source read, code, test quota, dependency or path |

No new Python facade was added. The preimplementation decision allowed one only
if the existing typed command response could not expose the current owners'
projection. M2-01A/M2-01B already expose the required session, memory,
provider, scheduler, worker, recovery, artifact and event facts. Adding another
Python route would therefore be forwarding-only duplication and would not
provide production credit.

Canonical custody remains:

- frontend truth: `CanonicalProjectionStore`;
- command mutation/receipts: `CommandSurfaceRuntime` and the existing M2-04A
  transport;
- session/context/compact/provider runtime: existing TypeScript
  `QueryEngine`/`ProviderControlPlane`;
- durable session/checkpoint/memory/scheduler/placement/recovery state:
  existing Python stores and control planes;
- browser controllers: transient projection, admission, preview, explanation,
  reconciliation and disconnect state only.

## 3. Internalized Zyra modules and semantic effect

| Zyra module | Runtime responsibility | Canonical input/effect |
| --- | --- | --- |
| `features/session/projection.ts` | Lineage, checkpoint, pending/committed/stale/conflict, context categories and compact preview | Immutable session/event/checkpoint/artifact projections |
| `features/session/checkpoint-ledger.ts` | Checkpoint/epoch/compact receipt reconciliation and exact-resume admission | Stable task/session/checkpoint/revision/epoch identity |
| `features/session/runtime.ts` | Seven real controls, disconnect/reconnect, queued/applied/failed receipt reconciliation | M2-04A command receipt followed by canonical projection |
| `features/session/control-effects.ts` | Proves that a later context, memory, route, checkpoint or artifact effect occurred | Baseline and later canonical projection fingerprints |
| `features/session/coordinator.ts` | Drives session, memory, provider and placement engines from one projection update | One `CanonicalProjectionState`, never feature-local truth |
| `features/memory/**` | Layered retrieval, ranking, provenance/veracity, deterministic curator receipts and restorable later-context blocks | Canonical memory/event/artifact projections |
| `features/providers/**` | Redacted catalog, credential presence, capabilities, quota/rate limits, usage/cost and failover/circuit planning | Canonical scheduler/provider/model/event projections |
| `features/placement/**` | Resource, sensitivity, privacy/SLA admission, model split and migration explanations | Canonical scheduler/worker/recovery/event projections |
| `session-console-workbench.tsx` | Active panel controls and cross-links | Runtime snapshots only; static JSX excluded from quota |

Deleting or disabling these modules changes real behavior:

- disabling `SessionConsoleRuntime` rejects `/context`, `/compact`, `/memory`,
  `/model`, `/resume`, `/rewind`, and `/export` before transport;
- disabling `MemoryContextExporter` prevents later-context restore;
- disabling `ProviderCredentialAuditor` prevents route credential admission;
- disconnect prevents controls, and reconnect rebuilds from canonical truth;
- a compact receipt without a later context mutation remains reconciling;
- removing the provider or placement projection changes fallback/privacy
  admission results.

## 4. Dynamic reachability

`apps/web/src/app/runtime.ts` constructs one `SessionConsoleRuntime` from the
production projection and command runtimes. `task-detail.tsx` mounts
`SessionConsoleWorkbench`. Binding a task calls `SessionBehaviorCoordinator`,
which updates checkpoint/context, retrieval/curator/context-export,
provider/credential/failover/usage, and placement/admission engines from the
same canonical projection. Panel actions call the real runtime methods and the
registered M2-04A commands.

This is not import-smoke reachability: the focused behavior test invokes
projection builders, effect reconciliation, memory context export/restore,
credential admission, privacy/SLA placement and the disconnected controller.
The adjacent workbench test confirms the production React entry and command
coordinator path.

## 5. Behavior and failure-path verification

| Verification | Result |
| --- | --- |
| Focused session/context/memory/provider suite | 9 passed, 54 assertions |
| Canonical projection + permission + recovery + browser + workbench + focused adjacent suite | 107 passed, 562 assertions |
| Web TypeScript typecheck | passed |
| Production Web build | passed; 319 modules bundled |
| Source-ledger synchronization/check | 5 decisions, 5 entries, 0 missing targets |
| Sibling-source/runtime-path scan | 0 production hits |
| Dependency/lockfile diff | no changed dependency or lockfile |

The focused suite proves:

1. parent/fork/resume lineage and pending-versus-committed checkpoints;
2. compact preview retains protected checkpoint references and
   drops/summarizes eligible real segments;
3. compact receipt reconciliation stays pending until canonical context tokens,
   epoch and compact count change;
4. memory search changes selected context and exposes provenance/veracity;
5. selected memory exports to a checksum-bound, exactly restorable later
   context and fails when the exporter is disabled;
6. provider failure produces a fallback candidate without credential values;
7. credential presence admission rejects secret-bearing projections and fails
   when its module is disabled;
8. restricted privacy/SLA rules reject cloud and favor the device;
9. disconnect and module disable make real controls unavailable.

## 6. Effective-line audit

The conservative audit at
`docs/reviews/evidence/M2-S04B-01/effective-lines.json` uses exact Git added-line
sets over `af6f6dc...9cb8272`. It excludes imports/exports, type declarations,
comments/blanks, complete JSX presentation, tests/fixtures, documentation,
command descriptor/result metadata, application composition adapters,
generated/data/vendor/source-pool material and unreachable support.

| Bucket | Lines |
| --- | ---: |
| Raw additions | 11,936 |
| Production runtime | 8,226 |
| Non-JSX UI behavior | 79 |
| Effective production | **8,305** |
| Minimum | **8,000** |
| Type declarations | 1,903 |
| Schema/DTO/data | 282 |
| UI presentation | 426 |
| Adapter-only | 6 |
| Tests/mock/fixtures | 647 |
| Docs/comments/blanks | 367 |
| Generated/vendor-like/source-pool | 0 |

The command registry's 268 lines are conservatively excluded as schema/DTO
metadata. The slice still clears the gate by 305 lines. No file contributes
more than 20% of effective production.

## 7. Triggered per-file review

Files trigger review when raw additions exceed 500 or an excluded bucket is
over 30%. Each triggered file was inspected against its actual responsibility.

| File | Raw / effective | Trigger and review judgment |
| --- | ---: | --- |
| `app/runtime.ts` | 8 / 0 | Composition adapter; all six executable bindings excluded intentionally. |
| `components/tasks/task-detail.tsx` | 3 / 0 | JSX mount only; presentation/type excluded. |
| `features/commands/runtime.ts` | 7 / 0 | Overlay labels are metadata, not behavior. |
| `features/memory/index.ts` | 4 / 0 | Export-only barrel, correctly excluded. |
| `features/memory/projection.ts` | 747 / 583 | Cohesive memory normalization, ranking, provenance/veracity and curator projection; types excluded, no duplicate owner. |
| `features/memory/retrieval-session.ts` | 643 / 531 | Cohesive constraint evaluation, deterministic selection and curator planning; types excluded. |
| `features/placement/admission.ts` | 525 / 413 | Workload admission, scenario comparison and migration feasibility; no scheduler custody. |
| `features/placement/index.ts` | 2 / 0 | Export-only barrel, correctly excluded. |
| `features/placement/projection.ts` | 939 / 752 | One projection boundary for resources, sensitivity, privacy/SLA candidates, split/migration and violations; large but cohesive. |
| `features/providers/failover.ts` | 529 / 426 | Failover/circuit planning and recovery explanation; consumes canonical facts and does not own provider state. |
| `features/providers/index.ts` | 4 / 0 | Export-only barrel, correctly excluded. |
| `features/providers/projection.ts` | 786 / 601 | Catalog/model/route/usage normalization and redaction; one cohesive provider projection. |
| `features/session/checkpoint-ledger.ts` | 757 / 577 | Pending/committed/stale/conflict, epoch and exact-resume ledger; canonical checkpoint semantics remain backend-owned. |
| `features/session/coordinator.ts` | 503 / 402 | Single orchestrator that makes all engines production-reachable from one projection; no duplicate store. |
| `features/session/index.ts` | 10 / 0 | Export-only barrel, correctly excluded. |
| `features/session/projection.ts` | 912 / 741 | Session lineage, checkpoint and context/compact projection are a cohesive state view; no browser persistence. |
| `features/session/runtime.ts` | 763 / 672 | Seven commands, receipt lifecycle, disconnect/reconnect and semantic reconciliation; single controller responsibility. |
| `session/view/session-console-workbench.tsx` | 519 / 79 | 424 JSX presentation lines excluded; 79 event handlers/behavior retained. |
| `session-context-memory-provider-panels.test.ts` | 647 / 0 | Behavior evidence only; all test/fixture lines excluded. |
| `packages/commands/src/contracts.ts` | 10 / 0 | Type-only command union/category declarations excluded. |
| `packages/commands/src/registry.ts` | 268 / 0 | Entire declarative command registry addition excluded as schema/DTO metadata. |
| `packages/commands/src/result-model.ts` | 7 / 0 | Result-title metadata excluded. |

`control-effects.ts` (400 raw/313 effective), `context-export.ts` (432/341)
and `credential-audit.ts` (379/316) do not independently trigger the threshold,
but were reviewed because they close critical semantic-effect, restore and
secret-admission gaps. Their tests include pending/committed and disable
failure behavior.

## 8. Dependency, clean-directory, and forward-boundary review

Production-path scan covers `apps/web/src` and `packages/commands/src` for
sibling source paths, `vendor-runtimes`, `source-pool`, `runtime-sources`, npm
links, editable parent paths and OpenClaw. It returns zero hits. No package or
lockfile changed. The slice therefore introduces no root-source runtime
dependency or hidden process/package owner.

This ordinary slice does not hit a high-risk escalation trigger: it does not
change a public persistence schema, transfer a canonical owner, change a global
default policy, or add an external dependency/process. Full cleanroom and
all-repository validation remain assigned to the M2-04 digital-stage aggregate
review. Focused and adjacent real behavior, build, source ledger, dependency
scan and disconnect/disable proof were completed here as required.

## 9. Critical findings and disposition

- Initial effective production was only 7,575 lines. The slice was not marked
  complete. Real memory-context export and credential admission were added.
- The next audit was 8,247 but relied on a narrow margin that included command
  registry semantics. The registry was conservatively reclassified as schema,
  and canonical semantic effect verification was added instead.
- Final conservative effective production is 8,305.
- `credential_present` was initially treated as a secret-bearing key by the
  recursive audit. The scanner now recognizes only boolean/redacted
  presence-only metadata while still rejecting real credential, authorization,
  API-key, token and private-key values.
- No unresolved blocker remains for this slice. `M2-S04B-02` remains the parent
  unit's next slice; this review does not close M2-04B.
