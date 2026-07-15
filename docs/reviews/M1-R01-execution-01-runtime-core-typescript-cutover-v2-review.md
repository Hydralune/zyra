# M1-R01 Execution 01 Runtime Core TypeScript Cutover v2 Review

## Verdict

`implementation_complete_review_pending`

E01 has an implementation/evidence candidate. It is not independently verified. E02 remains blocked and was not started. The next entry is `docs/执行单元独立复审任务书.md`.

## Baseline and authority

- Direct implementation baseline: `0cd21bff5e2d160476f2ce3cef766bf53aab1239`.
- Prior verified Zyra head: `c34535a2dc5b80e118660d66dc1bf2243f418353`.
- The baseline deviation is intentional: the user directed committing the pre-existing runtime event work before E01 continued.
- The remediation `manifests/` directory does not exist. Under `M1-R01-FINAL-EXECUTION-AUTHORITY`, manifests are optional, non-authoritative evidence. Every threshold in this review is recomputed from files and the baseline diff.

## Runtime custody result

- `apps/code-worker/src/main.ts` is the default stdio/health entrypoint.
- `packages/runtime/claude-runtime/src/query-engine.ts` invokes `E01RuntimeCoordinator` on the real query path and embeds the E01 journal snapshot.
- Forty TypeScript domain owners cover query, input, context, tools, compact, provider, session and commit protocol.
- `packages/runtime/claude-runtime/src/e01/kernel.ts` owns stable identity, prepare/effect/receipt/commit/ack, idempotency, stale owner/revision rejection, outbox acknowledgement and reconcile.
- Python is narrowed to process supervision, durable byte storage, permission/artifact/tool side-effect ports and event/result projection.
- The default Python `CodeWorkerRuntime` reaches the TypeScript process. It has no Python query fallback and fails closed when TypeScript is disabled.

## Direct quantitative gates

| Gate | Result | Minimum / maximum | Pass |
| --- | ---: | ---: | --- |
| Final TypeScript production | 33,239 | >= 28,000 | yes |
| Changed TypeScript production additions | 27,399 | >= 25,416 | yes |
| E01 test source | 7,044 | >= 7,000 | yes |
| Accepted upstream source coverage | 11,886 | >= 10,587 | yes |
| Deleted Python logical-owner source | 32,634 | >= 26,217 | yes |
| Adapter-only ratio | 1.978% | <= 10% | yes |

Tests, docs, evidence, lockfiles, adapter-only source, browser supplement, vendor/source pools and deleted Python are excluded from the TypeScript production totals.

## Source custody

- Primary implementation source is `claude-code-best`.
- Fourteen pure E01 query/model/compact files provide 11,886 accepted lines with exact full-file ranges and SHA-256 digests.
- Mixed permission/MCP/skill sections are deliberately excluded to prevent E02 overlap.
- Target paths use Zyra runtime contracts, journal identity, events, snapshots, tool host RPC and failure codes. Zyra does not execute the root source repository.

## Behavioral evidence

- E01 behavior and crash tests: 296 passed, 0 failed.
- Adjacent protocol and runtime tests: 9 passed, 0 failed.
- Python default cutover integration: 10 passed, 0 failed.
- Health/default reachability checks: passed.
- Exact TypeScript snapshot restore passed after excluding E01/control projections from the legacy session checksum payload.
- Default execution produced a TypeScript-owned checkpoint and 15 projected events.
- Environment and configuration disconnects failed closed with `python_policy_fallback=false`.
- Corrupt checkpoint bytes were rejected and replaced by a new TypeScript snapshot.
- Invalid tool arguments failed before a workspace side effect.

## Crash and commit protocol

The checked-in crash matrix covers 96 cases across prepare lost ACK, effect before receipt, receipt before commit, commit lost ACK, partial projection, duplicate prepare/commit, stale revision, unknown effect, corrupt restore, late result and stale owner. Reconciliation is deterministic and idempotency keys are stable within the run/session/transition identity.

## Anti-pseudo-internalization review

- No root source-repository runtime path was found.
- No vendor runtime, opaque bundle, binary or legacy inspection sidecar is required.
- Deleted Python owner paths are absent.
- Default runtime reachability and disconnect tests prove semantic effect, not inventory-only reachability.
- The Python host persists TypeScript-owned snapshot bytes atomically but does not choose tools, calculate compact thresholds, advance query phases or select providers.
- Browser context-window code was moved to the BrowserWorker-owned namespace and is excluded from E01 TypeScript counts.

### Independent-review risk

The forty domain files intentionally share an explicit transaction skeleton. They are imported, bootstrapped and journaled on the default path, and their source is maintained rather than a checked-in build artifact. Nevertheless, their high structural repetition is the main adversarial review risk. Independent review must challenge effective-line counting and verify that domain-specific operations are not treated as capability proof merely because the common commit protocol executes.

## Validation scope and deferrals

- Targeted E01 and adjacent behavior were run within the slice validation policy.
- A full repository suite and cleanroom packaging run were not repeated here; they belong to independent/high-risk review and later aggregate review.
- Bun was not preinstalled on PATH. Exact Bun `1.2.15` was executed through npm to generate the committed `bun.lock`; runtime behavior was validated with Node.js 22 TypeScript stripping.
- E02 permission/MCP/skill implementation was not started.

## Commit and repository boundary

- Code, tests, lockfile, review and evidence belong to the `G:\agent-zoo\zyra` Git repository.
- `G:\agent-zoo\docs\milestones\execution-state.yaml` is outside that repository and must be updated after the Zyra evidence commit.
- The commit containing this review is the E01 implementation evidence commit.
