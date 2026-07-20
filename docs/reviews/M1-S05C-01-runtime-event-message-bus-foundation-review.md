# M1-S05C-01 Runtime Event and Message Bus Foundation Review

## 1. Review identity

- Slice: `M1-S05C-01`
- Measurement baseline: `c34535a783e88f9481387ced89cba4fbc333dc74`
- Implementation commit: `62d355675c0656c5714636c962a42746d7ad734a`
- Review type: incremental critical self-review with upgraded clean-source-tree verification
- Result: PASS for this slice only
- Parent status: `M1-05C` remains in progress; `M1-S05C-02` is still required

The first version of the event-spine files was checkpointed in `0cd21bff` but was never
credited as a completed slice. This review therefore measures the complete retained production
boundary from the last protected M1-S05B-02 evidence commit through the final M1-S05C-01
implementation commit. It does not treat the historical checkpoint, a ledger, a manifest, or
file presence as completion evidence.

## 2. Outcome and production boundary

The slice establishes one Zyra-owned canonical runtime event spine instead of parallel session,
UI, worker, and message-bus histories:

- `@zyra/runtime-event-spine` owns the versioned event envelope, event catalog, append-only SQLite
  fact store, global cursor, route-decision records, projector cursors, durable delivery state,
  ACK/replay/catch-up behavior, payload policy, routing, metrics, and stream normalization.
- `RuntimeEventSqliteStore` is the canonical persistence owner. Canonical append and the selected
  route decision are committed together; projector and subscriber work happen after that commit
  and can be repaired without erasing or rewriting the fact.
- `RuntimeEventSpine` supplies the production composition root for catalog validation, canonical
  persistence, projection, routing, delivery, repair, replay, and baseline metrics.
- `zyra_runtime.runtime_events` is the Python process boundary used by the real API and worker-side
  runtime. It maps Zyra models and errors, supervises the TypeScript process, checks custody, and
  folds OpenHands-compatible observations. It does not create another canonical event store.
- The API task-creation path appends the canonical fact before updating the legacy JSONL
  compatibility projection. Per-event projection prevents a partially failed batch from
  advertising facts that were never committed canonically.
- Server shutdown closes the TypeScript event process and its SQLite handle, which removes the
  Windows file-lock leak found during integration testing.

## 3. Canonical envelope and causality

Every accepted fact has a catalog-owned event type/version, stable event identity, aggregate
identity and sequence, globally monotonic sequence, correlation and causation fields, actor and
sender identity, intent, durability, visibility, payload summary, artifact references, and
canonical timestamps. Validation occurs before commit.

Legacy normalization is deliberately constrained:

- process-global events receive the explicit `runtime-global` aggregate rather than an empty ID;
- inferred causation is limited to the same aggregate and correlation, so unrelated runs cannot
  acquire a fabricated parent;
- unknown input becomes a typed observation rather than caller-selected control authority;
- malformed MCP output is summarized as a typed fact without inserting the raw result into the
  canonical payload;
- UI control input produces a new backend-owned record and cannot forge a canonical sender,
  event version, route, or broadcast authority.

## 4. Persistence, projection, and bus semantics

The event store assigns both aggregate sequence and global sequence under SQLite transaction
control. Event ID and idempotency-key duplicates return the existing fact rather than advancing
either sequence. Queries use the global cursor, so equal aggregate-local sequence numbers from
different tasks cannot be skipped.

Route decisions are durable facts containing policy, fanout, recipients, and a digest. Targeted
messages expose the body only to the explicit recipient. Critical broadcast requires an exact
allowlist match, trusted provenance, and a non-empty reason. Recipient requirements cannot be
self-satisfied by sender claims.

Projection and delivery are derived, independently repairable responsibilities:

- projector failure leaves the canonical fact intact and rebuilds from the last durable cursor;
- subscriber backpressure does not roll back the canonical append;
- durable catch-up creates missing delivery records from canonical history;
- ACK state and replay are persisted and queried through the same TypeScript owner;
- wildcard routing works at any intent segment, including `system.failure.*` recovery routes.

## 5. Low-entropy payload and comparison baseline

The production policy enforces measured serialized bytes rather than trusting caller-declared
sizes:

- inline payload: at most 4 KiB;
- complete canonical envelope: at most 8 KiB;
- payload summary: at most 1 KiB;
- artifact references: at most 64 per event.

Raw transcripts and oversized values are content-addressed and represented by opaque artifact
URIs. They are not silently merged back into the inline payload. The store revalidates actual
bytes immediately before canonical commit, preventing a forged `inline_bytes` declaration from
bypassing the limit.

`LowEntropyBaselineHarness` executes the same immutable workload under targeted-reference,
static-reference, full-broadcast-reference, and full-broadcast-text policies. Each policy uses
real independent recipients and measured transmitted bytes, followed by the same result
verifier. The report exposes targeted delivery ratio, broadcast count, fanout, duplicate facts,
artifact offload, inline bytes, transmitted bytes, latency, and verified task success. The API
route `/runtime-events/baselines` runs this harness; it does not manufacture a comparison from a
closed-form formula or accept caller-injected success.

## 6. Stream and tool lifecycle preservation

The oh-my-pi supplementary normalizer preserves typed turn, tool-call, tool-result, partial,
final, subagent, and compaction frames. Request identity and causation survive the fold, while
partial and final frames remain distinguishable. Tool-call/result pairing is validated instead
of being inferred from display order. OpenHands-compatible folding remains a projector input and
does not write around the canonical store.

## 7. Source-to-target role and custody

| Role | Source mechanisms | Zyra target and retained responsibility |
| --- | --- | --- |
| Primary: opencode | `packages/core/src/event.ts`, `session/event.ts`, `session/projector.ts`, `session/sql.ts`; `packages/schema/src/session-event.ts`, `session-message.ts` | `packages/runtime/runtime-event-spine/src/**`: typed facts, durable event history, projection, query, and message contracts. Zyra intentionally separates canonical commit from repairable projection/delivery instead of copying a transaction that would let a derived failure reject the fact. |
| Supplementary: OpenHands | `event_service.py`, `event_service_base.py`, frontend `use-event-store.ts`, `handle-event-for-ui.ts` | `runtime_events/openhands_fold.py` and the API facade: event fold, stream/final handling, and UI-compatible projection. OpenHands receives no canonical persistence or routing ownership. |
| Supplementary: oh-my-pi | agent loop/types/agent/append-only-context; coding-agent task/executor/isolation; RPC/ACP/collab frames | `normalizers.ts`, `stream-fold.ts`, and `tool-pair.ts`: typed streaming, request correlation, tool pairing, subagent lifecycle, and compaction frames. It receives no second store or bus. |
| Conformance/reference: claude-code-best | Query/session/tool/permission event boundary | Existing Claude-derived runtime can emit and query these facts, but its protected session and permission owners are unchanged and it does not become a second event owner. |
| Conformance/reference: LangGraph | stable identity, lineage, pending/committed-write and exact-resume contracts | No StateGraph, channel, Pregel, stream controller, Store, or ToolNode production migration. The event spine remains Zyra-owned and supports future immutable checkpoint conformance. |

No root source repository, upstream CLI, external service, source pool, or vendored runtime is
loaded by the completed path. The TypeScript mechanisms were retained in their native language
inside a Zyra package; the Python layer owns process supervision and schema/error integration.

## 8. State-custody map

| State domain | Canonical owner | Derived/compatibility boundary |
| --- | --- | --- |
| Runtime facts and global ordering | `RuntimeEventSqliteStore.events` | None |
| Routing decision | `RuntimeEventSqliteStore.route_decisions`, committed with the fact | Router computes a decision but does not persist an alternative history |
| Projection cursor and rebuild status | projector tables owned by the TypeScript store | OpenHands/UI/API views are projections only |
| Subscriber delivery, ACK, replay, catch-up | TypeScript delivery and dead-letter tables | Python exposes RPC methods; it does not duplicate queue state |
| Metrics and low-entropy comparison | TypeScript event metrics/baseline harness | API serializes the measured report |
| Legacy task JSONL | compatibility projection after each successful canonical append | Never authoritative; failure cannot precede or fabricate a canonical event |

## 9. Behavioral verification

### TypeScript package in the implementation worktree

Commands:

```text
node_modules\.bin\tsc.cmd -p packages\runtime\runtime-event-spine\tsconfig.json --noEmit
node --experimental-strip-types --test packages\runtime\runtime-event-spine\test\runtime-event-spine.test.ts
```

Result: typecheck passed; `16/16` tests passed.

The tests exercise global cursors, catalog authority, real four-policy baseline execution,
payload budgets and spill, targeted privacy, canonical survival under backpressure, durable
catch-up, projector recovery, forged UI/broadcast rejection, capability-spoof rejection, typed
OMP frames, malformed MCP folding, backend-owned control records, global legacy identity,
wildcard recovery, and revalidation of caller-declared bytes.

### Python/API path in the implementation worktree

Command:

```text
.\.venv\Scripts\python.exe -m pytest tests\unit\test_runtime_event_spine_foundation.py tests\integration\test_runtime_event_spine_api.py -q
```

Result: `4 passed`.

The tests cross the real Python-to-TypeScript process boundary and verify query, projection,
delivery, ACK, replay, global cursor semantics, real task creation, metrics/baseline API routes,
and JSONL compatibility behavior after a partial canonical batch failure.

### Exact-commit clean-source-tree verification

An archive of `62d355675c0656c5714636c962a42746d7ad734a` was extracted into
`.tmp/cleanroom-M1-S05C-62d3556`; dependencies were installed from the frozen lock with Bun
`1.2.15`. The same TypeScript typecheck/test and targeted Python suites passed there:

- TypeScript: typecheck passed; `16/16` tests passed.
- Python: `4 passed in 9.07s`.
- Exact checked-out commit: `62d355675c0656c5714636c962a42746d7ad734a`.
- Forbidden runtime dependency hits: `0`.
- Root source repositories present: `0`.
- Frozen package lock: true.
- Runtime-event SQLite residue before the test: `0`.

This upgraded check was used because the slice establishes a public event schema, persistent
SQLite state, and a supervised TypeScript subprocess. The archive still contains pre-existing
tracked historical vendor directories, but the new package has no import, editable path, npm
link, build context, or runtime lookup into them.

## 10. Disconnect and semantic-effect evidence

- Removing the canonical store makes the Python/API integration test fail before JSONL
  projection, proving JSONL is not a fallback owner.
- Disconnecting route persistence loses the route-decision/idempotency assertions and durable
  catch-up behavior.
- Disconnecting the projector makes the recovery/rebuild test fail; forcing the projector to
  fail proves the canonical fact itself remains available.
- Disconnecting the router or weakening targeted delivery exposes the body to the non-target
  subscriber and fails the privacy assertion.
- Disconnecting store-side byte measurement makes the forged-byte test accept an oversized
  payload.
- Disconnecting the OMP and MCP normalizers loses typed lifecycle and bounded malformed-result
  facts.
- Disconnecting the TypeScript process/API boundary fails real task creation, query, projection,
  metrics, baseline, poll, ACK, and replay coverage.

These are state and behavior changes, not import smoke tests, fixture replay, or ledger checks.

## 11. Effective line buckets

`git diff --numstat c34535a783e88f9481387ced89cba4fbc333dc74 62d355675c0656c5714636c962a42746d7ad734a`
reports the complete uncredited implementation boundary:

- TypeScript production source additions: `6,914`.
- Python production-boundary additions: `2,831`.
- Gross Zyra-owned production additions: `9,745`; deletions: `0`.
- Conservative counted production: `9,201`.
- Adapter-only `typescript_port.py` excluded from the minimum: `455`.
- Package export surface `__init__.py` excluded from the minimum: `89`.
- Dedicated test additions: `755`; excluded from production.
- Mixed shared API file changes: excluded from the minimum.
- Generated, data-as-code, fixture-only, mock-only, vendor-like, and source-pool additions counted
  as production: `0`.
- Documentation and this review: excluded.

The conservative `9,201` production lines exceed the slice minimum of `9,000`. Models,
integration, custody, API facade, and OpenHands fold remain counted because they own real Zyra
schema validation, process/error mapping, custody checks, event folding, and production routing;
they are not fixed-contract or pass-through adapters.

## 12. Adversarial findings corrected in this slice

The review and targeted tests found and corrected the following blocking defects before the
implementation commit:

- aggregate-local cursors could skip facts from another aggregate;
- catalog version and sender claims were not authoritative before commit;
- route decisions and critical-broadcast provenance were insufficiently durable;
- sender claims could satisfy recipient capability requirements;
- subscriber/projector failure could incorrectly affect canonical success;
- caller-declared bytes could bypass payload limits;
- raw inline payload could be reconstructed after offload;
- catch-up did not create the missing durable delivery;
- low-entropy comparison used derived values rather than executing equal workloads;
- normalizers lost or fabricated lifecycle/causation information;
- Python poll/replay method names and custody-table checks diverged from the TypeScript runtime;
- API JSONL projection could get ahead of a partially failed canonical batch;
- API shutdown leaked the event subprocess and SQLite handle on Windows.

Blocking findings remaining within M1-S05C-01 scope: `0`.

## 13. Adjacent diagnostics and residual work

Selected protected CodeWorker/control/checkpoint tests were also run diagnostically. Their
remaining failures are existing M1-R01/E02/E04 contract mismatches: a missing protected runtime
export, `config_snapshot_digest_mismatch`, an absent E02 route, and older expectations for
permission/restore/control-command shapes. The event integration specifically corrected the
SQLite lifecycle failure found in those runs; an isolated real `/change` request then completed
with HTTP `201`, while the old test still expected the protected legacy event label. These
unrelated failures are not counted as passing evidence and this slice does not rewrite protected
owners to satisfy stale expectations.

Full-repository regression remains assigned to the applicable numeric-stage aggregate or
milestone-exit review. `M1-S05C-02` must still deepen API/worker stream and subscription
integration and close the parent-unit cumulative behavior/line target. This slice makes no claim
that live edge/cloud dispatch, 2,000-transition autonomy, or the remaining competition evidence
gates are closed.

## 14. Final adversarial conclusion

M1-S05C-01 now has one durable TypeScript canonical owner, one measured low-entropy message
policy, repairable projection and delivery state, typed cross-runtime normalization, real API
reachability, failure-path coverage, a clean-source-tree exact-commit check, and conservative
production volume above the slice threshold. It does not establish a second session, permission,
checkpoint, UI, or bus owner and does not rely on an external source repository at runtime.

