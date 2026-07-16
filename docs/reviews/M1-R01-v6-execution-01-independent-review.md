# M1-R01 V6 Execution-01 Independent Review

- Review date: `2026-07-16`
- Execution: `M1-R01 / E01`
- Verdict: **FAIL**
- Recommended lifecycle state: `ready_for_fix`
- Reviewer nonce: `ed45733d91e909e610b566d878a691da21f63d2c111df81057e94b251eb9a3c4`

## 1. Frozen identities

| Role | Commit |
| --- | --- |
| Verified head `V` | `c34535a783e88f9481387ced89cba4fbc333dc74` |
| Implementation baseline `B` | `0cd21bff5e2d160476f2ce3cef766bf53aab1239` |
| Implementation candidate `I` | `16e8c83a0b9c67ae6fabacfa16d1da029581f96a` |
| Candidate evidence `E` | `9ab8fd49a652bb662e13f090b472d7d6e6d66ee3` |
| Self-review `A` | `018864090d3301a3f63528156e9206f17e309d8f` |
| Candidate metadata `M` | `2324cbf7616e993d8e42680fe1f2b587e82077c8` |
| Claude source snapshot | `c57f5a29e88e9a814bea47abeb9a0a6f725dc102` |

The ancestry chain `V -> B -> I -> E -> A -> M` is valid. `B` has parent `V`. No production or test code changes occur after `I`; `I..A` contains candidate evidence/self-review and `A..M` contains candidate metadata.

## 2. Decision

V6 is a substantive TypeScript implementation, not the generic-journal template rejected in the prior review. It passes typecheck, build, 388 behavior tests, 50 executable mutations, runtime ownership/resume/disable probes, exact-`I` cleanroom, dependency audit, and the numeric TypeScript floor.

It nevertheless fails two independent hard gates:

1. The frozen baseline receipt explicitly records `clean_worktree: false`, but the strict checker accepts it.
2. A post-freeze deterministic semantic sample rejects `11/24` accepted source-to-target records. The manifest generator and checkers validate shape, paths, hashes and token presence, not semantic equivalence between each source symbol, target symbol, default call path, state effect, behavior test and mutation.

Passing execution tests and mutations cannot compensate for either failure.

## 3. Findings

### P0 - The baseline receipt is unclean and the gate fails open

`G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-baseline-receipt.json` records:

- `clean_worktree: false`
- modified candidate gate, metadata, LOC, five-hop and strict-gate evidence files
- untracked `.tmp/`
- capture time `2026-07-16T01:47:40.133Z`
- `current_control_plane_head` already at `I`

Schema v3 section 8 and candidate gate G0 require a clean captured baseline. The reviewer manifest audit validates every referenced hash but exits nonzero for this receipt. In contrast, `verify_m1_r01_e01_v4.ts` returns exit `0` because it never asserts receipt cleanliness. V6 generation updates receipt hashes/head fields while preserving the dirty flag and dirty paths.

Required repair: regenerate the receipt from a clean, correctly timed baseline authority and make both generic and strict gates fail when `clean_worktree !== true`, dirty paths are nonempty, or capture identity/timing is inconsistent.

### P0 - Structural 285/285 closure contains false semantic mappings

The nonce was generated with a 32-byte CSPRNG after identities were frozen. Selection uses the lowest `SHA256(nonce|kind|mapping_id)` values per required capability bucket. Of 24 accepted records, 13 pass and 11 fail semantic review.

| Mapping | Claimed hop | Failure |
| --- | --- | --- |
| `e01-src-0252` | `processPreMappedToolResultBlock -> ToolResultRuntime.deliver` | Source persistence/reference replacement is actually implemented by `applyToolResultBudget`, not the claimed target. |
| `e01-src-0242` | `PERSIST_THRESHOLD_OVERRIDE_FLAG -> deliver` | No per-tool feature-flag threshold override exists in `deliver`. |
| `e01-src-0256` | `isPersistError -> deliver` | `deliver` performs no persistence and has no persistence-error classification/fallback. |
| `e01-src-0241` | `TOOL_RESULT_CLEARED_MESSAGE -> deliver` | No equivalent model-visible cleared-content marker exists. |
| `e01-src-0227` | `DIMINISHING_THRESHOLD -> decideContext` | No continuation/delta tracker exists. |
| `e01-src-0226` | `COMPLETION_THRESHOLD -> decideContext` | No 90% continue/nudge policy exists. |
| `e01-rej-0038` | `checkTokenBudget tail -> decideContext` | Source continue/stop state machine is replaced by a compact boolean. |
| `e01-src-0229` | `checkTokenBudget -> decideContext` | Same state-machine mismatch for the leading source range. |
| `e01-src-0156` | `consumePendingCacheEdits -> compactConversation` | No pending cache-edit consume-and-clear effect exists. |
| `e01-src-0170` | `getEffectiveContextWindowSize -> calculateTokenWarningState` | Target consumes a window rather than deriving it, and the default caller passes empty messages. |
| `e01-src-0141` | `detectGateway -> logging_module` | Target logs events/spans but never fingerprints gateway headers or host suffixes. |

The six independently selected rejected records are correctly rejected. The 23 V5-targeted closure records are structurally present, and the two largest accepted ranges (`queryModel`, 1,876 lines; `queryLoop`, 1,375 lines) have substantive default chains. Those positives do not repair false records elsewhere in the accepted population.

Required repair: replace family-level templating with per-symbol semantic decisions. Incorrect records must be rejected or pointed at the actual owner with a real immediate call edge, observable state effect, behavior assertion, and mutation that changes that target behavior.

### P1 - Default context budget behavior does not match credited source semantics

At `I`, `E01RuntimeCoordinator.decideContext` calls:

```ts
this.compact.calculateTokenWarningState([], contextWindow, 8_192)
```

The empty message list yields a zero-token warning calculation. The remaining decision uses only `forceCompact`, `contextChars > maxContextChars`, and `compactionCount`. It cannot observe the source tracker's `continuationCount`, `lastDeltaTokens`, `lastGlobalTurnTokens`, 90% completion point, diminishing returns, nudge message or completion event.

This is both a false custody claim and a default-path semantic gap. Mutation `e01-mut-016-compact-threshold` proves a compact threshold exists; it does not prove the credited token-budget state machine exists.

### P1 - Mutation linkage is not symbol-specific

All 50 declared mutations compile and are killed, but the five-hop manifest can attach them to unrelated target claims:

- `e01-mut-030-result-budget` changes `packages/runtime/claude-runtime/src/budget.ts::applyToolResultBudget`, while four sampled mappings claim `packages/runtime/claude-runtime/src/tools/result-runtime.ts::ToolResultRuntime.deliver`.
- `e01-mut-026-usage` changes provider usage accounting; it does not exercise source `detectGateway` or a gateway classifier in `logging_module`.
- The tests cited by the tool-result rows validate provider observation and settlement order rather than each row's threshold override, persistence-error fallback, cleared marker, or claimed `deliver` effect.

Required repair: gate mutation linkage by exact target path/symbol and require the named killer test to assert the row's declared state effect. A killed mutation in the same capability family is insufficient.

## 4. Independent machine checks

| Check | Result |
| --- | --- |
| Strict V4 checker against `I` | PASS mechanically; `30,841` effective changed TS; zero reported failures |
| Generic manifest checker | PASS mechanically; `285/285`; `50` unique target symbols; maximum `47` mappings/symbol |
| Reviewer manifest/hash audit | FAIL only for unclean receipt; all source blobs, ranges, target-at-`I` hashes, IDs and receipt file hashes otherwise valid |
| Bun | `1.2.15` |
| TypeScript | `5.8.3` |
| `typecheck:e01` | PASS |
| build and built health | PASS |
| E01 behavior suite | `388 pass / 0 fail`, 14 files, 1,233 assertions |
| Mutation run | `50/50 KILLED`, zero invalid/survivor, restored hashes match |
| Runtime-origin probe | PASS, TypeScript journal owner and E01 V6 snapshot |
| Write-path probe | PASS, canonical revision and state digest changed |
| Resume probe | PASS, three distinct processes, two forced restarts, no replayed transition ID |
| Lost-ACK probe | PASS, no duplicate mutation/effect |
| Disable probe | PASS, disabling the owner changes/fails the expected runtime path |
| Dependency audit | PASS |
| Exact-`I` cleanroom | PASS from `git archive`, fresh install/typecheck/build/tests/built health |

These checks establish that the implementation is executable and substantial. They do not override the receipt or semantic-custody failures.

## 5. Effective line and deletion audit

| Bucket | Reviewer-recognized machine count |
| --- | ---: |
| Gross changed executable TypeScript | `31,495` |
| Winnowing deduction | `654` |
| Effective changed production TypeScript | `30,841` |
| E01 minimum | `25,416` |
| Final production TypeScript | `48,420` |
| Final test TypeScript | `10,679` |
| Deleted Python | `35,151` |

The numeric production floor passes under the strict AST/winnowing checker. The generic checker independently reports `26,427`, also above the floor. Neither figure includes generated/data/vendor-like/source-pool credit. LOC is not used to compensate for invalid source mappings.

The Python owner manifest contains 763 records. Hash-ranked reviewer sampling selected 153 records (20%); every sampled path exists at `B` and is absent at `I`. The full machine audit also reports no owner/path mismatch.

## 6. State custody and default reachability

The default `apps/code-worker/src/main.ts` path reaches `ClaudeRuntimeCore.run`, which constructs/restores `E01RuntimeCoordinator`, executes provider/model transport, plans tool batches, commits observations, compacts context, and returns session/E01/model-iteration snapshots. Reviewer probes confirm TypeScript ownership, state mutation, resume epochs and disable sensitivity.

The failure is therefore not that E01 remains a thin or unreachable implementation. The failure is that the evidence layer overclaims source-level equivalence and currently permits an invalid baseline receipt.

## 7. Required lifecycle action

1. Keep verified head at `c34535a783e88f9481387ced89cba4fbc333dc74`.
2. Mark E01 `ready_for_fix` with failed candidate `16e8c83a0b9c67ae6fabacfa16d1da029581f96a`.
3. Keep E02 and E03 blocked.
4. Regenerate the baseline receipt from a clean authority point and add fail-closed receipt checks.
5. Repair/reject the false mapping families and enforce exact target/test/mutation linkage.
6. Implement or explicitly reject the missing token-budget, cache-edit, gateway and tool-result sub-semantics.
7. Run a new post-freeze independent semantic sample; do not reuse this nonce as the sole selection authority.

## 8. Evidence index

Reviewer-owned evidence is under `docs/reviews/evidence/M1-R01-v6/execution-01-independent-review/`:

- `review-identity.json`
- `strict-gate.json`
- `manifest-audit.json`
- `nonce-selection.json`
- `nonce-semantic-sample.json`
- `python-deletion-sample.json`
- `mutation-results.json`
- `review-summary.json`
- `generic/`
- `probes/`
- `commands/`

No production code, tests, candidate evidence, root manifest, or root execution-state file was modified by this review.
