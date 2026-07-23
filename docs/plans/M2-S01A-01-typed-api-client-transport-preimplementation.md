# M2-S01A-01 typed API client and transport preimplementation decision

Date: 2026-07-23

Authority: `docs/milestones/M2-console-demo/slice-01a-01-typed-api-client-transport-contract.md`

Baseline commit: `f53bf78287a2b2a6eec74102e7218d664307c137`

This record is frozen before the first production-code change. It applies the
2026-07-22 effective-code/language/migration gate and fixes the source roles,
language path, canonical owners, and migration modes for this slice.

## Source, language, migration, and owner decision

| Role | Source repository and bounded path | Source language | Zyra target | Target language | Migration mode | Canonical owner after the slice | Preimplementation decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| primary | `opencode/packages/protocol/src/api.ts`, `errors.ts`, `middleware/auth.ts`, `middleware/schema-error.ts`, `groups/session.ts`, `groups/event.ts`, `groups/health.ts` | TypeScript | `packages/core/typed-api-client/src/{protocol,errors,normalizers,version}.ts` | TypeScript | `same_language_adapt` | Zyra typed API protocol and response normalizers | Preserve the single typed protocol, global auth/error boundary, opaque cursor, and structured error behavior; replace OpenCode schemas and session state with Zyra IDs and M1-owned API projections. |
| primary | `opencode/packages/app/src/context/server-sdk.tsx` | TypeScript | `packages/core/typed-api-client/src/{transport,registry,request,retry}.ts` | TypeScript | `same_language_adapt` | Zyra transport registry and request lifecycle | Preserve one client/one transport, reconnect classification, heartbeat/deadline cancellation, and bounded retries; remove Solid context, event cache, and OpenCode SDK dependencies. |
| primary | `opencode/packages/app/src/context/server-sync.tsx`, `server-session.ts` | TypeScript | `apps/web/src/api/{client,lifecycle,task-api}.ts` | TypeScript | `same_language_adapt` | Zyra web client transient request/cursor/receipt lifecycle only | Preserve request de-duplication, generation fencing, opaque cursor ownership, and event-during-request freshness rules. Do not migrate its canonical frontend session store; M2-S01A-02 and M2-S01B own shell and reducer work. |
| primary | `opencode/packages/core/src/util/retry.ts`, `id/id.ts` | TypeScript | `packages/core/typed-api-client/src/{retry,identifiers}.ts` | TypeScript | `same_language_adapt` | Zyra typed request identity and idempotent retry policy | Extend transient classification with HTTP/version/auth/receipt semantics and bind all generated IDs to Zyra prefixes and causal identifiers. |
| supplementary | `OpenHands/frontend/src/api/conversation-service/{conversation-service.api.ts,v1-conversation-service.api.ts}` | TypeScript | `apps/web/src/api/lifecycle.ts` | TypeScript | `same_language_adapt` | Zyra web lifecycle coordinator; M1 remains task/run state owner | Adapt create/cancel/resume and per-session authentication routing into one Zyra client. No second Axios/fetch client and no runtime-specific URL authority are retained. |
| supplementary | `OpenHands/frontend/src/api/event-service/event-service.api.ts`, `sandbox-service/sandbox-service.api.ts` | TypeScript | `apps/web/src/api/{task-api,lifecycle}.ts` | TypeScript | `same_language_adapt` | Zyra request coordinator only | Adapt bounded lifecycle calls and success/error mapping. Sandbox and event state remain server-owned and are not copied into a client cache. |
| supplementary | Existing Zyra `apps/api/zyra_api/main.py` task create/run/cancel routes and M1 task/event/checkpoint stores | Python | `apps/api/zyra_api/{typed_transport.py,main.py}` | Python | `same_language_extend` | Existing M1 store/runtime/event/artifact/control owners | Add version/auth/correlation/idempotency receipt enforcement around existing owners. Do not create a second task store, event log, checkpoint store, or control owner. |
| conformance_only | `oh-my-pi/packages/coding-agent/src/modes/rpc/rpc-types.ts`, `jsonrpc/message-framing.ts`, `modes/acp/acp-client-bridge.ts` | TypeScript | `packages/core/typed-api-client/test/conformance.test.ts` | TypeScript | `conformance_only` | No production owner | Check request/response correlation, explicit cancellation targets, malformed-envelope fail-closed behavior, and capability/auth separation. No RPC/ACP runtime is migrated. |
| conformance_only | Agent Framework AG-UI approval/history envelopes and AgentScope `src/agentscope/app/_router/_session.py`, `_service/_session.py`, `_service/_session_projection.py` plus `examples/web_ui/frontend/src/api/{chat,session}.ts` | Python / TypeScript | `packages/core/typed-api-client/test/conformance.test.ts` | TypeScript | `conformance_only` | No production owner | Compare envelope correlation and lifecycle status semantics only. No workflow, checkpoint, session store, or frontend client is migrated. |
| excluded_forward_only | OpenClaw | N/A | none | none | `excluded_forward_only` | none | Do not read, restore, depend on, test against, or cite OpenClaw as an implementation/conformance source. |

## Fixed implementation boundaries

- `packages/core/typed-api-client` is the only reusable typed protocol,
  normalizer, retry, and transport-registry implementation.
- `apps/web/src/api` is the only browser-facing Zyra API client. It may own
  in-flight requests, deadlines, cancellation signals, opaque cursors,
  retry-attempt journals, and receipt verification. It may not own canonical
  task, run, event, artifact, permission, or checkpoint state.
- `apps/api/zyra_api/typed_transport.py` persists request receipts beside the
  real Zyra store and guards the existing task lifecycle. It does not implement
  task execution itself.
- The default web entry may call the API only through `apps/web/src/api`.
  Direct `fetch`, Axios, a source SDK, or a fallback transport is forbidden.
- Version negotiation fails closed. Authentication is deployment-configurable
  and, when configured, fails closed. Mutating retries require an idempotency
  key and a server receipt.
- Disabling the registered transport or response normalizer must make the real
  task operation fail before state can be fabricated locally.

## Language and effective-code obligations

The primary implementation source is TypeScript and the target is TypeScript,
so non-zero original-language production code is mandatory. The supplementary
OpenHands TypeScript path is also adapted in TypeScript. The existing Zyra API
extension remains Python. No cross-language rewrite exception is requested.

The slice minimum is 6,000 effective production lines. The final evidence will
bucket each changed file into effective behavior, excluded declarations/DTOs,
adapter-only glue, tests, docs, and generated/data/vendor-like content.
Production line volume alone will not close the slice: the implementation must
also pass build, real-store embedded API behavior, duplicate/idempotency,
timeout, authentication, version mismatch, disconnect, malformed-response, and
disable-path tests.

## Commit boundary

- `baseline_commit`: `f53bf78287a2b2a6eec74102e7218d664307c137`
- This decision record is intentionally committed before production code.
- `implementation_commit` will be created only after production code and its
  directly related tests pass, but before the completion report, ledger
  updates, and execution-state update.
- The effective implementation range is
  `baseline_commit..implementation_commit`; documentation is excluded from
  effective production counts.
