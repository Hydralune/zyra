# M1-R01 v3 Execution-01 Critical Self-Review

- Execution: `E01 runtime core TypeScript cutover`
- Verified baseline: `c34535a783e88f9481387ced89cba4fbc333dc74`
- Implementation target: `6dc12a5cd68f18a85c286331fcb784b05434a2aa`
- Evidence commit: `30be0d0ad7a55cf4d71d870d0e17d4191dc2b9dd`
- Prior failed candidate: `df2937406e639862ec6a29795016b51651ee9cc0`
- Review scope: E01-owned code under `apps/code-worker/src/**` and `packages/runtime/claude-runtime/src/**`, with later-execution owners excluded by the verifier
- Verdict at this checkpoint: implementation complete; a fresh independent PASS is still required

## 1. What changed after the independent FAIL

The review at `7c06e937bad6d3d8b3b0cbdc539d42fd2f163027` rejected the previous candidate. This candidate does not reuse that verdict or advance the verified head. It closes each reported defect:

| Failed-review finding | Current correction | Independent evidence |
| --- | --- | --- |
| G0 was dirty and target hashes were null | G0 is captured before evidence writes from a clean implementation HEAD; every accepted target row carries a real SHA-256 | `g0-summary.json`, schema gate, candidate gate |
| Effective changed TypeScript was below 25,416 | Added real provider iteration, compatible protocol, permission enforcement, and execution-settlement modules; AST accounting excludes declarations, repeated bodies, generated/data/vendor-like content, and later-execution owners | `effective-loc-report.json`: 25,722 effective lines |
| Mutation hashes were stale | The generator imports the exact mutation definitions and shared patch-fingerprint function | `mutation-results.json`: 44 declared, 44 applied, 44 killed, restored hashes match |
| Five-hop evidence omitted tool/session and rejected samples | Source domains now include tool orchestration, tool result storage, session state, restore, and history; rejected source rows remain explicit but receive no target mapping | 322/322 complete chains, 40 target symbols |
| Default HTTP provider path failed and did not revise after tools | The default path uses an OpenAI-compatible chat-completions request/response protocol, assembles SSE tool deltas, executes tools, appends tool observations, and performs the next provider round | Runtime tests assert two HTTP calls and a `role: "tool"` observation in the second request |
| Denied or ask-gated tools still reached the gateway | `PermissionEnforcementRuntime` partitions allow/deny/ask, and `PermissionedCapabilityHost` delegates only the exact allow set; blocked calls receive synthetic receipts | Adversarial tests and mutations 037/043 |
| Transport cleanup was not trustworthy | A mutation exposed that first-use endpoint counters were applied to a defensive clone; `execute()` now reacquires the canonical endpoint before slot accounting | Mutation 035 now fails without iterator-finally cleanup |
| Baseline and candidate identities were conflated | Verified baseline, implementation target, evidence commit, candidate anchor, and historical failed candidate are separate state fields | `execution-state.yaml` and G0 receipts |

## 2. Effective line accounting

The enforced candidate gate reports:

| Bucket | Actual | Required | Credit rule |
| --- | ---: | ---: | --- |
| Accepted source physical SLOC | 13,141 | reference only | Source basis, not Zyra production credit |
| Accepted source executable SLOC | 13,046 | 10,587 | Executable accepted source only |
| Effective changed TypeScript SLOC | 25,722 | 25,416 | AST-filtered E01 production credit |
| Final non-test TypeScript SLOC | 35,575 | 28,000 | Corroborating final inventory |
| Effective behavior-test SLOC | 8,664 | 7,000 | Test evidence only |
| Adapter-only ratio | 0 | at most 0.10 | Thin adapters receive no production credit |

The effective production margin is 306 lines. This is deliberately narrow and must be independently recalculated. Generic journal templates, equivalent normalized bodies, type/interface-only spans, uninitialized fields, generated files, data-as-code, mocks, fixtures, source pools, vendor-like copies, and inactive execution roots receive no production credit.

## 3. Default provider and model-iteration custody

The default `QueryEngine.run` path owns a continuous loop rather than a one-shot provider call:

1. Provider credentials and model policy prepare a request.
2. `ProviderTransportRuntime` sends `/v1/chat/completions` with the selected compatible credential.
3. `compatible-runtime.ts` parses JSON or SSE and incrementally assembles tool-call arguments.
4. `ModelIterationRuntime` records the provider round and rejects duplicate transitions or repeated tool-call IDs.
5. Tool calls execute through the permissioned host and settlement owners.
6. Tool receipts become provider-visible observation messages.
7. The next provider round revises from those observations and produces the final answer.

`ModelIterationRuntime` has a checksummed `zyra.model-iteration/v1` snapshot. Its transcript is prefix-stable across restart, transition IDs cannot replay, and protocol failures settle the transport slot. Mutations 033, 036, 038, 039, 040, 042, and 044 each remove a distinct part of this chain and are killed.

## 4. Permission and tool-execution custody

`PermissionEnforcementRuntime` is the generic protocol owner for allow, deny, and ask decisions. `PermissionedCapabilityHost` applies those decisions before `gateway.executeBatch` and delegates exactly the allow subset. Missing, extra, duplicate, or mismatched gateway receipts are rejected or synthesized according to the protocol.

`ToolExecutionSettlementRuntime` owns batch/call lifecycle, permission records, delegated-call fences, gateway receipts, request-order restoration, progress budgets, restart fencing, and checksummed snapshots. It prevents a blocked call from acquiring a gateway effect and prevents a restored in-flight side effect from being replayed as success.

This is semantic enforcement, not event-only evidence: disabling the permission partition or settlement fence changes gateway calls and behavior tests fail. Mutations 037, 041, and 043 cover those boundaries.

## 5. Canonical session and exact resume

`DurableSessionRuntime` remains the canonical owner for turns, tool effects, and messages. `RuntimeSession` is a projection, not a second durable owner. The composite checkpoint is `zyra.e01-runtime/v6` and now includes model-iteration, permission-enforcement, execution-settlement, provider, query, tool, result, session, and cross-owner custody state.

The same-session probe starts three worker processes and externally terminates each predecessor. It restores revisions and restart epochs without replaying transition IDs. The lost-ACK probe proves a committed outbox effect is redelivered but not recommitted or repeated. Snapshot checksum, identity, and transition uniqueness failures are covered by behavior and mutation tests.

## 6. Source-to-target evidence

The frozen source manifest contains 360 source ranges: 322 accepted and 38 rejected. Only accepted rows receive target mappings. The five-hop output contains 322 mappings and all 322 are complete:

1. source file and executable symbol;
2. Zyra target symbol with non-null content hash;
3. default runtime call site;
4. state mutation or execution effect;
5. behavior-test assertion and killing mutation.

There are 40 unique target symbols and no symbol owns more than 43 mappings. Provider, tool, session, compact/restore, and history sources terminate in executable runtime effects. Ledger presence, manifest lookup, fixed health output, and fixture replay are not accepted as a behavioral hop.

## 7. Validation on the exact implementation target

| Gate | Result |
| --- | --- |
| G0 clean baseline receipt | PASS |
| Schema and target-hash gate | PASS |
| TypeScript `typecheck:e01` | PASS |
| Full E01 behavior suite | PASS, 381/381 with 1,219 assertions |
| Mutation gate | PASS, 44/44 killed |
| Production build | PASS, 77 modules, 1.14 MB bundle |
| Built runtime health | PASS |
| Runtime-origin probe | PASS, TypeScript v6 owner |
| Write-path census | PASS |
| Same-session cross-process resume | PASS |
| Lost-ACK probe | PASS |
| Disable/negative-control probe | PASS |
| Dependency/path audit | PASS, 93 files, no forbidden path/link |
| Candidate verifier | PASS |
| Fresh external cleanroom | PASS |

The pinned toolchain is Bun `1.2.15` and TypeScript `5.8.3`. The cleanroom uses `git archive` of `6dc12a5cd68f18a85c286331fcb784b05434a2aa`, extracts outside the repository, starts without `node_modules`, `dist`, `.tmp`, Git metadata, ancestor dependency resolution, or `NODE_PATH`, performs a frozen install into a fresh cache, and then passes typecheck, build, all 381 tests, and built health.

## 8. Dependency and ownership assessment

- No Python runtime owner path remains in the E01 default path.
- The E01 roots do not depend at runtime on `../claude-code-best`, another root source repository, vendor/source-pool trees, npm links, editable paths, external Docker contexts, or repository cache residue.
- Provider iteration, permission enforcement, tool settlement, durable session, and transport state are held by Zyra TypeScript schemas and snapshots.
- Runtime events correlate to real provider rounds, tool calls, receipts, session mutations, and checkpoint effects.
- The disable probe proves the default entry fails when the TypeScript E01 owner is removed.

## 9. Residual risks for the independent reviewer

- Recompute the 25,722 effective changed lines rather than trusting the generated report.
- Sample accepted and rejected source ranges and verify target hashes and all five hops without relying on ledger claims.
- Re-run reviewer-owned adversarial probes for provider observation revision, deny/ask non-delegation, transport slot cleanup, duplicate transition IDs, and restart fencing.
- Confirm the clean G0 target, cleanroom target, evidence commit, and candidate review target are distinct but correctly ordered descendants of the verified baseline.
- Confirm the mutation runner restored every production file and that its shared patch fingerprints match the frozen manifest.

This self-review does not mark E01 verified. `verified_zyra_head` must remain `c34535a783e88f9481387ced89cba4fbc333dc74` until a fresh independent review commits a PASS report against the final candidate anchor.
