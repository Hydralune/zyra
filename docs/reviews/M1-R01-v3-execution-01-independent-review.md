# M1-R01 v3 Execution-01 Independent Review

## Verdict

**FAIL**

Execution-01 is not eligible for approval. The verified baseline must remain
`c34535a783e88f9481381387ced89cba4fbc333dc74`, E01 must remain
`review_pending`, and E04 must not start from this review result.

The verdict does not rely on a single environmental failure. It is already
determined by repository-owned evidence and reviewer-owned default-entry
probes: G0 was captured from a dirty worktree, all frozen target hashes are
null, the effective changed production line gate falls below threshold under
fail-closed accounting, the real HTTP provider path fails, denied tools can be
executed and accepted, and the query loop does not perform a provider revise
request after observing a tool result.

Reviewer evidence: `docs/reviews/evidence/M1-R01-v3/execution-01-independent-review-evidence.json`.

## Review boundary

| Item | Value |
| --- | --- |
| Verified baseline | `c34535a783e88f9481381387ced89cba4fbc333dc74` |
| Implementation commit | `d6ff45435167c04c132e08e3de275a24cf28b544` |
| Evidence commit | `e4e3f7edeac6c628a988cf0ff273ad0f9f1d6e29` |
| Anchored target | `df2937406e639862ec6a29795016b51651ee9cc0` |
| Initial worktree | Clean, exact anchored target |
| Production roots audited | `apps/code-worker/src`, `packages/runtime/claude-runtime/src` |
| Reviewer nonce | `2e6e7dafad7982963c874e1e0a8ab4b0f196adb8190edb208dee19594cc49445` |

The ancestry from baseline through implementation, evidence, and target is
linear. The implementation-to-evidence diff contains review evidence only;
the evidence-to-target diff contains candidate metadata only. No later
runtime-event-spine or Python runtime-event work was credited to E01 changed
production lines.

## Gate matrix

| Gate | Result | Independent observation |
| --- | --- | --- |
| G0 frozen baseline | **FAIL** | Receipt records `clean_worktree: false` and 19 dirty paths, including E01 production, tests, and verification scripts. |
| Effective changed production SLOC `>= 25,416` | **FAIL** | Claimed `25,605`; generous fail-closed upper bound `22,425`. |
| E01-only final production SLOC `>= 28,000` | **FAIL / unproven** | Claimed raw `34,642` includes pre-existing agent, permission, skill, and control modules; no E01-only final bucket is supplied. |
| Effective behavior-test SLOC `>= 7,000` | **Unaccepted** | Candidate claims `8,099`; behavior is substantial, but the same wrapper-unit/exact-skeleton counter is not a sufficient independent near-clone audit. |
| Source-to-target five-hop | **FAIL** | `229/229` target hashes are null; only 33 target symbols, four success sets, four failure sets, and one disable set. |
| Required six-domain sample plus rejected sample | **FAIL** | Source manifest has no tool or session-lifecycle source domain and has zero rejected records. |
| Frozen mutation package | **FAIL** | `0/34` frozen patch hashes equal the actual patches executed. |
| Default HTTP provider path | **FAIL** | Fresh stdio task exits `1`: `cannot snapshot provider transport with active or queued requests`. |
| `reason -> tool -> observe -> revise` | **FAIL** | One model resolution occurs before tool execution; no post-result provider request exists. |
| Permission fail-closed | **FAIL** | Two TypeScript `deny` decisions were delegated, executed, and accepted. |
| Tool-result budget, compact, later real tool | **PASS** | Two real tool requests, three artifact requests, one compact event, later tool observed, successful result. |
| Primary owner disable | **PASS** | Disabled owner returns `e01_typescript_runtime_disabled`, no tool request, no fallback result. |
| Three-process same-session resume | **PASS** | Three distinct killed PIDs, epochs `0 -> 1 -> 2`, revisions `2 -> 4 -> 6`, zero replayed transition IDs. |
| Anchored-worktree E01 tests | **PASS** | Bun 1.2.15: `364 pass`, `0 fail`, `1,156` expectations. |
| Fresh-cache cleanroom | **Unresolved, fail-closed** | Sandbox blocked registry downloads; escalation was rejected, so install/typecheck/build/test/built-entry could not be independently closed. |

## Findings

### P0-1: G0 and frozen custody evidence are not valid

The mandatory receipt at
`../docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-baseline-receipt.json`
records `clean_worktree: false` and 19 dirty paths. The dirty set contains E01
production files, E01 tests, the gate generator, mutation runner, probe, and
manifest verifier. Schema v3 requires a clean frozen capture; this alone is a
deterministic G0 failure.

The target custody map contains 229 records, but every `target_sha256` is null.
The generator writes this explicitly in
`scripts/remediation/m1_r01_e01_v3.ts:582`. A G2 target record without its
target content hash is not frozen target custody. The candidate verifier only
checks a target hash when one is present, so the null value bypasses rather
than satisfies the gate.

The mutation package has the same integrity problem. All 34 mutants compile,
are reported killed, and restore their original file hashes, but none of the
34 `frozen_patch_sha256` values equals the actual patch hash executed. The
published kill rate therefore does not validate the frozen mutation manifest.

### P0-2: Effective changed production SLOC misses the mandatory threshold

The candidate reports 1,467 effective AST units spanning 25,605 lines, only
189 lines above the 25,416 threshold. The counter credits the inclusive
physical span of TypeScript declarations, including 2,980 lines of interfaces
and 170 lines of type aliases. Those 3,150 lines are erased at runtime, and the
evidence provides no item-level behavior or reachability proof that would make
them independently creditable under the fail-closed rules.

Independent accounting found:

| Measure | SLOC |
| --- | ---: |
| Candidate claimed effective changed | 25,605 |
| Nonblank/noncomment within claimed spans | 25,575 |
| Unproven interface/type-alias spans | -3,150 |
| Generous fail-closed upper bound | **22,425** |
| Required | **25,416** |
| Deficit | **2,991** |

This upper bound is deliberately favorable to the candidate: it applies no
additional deduction for unreachable code, near clones, DTO-like class
members, or behaviorally dormant branches. The changed production gate still
fails.

The final raw count of 34,642 is also not an E01-only count. The candidate
report includes pre-existing `agents/**`, `permission/**`, `skills/**`, and
`control/**` modules in the final bucket. Because this review may not borrow
lines from E02/E03 or other later capabilities, that raw total cannot close
the E01 final-SLOC gate.

### P0-3: The real HTTP provider default path cannot finish a run

A reviewer-owned task launched the actual
`apps/code-worker/src/main.ts --stdio` entry, used a local OpenAI-compatible
SSE endpoint, and supplied a fresh nonce and session. The provider received
one request. The runtime produced no tool request or final result and exited
with code 1:

```text
cannot snapshot provider transport with active or queued requests
```

This is a default-path failure, not an import or inventory probe. It invalidates
the claim that the productized HTTP provider/session path is complete.

### P0-4: TypeScript permission `deny` is advisory, not enforced

The default stdio budget probe deliberately submitted paths outside the bound
workspace. The TypeScript evaluator returned `deny` twice with reason
`typescript_path_outside_workspace`. Both requests were still emitted to the
external host, and both successful receipts were accepted into the completed
run.

The control flow explains the result:

- `packages/runtime/claude-runtime/src/capability-host.ts:80` computes the permission decision.
- `packages/runtime/claude-runtime/src/capability-host.ts:92` delegates the enriched request unconditionally.
- `packages/runtime/claude-runtime/src/capability-host.ts:97` accepts every external-tool receipt because `permissionOnly` is false.
- `packages/runtime/claude-runtime/src/capability-host.ts:98` pushes that receipt without enforcing `deny`.

This contradicts the claimed TypeScript canonical permission owner and fails
the required fail-closed behavior under an adversarial or faulty gateway.

### P0-5: The core is not a true observe-and-revise model loop

`ClaudeRuntimeCore.run` invokes `resolveModelTurns` once at
`packages/runtime/claude-runtime/src/query-engine.ts:240`, before executing the
returned tool batches. The provider resolver returns precomputed turns at
`packages/runtime/claude-runtime/src/model-stream.ts:367` or
`packages/runtime/claude-runtime/src/model-stream.ts:477`. Tool execution then
consumes those preplanned turns; the tool result is not appended to a new
provider request, and there is no second provider call for revision or a final
answer.

Multiple scripted turns are therefore a predeclared tool plan, not
`reason -> tool -> observe -> revise`. The reviewer HTTP probe fails even
earlier because of P0-3, but that earlier failure does not cure this structural
loop defect.

### P1-1: The five-hop map is mechanically many-to-few and cannot satisfy the required sample

The source manifest has 229 accepted records across 15 files and zero rejected
records. Its files cover query, provider/API, and compact/context code. It has
no tool source module and no session-lifecycle source module, so the mandated
query/tool/context/compact/provider/session sampling frame cannot be formed;
the required four rejected samples are also impossible.

On the target side, 229 source records collapse to eight paths and 33 symbols.
`ContextCompactionRuntime.compactConversation` and
`ProviderRecoveryRuntime.classify` each receive 43 mappings. Across the entire
map there are only four distinct success-test sets, four failure-test sets,
and one disable-test set. A nonce-derived 24-record sample found null target
hashes in all 24. This is string-linked coverage, not defensible five-hop
semantic custody.

### P1-2: The independent cleanroom gate is not closed

A `git archive` of the anchored target was extracted into a fresh tree with no
`.git`, `node_modules`, or `dist`, using Bun 1.2.15 and a fresh isolated cache.
`bun install --frozen-lockfile` attempted the pinned dependencies but received
sandbox `ConnectionRefused` errors. The required network escalation was
rejected because it would disclose dependency metadata to external
registries. The cleanroom tree and cache were deleted.

This environmental limitation is not presented as a repository defect and is
not needed for the FAIL verdict. It does mean the independent install,
typecheck, build, test, and built-entry chain remains unverified and must be
treated fail-closed.

## Positive evidence retained

The review does not discard working behavior:

- The anchored worktree ran `runtime:e01:test` under Bun 1.2.15 with 364 passes, zero failures, and 1,156 expectations across ten files.
- Nine E01 behavior files contain 355 uniquely named test cases over 8,353 physical lines.
- The reviewer budget probe externalized oversized results, compacted once, and still executed a later real tool call.
- Disabling the TypeScript E01 owner failed the default stdio task without a successful fallback.
- A reviewer-owned three-process crash/restore probe used PIDs `69144`, `62860`, and `80280`, advanced restart and custody epochs `0,1,2`, advanced revisions `2,4,6`, and replayed no transition IDs.
- The submitted mutation run reports 34 compiling mutants killed and all source files restored, although its frozen patch identities are invalid.

These positives are insufficient to offset any mandatory fail-closed gate.

## Required remediation before another review

1. Recreate G0 from a clean worktree and bind every required manifest/profile hash in the receipt.
2. Populate immutable target hashes at G2 and freeze the exact mutation patches that will actually execute.
3. Rebuild source custody to cover query, tool, context, compact, provider, and session domains, including explicit rejected records and semantically distinct tests.
4. Replace physical-span/exact-skeleton counting with an independent executable/reachability classification and near-clone analysis; publish an E01-only final bucket.
5. Complete the provider request lifecycle before snapshotting and implement an actual post-tool provider revise/final-answer request.
6. Enforce TypeScript `deny` before delegating or accepting any external tool result.
7. Repeat reviewer-owned default-entry, mutation, crash/restore, and fresh-cache cleanroom evidence on a new anchored target.

## Reviewer change boundary

This review adds only:

- `docs/reviews/M1-R01-v3-execution-01-independent-review.md`
- `docs/reviews/evidence/M1-R01-v3/execution-01-independent-review-evidence.json`

No production code, tests, build configuration, remediation scripts, root
planning documents, or root `execution-state.yaml` were modified.
