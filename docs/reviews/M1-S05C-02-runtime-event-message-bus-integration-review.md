# M1-S05C-02 Runtime Event and Message Bus Integration Review

## 1. Review identity

- Slice: `M1-S05C-02`
- Measurement baseline: `439860b222bde3b85b9eefac669b00f1a4314246`
- Implementation commit: `156b6e80b11558a11f29328e17191a210299fba0`
- Review type: incremental critical self-review with exact-commit cleanroom verification
- Result: PASS for this slice
- Parent result: PASS; `M1-05C` is complete after cumulative verification of both slices

This review measures only the uncredited delta after the protected M1-S05C-01 evidence commit.
It treats canonical runtime behavior, durable delivery/projection state, failure effects, and real
CodeWorker/API reachability as evidence. Source inventories, manifests, static imports, and the
historical ledger seed are not counted as implementation evidence.

## 2. Outcome and production boundary

The slice closes the foundation-to-production gap by making the TypeScript event spine the real
runtime integration boundary:

- `RuntimeEventIntegration` accepts versioned source records from CodeWorker, permission, MCP,
  skill, subagent, browser, gateway, artifact, control, compact, API, backend, and recovery
  producers. Unknown source families fail closed; there is no generic event fallback.
- `DurableConsumerRuntime` supplies durable subscription checkpoints, lease/ACK handling,
  idempotence fences, bounded retry, dead-letter state, and explicit consumer mutations.
- Worker, scheduler, control, MCP, memory, artifact, recovery, and audit consumers update their
  own derivative state from canonical events. They do not append an alternative canonical fact
  history.
- `ProjectionDeliveryRuntime` is a dedicated durable bus subscriber. It owns the task view,
  projected history, stream cursor, global projector cursor, rebuild generation, and coverage
  checks; the SQLite fact store no longer invokes the projector directly.
- `RuntimeEventReconciler` compares canonical high-watermarks with delivery and projection
  coverage, repairs missing deliveries, reopens incorrectly acknowledged projector work, and can
  rebuild derivative state from canonical history.
- `ArtifactReadService` resolves only canonical content-addressed references and verifies digest,
  declared size, offset, range, and output encoding. It never falls back to caller paths.
- The Python bridge exposes source admission, projected history/stream, artifact reads, and
  reconciliation through the existing supervised TypeScript process rather than introducing a
  Python store or projector.
- The API task projection/history paths now read the durable projector only. The old raw event
  fold fallback was removed so a disabled or stale projector is visible rather than concealed.
- The default `CodeWorkerRuntime` receives the bridge through runtime services and mirrors real
  typed runtime frames at the frame boundary. It does not parse stdout, logs, or reconstruct the
  Claude-derived query/session owner.

## 3. Canonical admission and source mapping

`zyra.runtime-source-record/v1` and `zyra.runtime-source-batch/v1` carry source owner, source
schema, producer identity, transport sequence, aggregate/session/run/task identity, correlation,
causation, intent, visibility, durability, payload, and source metadata. Source mappers validate
the declared owner and schema before converting to a catalog-owned canonical draft.

The mapping rules preserve the existing state owners:

- Claude-derived query frames retain query/session/tool/compact causality and transport order.
- permission events remain observations of the protected permission runtime and cannot grant
  authority by declaring a sender or event type;
- MCP/skill/subagent frames remain typed and correlated rather than collapsing into display text;
- browser/gateway/artifact events carry opaque references and bounded summaries;
- control input remains an input request until the backend emits a new backend-owned fact;
- recovery/failure facts route to the recovery consumer using the catalog and router policy;
- live-only progress remains non-canonical and receives a bounded TTL.

The live CodeWorker ingress uses the outer transport-frame sequence. Phase-local sequence resets
therefore cannot violate monotonic source ordering. Legacy host events are mirrored only when no
typed runtime frame already supplied the same fact.

## 4. Message bus, consumer, and projector semantics

Canonical commit precedes every derivative action. The bus and projector preserve the following
separation:

1. the store commits the immutable event and selected business route;
2. the bus creates durable delivery work for authorized subscriptions;
3. consumers lease, mutate derivative state, and ACK with a persisted checkpoint;
4. retry/dead-letter cannot erase or rewrite the canonical fact;
5. reconciliation catches up gaps from the canonical high-watermark.

The projector has a dedicated internal subscription rather than a store callback. Enabling a
projector while disabling the bus is rejected by composition: no hidden store-to-projector path
is constructed. Projector coverage compares canonical, timeline, task-view, and cursor counts, so
a superficially advanced cursor cannot hide a missing derivative row. Rebuild deletes only
derived rows, increments the projection generation, and deterministically replays canonical
history.

Duplicate canonical appends remain idempotent. If a previous post-commit failure left a business
delivery missing, retrying the same source record repairs the delivery without duplicating the
fact or aggregate sequence. Catch-up uses the persisted route decision and cannot expand the
original recipient set.

## 5. OMP RPC and low-entropy behavior

The oh-my-pi supplementary boundary now maps real RPC agent frames, including message lifecycle,
assistant progress/final, tool call/progress/result, subagent lifecycle, compaction, cancellation,
and error frames. Correlation IDs, request IDs, tool-call IDs, parent-agent IDs, and final/partial
status survive the conversion. Cancellation cannot invent a successful tool result or silently
fall through to a generic event.

Payload externalization preserves the original source payload for the content-addressed artifact
while keeping the canonical envelope bounded. Artifact reads re-check the stored digest and size;
range reads return explicit byte metadata. The common baseline verifier now applies the same task
completion predicate and denominator to all four policies, and the latency gate fails when p95
exceeds the 4 KiB-event target instead of merely reporting the regression.

## 6. Source-to-target internalization ledger

| Role | Source mechanism | Zyra target and integration responsibility |
| --- | --- | --- |
| Primary: opencode | durable session/event contracts, typed messages, projector/query patterns, provider-side event transport | `integration-contracts.ts`, `source-mapper.ts`, `consumer-runtime.ts`, `projection-delivery.ts`, `integration-runtime.ts`: versioned source admission, canonical-to-delivery flow, durable consumer/projector state, stream cursors, and reconciliation. Zyra owns the schema, store, route, errors, tests, and maintenance boundary. |
| Supplementary: OpenHands | event service subscription semantics and UI/session event folding | `domain-consumers.ts`, `projection-delivery.ts`, `api.py`: durable consumer mutations and projector-backed task/history views. OpenHands contributes folding/consumer semantics only and receives no canonical store or projector ownership. |
| Supplementary: oh-my-pi | agent RPC frames, tool progress/result correlation, subagent/cancel lifecycle | `omp-rpc-mapper.ts` and source admission: typed frame preservation and correlation. It contributes no second bus, session store, or canonical event owner. |
| Conformance: claude-code-best | QueryEngine/session/tool/permission/compact lifecycle | `worker_ingress.py` and `typescript_claude_runtime.py` connect the already-emitted runtime frame boundary to the spine without splitting or replacing the protected query loop. |
| Conformance: LangGraph | stable identity, pending/committed separation, replay correlation, exact-resume expectations | `reconciliation.ts` and projector coverage tests validate narrow recovery properties. No StateGraph, channel, Pregel, Store, ToolNode, stream controller, SDK, or server becomes a production owner. |

The 05C source seed remains a historical planned inventory and was not mechanically rewritten as
proof. This review and its machine-readable evidence are the slice ledger because they identify
the retained mechanisms, exact Zyra paths, runtime reachability, tests, exclusions, and owners.

## 7. State-custody map

| State domain | Canonical owner | Derived owner / boundary |
| --- | --- | --- |
| Runtime fact identity, aggregate/global sequence, immutable envelope | TypeScript `RuntimeEventSqliteStore` | None |
| Business route decision | TypeScript store, committed with canonical fact | bus reads the persisted route for repair; it cannot broaden it |
| Delivery lease, attempt, ACK, retry, dead letter | TypeScript delivery tables through `DurableConsumerRuntime` | consumer mutation is derivative and idempotent |
| Projected timeline, task view, history, stream cursor, rebuild generation | `ProjectionDeliveryRuntime` tables | API/Python are query/transport adapters only |
| Worker/scheduler/control/MCP/memory/artifact/recovery/audit view | dedicated domain consumer state | canonical fact remains the replay source |
| Source transport ordering and producer checkpoint | TypeScript integration/source contracts | Python CodeWorker ingress forwards typed frames and owns no durable event history |
| Artifact bytes | Zyra content-addressed artifact store | canonical event owns the verified opaque reference |
| Query/session/permission/compact runtime state | existing protected Claude-derived owners | event spine observes; it does not take custody |

## 8. Behavioral and disconnect verification

The six new TypeScript integration cases exercise the actual composition root:

- source admission commits one canonical event, routes it, projects it, and invokes the worker
  consumer;
- duplicate retry repairs a missing delivery without duplicating the fact;
- oversized tool payload is externalized and read back only through digest-checked artifact
  service;
- online delivery and rebuild yield equivalent projector state and API history;
- real OMP RPC tool call/progress/final frames retain durable correlation;
- disabling the bus or projector produces explicit missing derivative behavior with no fallback.

The Python integration cases cross the real supervised stdio process and default CodeWorker path.
They verify causal ingress, live-only boundaries, projection-backed APIs, reconciliation, and that
real TypeScript runtime frames reach the canonical spine without log parsing.

Disconnect effects are semantic:

- bus disabled: canonical facts remain, delivery/projector state does not advance;
- projector disabled: task projection/history routes return no derivative state instead of raw
  event fallback;
- missing delivery: reconciliation/canonical retry repairs only persisted-route recipients;
- missing task/timeline row with an advanced cursor: coverage detects and rebuild repairs it;
- corrupt artifact digest or size: artifact read fails closed;
- malformed/unknown source or OMP frame: admission fails before canonical commit;
- CodeWorker bridge absent: the protected worker still runs, but the new canonical event
  integration is observably absent and its integration assertion fails.

## 9. Verification results

### Implementation worktree

```text
.\node_modules\.bin\tsc.exe -p packages\runtime\runtime-event-spine\tsconfig.json
node --experimental-strip-types --test packages/runtime/runtime-event-spine/test/*.test.ts
.\.venv\Scripts\python.exe -m pytest tests\unit\test_runtime_event_spine_foundation.py tests\integration\test_runtime_event_spine_api.py tests\integration\test_runtime_event_message_bus_integration.py tests\integration\test_e01_typescript_runtime_cutover.py tests\unit\test_internalization_ledger_policy_linecount.py -q
.\.venv\Scripts\python.exe -m compileall -q packages\runtime\zyra_runtime\runtime_events packages\workers\zyra_workers\typescript_claude_runtime.py tests\integration\test_runtime_event_message_bus_integration.py
ruff check <changed Python runtime/worker/test files>
git diff --check
```

Results:

- TypeScript typecheck: passed.
- TypeScript behavior: `22/22` passed.
- Python current behavior plus adjacent CodeWorker and ledger policy: `39/39` passed, with one
  pre-existing pytest collection warning for dataclass `TestEntry`.
- Python compileall: passed.
- Ruff on changed Python runtime/worker/test files: passed. A broader directory probe found two
  pre-existing unused imports in untouched `custody.py` and `models.py`; the API application also
  retains unrelated historical lint debt, neither counted as passing nor modified here.
- `git diff --check`: passed.

### Exact-commit cleanroom

The exact implementation commit was archived to
`.tmp/cleanroom-M1-S05C-02-156b6e80b115`. No root source repository or worktree state was copied.
The already-installed locked TypeScript compiler and Bun executable were supplied as toolchain
paths; source and runtime state came from the archive.

- TypeScript typecheck: passed.
- TypeScript behavior: `22/22` passed.
- Python behavior and adjacent regression: `39/39` passed in `113.49s`.
- Forbidden root-source/editable-link/build-context runtime dependency hits: `0`.

Two non-product cleanroom setup attempts are retained as diagnostic evidence. The first omitted
`node_modules`, so process tests failed before runtime admission because Bun was absent (`20`
passed, `19` failed). The second supplied Bun but hit Windows ACL denial in pytest's default temp
and cache roots (`14` passed, `25` setup errors). The final run used the same archived source,
disabled pytest cache, and placed `--basetemp` under the writable workspace; it passed completely.

## 10. Effective line buckets and parent closure

Nonblank additions were counted from
`git diff --unified=0 439860b222bde3b85b9eefac669b00f1a4314246..156b6e80b11558a11f29328e17191a210299fba0`:

- conservative core production: `7,045`;
- API/stdio/Python protocol and wiring: `463`, excluded from the minimum as adapter/integration
  glue;
- governance budget correction: `1`, excluded;
- dedicated and adjacent test additions: `491`, excluded;
- generated, data-as-code, docs, fixture-only, mock-only, vendor-like, source-pool, ledger seed,
  manifest, and adapter-only lines counted as production: `0`.

The conservative `7,045` exceeds the slice minimum of `7,000`. Together with M1-S05C-01's
conservative `9,201`, the parent M1-05C cumulative count is `16,246`, exceeding the `16,000`
parent minimum. Line volume does not replace behavior; the parent closes because both slices also
have real canonical, delivery, projection, API, worker, fault, replay, and cleanroom evidence.

## 11. Adversarial findings corrected before commit

Critical review and behavior tests found and corrected:

- store-side direct projection bypassed durable bus delivery;
- `enableMessageBus=false` could still construct a projector and hide the disabled boundary;
- automatic consumer pumping was a no-op;
- duplicate append could leave a committed fact permanently missing delivery work;
- catch-up examined only one page and could expand beyond the persisted route;
- an ACKed but missing projector row could not be reopened;
- projector cursor coverage could hide missing task/timeline rows;
- artifact externalization could store the normalized draft rather than original source payload;
- artifact queries did not correctly escape SQL LIKE wildcard/backslash input;
- optional `undefined` values crossed JSON serialization boundaries;
- CodeWorker phase-local sequence resets broke source monotonicity;
- live-only pseudo envelopes and optional task/session projection fields were malformed;
- legacy append receipts invented delivery identity and read fields absent from the Python model;
- aggregate owner provenance and source owner validation diverged;
- API compatibility mixed projection kind with projection owner;
- baseline completion/denominator and p95 failure semantics were inconsistent.

Blocking findings remaining within M1-S05C-02 scope: `0`.

## 12. Residual work and final conclusion

The slice does not claim the numeric-stage aggregate review, milestone exit, M2 visual console,
real edge/cloud dispatch, two live cross-domain submissions, or the 2,000-transition sealed
autonomy gate. Those remain assigned to later authoritative execution entries.

M1-S05C-02 and parent M1-05C now have one canonical TypeScript event owner, durable bus-driven
consumers and projector, real runtime/API ingress, deterministic reconciliation/rebuild, verified
artifact retrieval, OMP typed RPC preservation, explicit disable behavior, cleanroom
reproducibility, and no root-source runtime dependency. The protected Claude query/session and
permission owners remain intact; OpenHands and oh-my-pi supplement rather than duplicate them.
