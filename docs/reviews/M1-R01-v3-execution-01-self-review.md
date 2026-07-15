# M1-R01 v3 Execution-01 Critical Self-Review

- Execution: `E01 runtime core TypeScript cutover`
- Verified baseline: `c34535a783e88f9481387ced89cba4fbc333dc74`
- Implementation candidate: `d6ff45435167c04c132e08e3de275a24cf28b544`
- Review scope: `apps/code-worker/src/**` and `packages/runtime/claude-runtime/src/**`
- Verdict at this checkpoint: implementation complete, independent review still required

## 1. Scope and cross-execution exclusions

This candidate replaces the E01 runtime-core execution path. It does not claim later remediation work as E01 output.

`packages/runtime/runtime-event-spine/src/**` is explicitly excluded from E01 production roots and contributes zero E01 production-line credit. That package belongs to the later `M1-S05C-01` event-spine work. The candidate verifier, dependency probe, source-to-target evidence, and effective-line report use only the two E01 roots listed above.

Generated files, data-as-code, fixtures, mocks, documentation, source pools, vendor-like copies, ledgers, manifests, and adapter-only shells receive zero effective-production credit.

## 2. Effective line accounting

The candidate gate reports:

| Bucket | Lines | E01 production credit |
| --- | ---: | ---: |
| Accepted source physical SLOC | 11,027 | source basis only |
| Effective changed TypeScript SLOC | 25,605 | 25,605 |
| Required effective changed SLOC | 25,416 | threshold |
| Final non-test TypeScript SLOC | 34,642 | corroborating inventory |
| Behavior-test SLOC | 8,099 | test evidence only |
| Adapter-only ratio | 0 | no adapter-only production credit |

The effective changed total exceeds the E01 threshold by 189 lines. The narrow margin is intentional rather than padded: the verifier rejects repeated generic journal templates, duplicate normalized bodies, source-pool material, generated/data content, and inactive roots.

## 3. Default provider execution custody

The default `QueryEngine.run` path now owns one continuous provider execution chain:

1. `QueryEngine.run` invokes `configureProviderRuntime`.
2. `ProviderCredentialRuntime` registers, rotates, selects, and resolves the selected credential. Anonymous credentials remain header-free rather than manufacturing a secret.
3. `ProviderModelRuntime.prepareRequest` normalizes the model request and binds provider/model identity.
4. The model-stream callback invokes `executePreparedProvider`.
5. `ProviderModelRuntime.execute` delegates the prepared request to `ProviderTransportRuntime.execute`.
6. Transport response, routing outcome, request/response accounting, rate-limit state, and execution-custody correlations settle on the same path.

The direct `fetch` branch in model-stream remains only as a compatibility fallback for standalone callers that invoke `resolveModelTurns` without the QueryEngine execution callback. It is not reachable from the default QueryEngine path. Mutation `e01-mut-033-provider-execution-callback` proves that removing the callback breaks the required default behavior.

## 4. Tool effect and result custody

The real tool path is no longer journal-only:

1. `planToolBatches` creates executable batches.
2. `ToolExecutionRuntime` schedules the invocation, grants a lease, and records the running attempt.
3. The actual host boundary executes through `host.executeBatch`.
4. `ToolExecutionRuntime` settles success or failure from the real host result.
5. `ToolResultRuntime` records and delivers the result.
6. `DurableSessionRuntime` commits or fails the tool effect against the canonical turn.
7. `E01ExecutionCustodyRuntime` rejects missing or inconsistent invocation, lease, effect, result, delivery, turn, and message correlations.

Mutation `e01-mut-034-execution-custody` proves that bypassing the custody runtime changes observable behavior and is killed by the behavior suite.

## 5. Canonical session and restart ownership

`DurableSessionRuntime` is the canonical owner for turns, tool effects, and messages. The older `RuntimeSession` remains a context projection and is not a second durable state owner.

The composite checkpoint format is `zyra.e01-runtime/v6`. It includes checksummed snapshots for query execution, tool execution, tool results, provider request/response/routing/rate/credential/model/transport state, durable session state, and E01 cross-owner execution custody. Secret material is not serialized.

The same-session resume probe performs real cross-process recovery:

- three distinct worker PIDs are started;
- each process is externally terminated with exit code 143;
- two successive restarts restore revisions `2 -> 4 -> 6`;
- journal epochs advance `0 -> 1 -> 2`;
- custody epochs advance `0 -> 1 -> 2`;
- six epoch-local transition IDs are unique, with zero replay;
- the probe uses system temporary storage outside the repository and removes it afterward.

During this verification, the probe exposed a real second-restart defect in `QueryExecutionPlanRuntime.restore`: the restart epoch changed without a matching state-digest transition. The candidate fixes this by recording a `plan.restored` transition and advancing revision/sequence state.

## 6. Source-to-target coverage

The frozen five-hop evidence contains 229 accepted rows covering 229 source occurrences and 33 unique source symbols, with no symbol accounting for more than 43 rows. Each accepted row records:

1. source file and symbol;
2. Zyra target symbol;
3. default call site;
4. state or execution effect;
5. behavior-test assertion.

Provider-source symbols terminate in the actual credential/model/transport/custody call chain. Tool and session symbols terminate in the actual host execution, result delivery, durable effect, and restart paths. A ledger or manifest lookup alone is not accepted as a behavior hop.

## 7. Validation results

All results below apply to the exact implementation commit named above.

| Gate | Result |
| --- | --- |
| TypeScript typecheck | PASS |
| Default runtime behavior tests | PASS, 9/9 |
| Full E01 behavior suite | PASS, 364/364 with 1,156 assertions |
| Mutation gate | PASS, 34/34 killed |
| Production build | PASS, 73 modules |
| Built runtime health | PASS |
| Runtime-origin probe | PASS, TypeScript `v6` runtime |
| Write-path probe | PASS |
| Lost-ACK probe | PASS |
| Disable/negative-control probe | PASS |
| Dependency/path probe | PASS, 88 files and no forbidden links |
| Same-session cross-process resume | PASS |
| Candidate verifier | PASS |

The pinned toolchain is Bun `1.2.15` and TypeScript `5.8.3`.

The external cleanroom was created with `git archive` from the exact implementation commit into system temporary storage outside the repository. It had no `.git`, no pre-existing `node_modules`, no ancestor `node_modules`, an empty `NODE_PATH`, and a fresh isolated Bun cache. A frozen install fetched 13 packages, after which typecheck, build, all 364 tests, and built health passed. The cleanroom, cache, and archive were removed afterward.

## 8. Dependency and ownership findings

- The default runtime has no Python owner path.
- The E01 roots have no runtime dependency on `../claude-code-best`, another source repository, a vendor/source-pool tree, an npm link, an editable path, an external Docker context, or a residual repository cache.
- Provider, tool, session, and custody state is owned by Zyra TypeScript modules and restored from the v6 composite checkpoint.
- Runtime events are caused by real provider/tool/session mutations rather than fixed health output or fixture replay.
- Removing the provider execution callback or execution-custody integration is detected by the mutation suite.

## 9. Critical residual assessment

The 189-line effective-SLOC margin is small and should be independently recalculated rather than trusted from this report. The reviewer must also verify that the 229 five-hop rows point to distinct executable semantics rather than repeated documentation, that the 34 mutations alter production behavior, and that the cleanroom target matches the implementation candidate exactly.

This self-review does not mark E01 verified. `execution-state.yaml` must retain the previous verified head until a fresh independent reviewer returns PASS against the anchored candidate and its frozen evidence.
