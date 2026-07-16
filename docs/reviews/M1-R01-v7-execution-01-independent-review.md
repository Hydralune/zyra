# M1-R01 V7 Execution-01 independent review

## Verdict

**FAIL**

Review target and frozen identities:

- verified baseline: `c34535a783e88f9481387ced89cba4fbc333dc74`
- implementation candidate `I`: `050c68e1e80cbe942b248111288981bffd6f3be5`
- evidence commit `E`: `5a991ce20fdb2b6490d289acf44600961e55cfdc`
- attestation/review target `A`: `68b0e6e91da7087afc11060e6a0523459a0c1c0c`
- reviewer nonce: `b5f9b110ee4767d124ba2ce0ba2448e766adec3c15cf84fc6ec0e4cb4e0b9b49`

The V7 runtime is substantive code and its executable checks pass. It nevertheless fails E01 because the accepted source-to-target coverage is materially overstated and the mutation evidence does not prove the declared mapping-specific killer relationship. Code health cannot compensate for either gate.

## Findings

### P0 - Twenty-six accepted mappings claim semantics that their target symbols do not own

The strict gate mechanically counts `10,613` unique executable source lines against the `10,587` floor, leaving only 26 lines of headroom. A reviewer-owned source-blob recount used the same frozen source snapshot and the gate's own executable-line rule, then removed only the 26 mappings whose source and target were directly inspected and found non-equivalent. These mappings account for `2,349` unique executable source lines, leaving a generous recognized upper bound of `8,264`, or `2,323` below the floor.

This is an upper bound because it continues to credit other high-fan-in generic mappings, including `queryModel`, `queryLoop`, and `QueryEngine`, without resolving their remaining semantic breadth.

| Mapping | Executable lines | Source -> target | Why the accepted equivalence fails |
| --- | ---: | --- | --- |
| `e01-src-0001` | 3 | `messageSelector` -> `ClaudeRuntimeCore.run` | The source is a lazy React/Ink module loader. The target loop neither loads nor replaces message-selection behavior. |
| `e01-src-0002` | 6 | `getCoordinatorUserContext` -> `E01RuntimeCoordinator.decideQuery` | The source conditionally builds MCP/scratchpad coordinator context. The target performs turn admission and stop decisions. |
| `e01-src-0019` | 163 | `getAnthropicClient` -> `E01RuntimeCoordinator.executePreparedProvider` | The source refreshes auth and chooses Anthropic, Bedrock, Foundry, or Vertex clients. The target only executes an already prepared and already credential-bound request. |
| `e01-src-0028` | 32 | `should1hCacheTTL` -> `ProviderTelemetryRuntime.recordPromptState` | The source owns provider, eligibility, allowlist, and session-stable TTL policy. The target merely consumes a supplied TTL while recording hashes. |
| `e01-src-0049` | 113 | `addCacheBreakpoints` -> `ProviderTelemetryRuntime.recordPromptState` | The source mutates provider messages with cache markers, pinned edits, deduplication, and cache references. The target is telemetry and does not perform those mutations. |
| `e01-src-0105` | 150 | `logToolUseToolResultMismatch` -> `ProviderRecoveryRuntime.classify` | The source reconstructs normalized and pre-normalized tool sequences and logs forensic metadata. The target classifies an error shape. |
| `e01-src-0108` | 401 | `getAssistantMessageFromError` -> `ProviderRecoveryRuntime.plan` | The source renders user-visible messages for quota, auth, image, model, concurrency, and interactive recovery cases. The target returns retry/fallback/stop state and no assistant message. |
| `e01-src-0120` | 8 | `getCacheBreakDiffPath` -> `ProviderTelemetryRuntime.recordPromptState` | The source creates a randomized filesystem path for a diff artifact. The target creates no path or diff artifact. |
| `e01-src-0127` | 3 | `isExcludedModel` -> `ProviderTelemetryRuntime.recordPromptState` | The source excludes Haiku from post-response detection. The target records every model and has no corresponding exclusion. |
| `e01-src-0136` | 189 | `checkResponseForCacheBreak` -> `detectPromptBreak` | The source uses response cache-token deltas, model exclusion, message time, deletion adjustments, and optional diff writing. The target compares two pre-recorded hash snapshots only. |
| `e01-src-0153` | 3 | `pendingCacheEdits` -> `ContextCompactionRuntime.compactConversation` | The source is a process-global pending cache-edit slot. The target neither owns nor consumes that cache-edit protocol. |
| `e01-src-0164` | 3 | `isMainThreadSource` -> `ContextCompactionRuntime.compactConversation` | The source prevents forked agents from touching global cache state. The target does not gate compaction on a main-thread query source. |
| `e01-src-0165` | 23 | `microcompactMessages` -> `ContextCompactionRuntime.compactConversation` | The source dispatches cached or time-based microcompact and may leave messages unchanged. The target always runs full summarizing compaction. |
| `e01-src-0166` | 71 | `cachedMicrocompactPath` -> `ContextCompactionRuntime.compactConversation` | The source explicitly does not mutate local messages and emits cache-edit metadata. The target removes messages and inserts a local summary boundary. |
| `e01-src-0168` | 28 | `maybeTimeBasedMicrocompact` -> `ContextCompactionRuntime.compactConversation` | The source is a time-gap trigger and tool-result clearing path. The target is not a time-gap predicate or content-clear operation. |
| `e01-rej-0029` | 33 | continuation of `maybeTimeBasedMicrocompact` -> `compactConversation` | The accepted supplementary continuation carries the same unmatched time-based clearing behavior. |
| `e01-src-0179` | 41 | `shouldAutoCompact` -> `calculateTokenWarningState` | The source applies enabled/message-count/model gates. The target symbol only calculates thresholds; the target package has a separate `shouldAutoCompact` method that was not mapped. |
| `e01-src-0180` | 57 | `autoCompactIfNeeded` -> `calculateTokenWarningState` | The source conditionally executes compaction. The target symbol only calculates warning state and cannot execute a summary. |
| `e01-rej-0030` | 30 | continuation of `autoCompactIfNeeded` -> `calculateTokenWarningState` | The accepted supplementary continuation contains execution behavior absent from the mapped calculator. |
| `e01-src-0198` | 19 | `annotateBoundaryWithPreservedSegment` -> `compactConversation` | The source writes relink head/anchor/tail metadata. Zyra has a separate same-purpose method, but the mapping points at a full-compaction method and its cited test does not assert relinking. |
| `e01-src-0201` | 288 | `partialCompactConversation` -> `compactConversation` | The source owns directional partial selection and preserved prefix/suffix behavior. Zyra has `partialCompactConversation`, but the mapping points to the full method and cites only a generic compact test. |
| `e01-src-0204` | 205 | `streamCompactSummary` -> `compactConversation` | The source owns streaming summary events and stream lifecycle. The target accepts a `Promise<string>` summary provider and has no equivalent streaming contract. |
| `e01-src-0222` | 25 | `shouldUseSessionMemoryCompaction` -> `planCompaction` | The source applies session-memory eligibility and threshold policy. The target planner does not inspect the stored session-memory eligibility fields. |
| `e01-src-0223` | 56 | `createCompactionResultFromSessionMemory` -> `planCompaction` | The source constructs a session-memory compaction result. The target symbol only selects compact/preserve ranges. |
| `e01-rej-0036` | 44 | continuation of `trySessionMemoryCompaction` -> `planCompaction` | The accepted supplementary range performs session-memory execution and result handling that the planner does not own. |
| `e01-src-0236` | 355 | entire `StreamingToolExecutor` class -> `ToolExecutionSettlementRuntime.appendProgress` | The source class owns queueing, safe/exclusive concurrency, permission execution, sibling abort, fallback discard, ordered results, and context modifiers. The target method only validates and stores one bounded progress chunk. |

The `e01-rej-*` identifiers above are not rejected records in V7. They are accepted supplementary continuation records and therefore contribute to the mechanical line total.

The clearest single counterexample is `e01-src-0236`: removing its 355 executable lines alone reduces recognized coverage from `10,613` to at most `10,258`, already below the threshold. The additional 25 failures show that this is a generator-level overclaim rather than an isolated typo.

### P1 - The mutation runner can attribute a kill to a test that passed

`scripts/remediation/run_m1_r01_e01_mutations.ts:530` checks only whether an expected killer test name appears anywhere in the test process output. Lines 531-532 then combine that appearance with a nonzero file-level test exit code. They do not verify that the named expected killer test failed.

A reviewer-owned semantic mutant changed `ToolExecutionSettlementRuntime.appendProgress` so it retained reachability and sequence handling but no longer enforced `maximumCharacters` truncation. Running the same adversarial test file produced:

- PASS: `settlement preserves request order across blocked and delegated calls`, the killer declared by mapping `e01-src-0236`.
- FAIL: `settlement progress enforces sequence and bounded storage`, a different test in the same file.
- process exit: `1`.

The current runner would observe the declared name, see a nonzero process exit, and record the mutant as killed even though the declared test passed. This invalidates mapping-specific mutation attribution. It also explains why a generic test can be reused by many mappings without proving each semantic claim.

Of the 103 frozen mutations, 51 are `disconnect-target` throws. Those are useful reachability checks, but they do not prove that the source behavior named by a mapping is preserved. For `e01-src-0236`, the first listed mutation even reports `target_matches: false`; only the generic disconnect matches the mapped symbol.

### P1 - The five-hop file is structurally complete but semantically generic

The candidate has 280 accepted mappings compressed into 51 unique target path/symbol pairs. Only 20 unique success test IDs support them. Examples of fan-in include 47 mappings to `ProviderRecoveryRuntime.classify`, 39 to `ContextCompactionRuntime.compactConversation`, and 19 to `ProviderRecoveryRuntime.plan`. One generic compact test is reused by 87 mappings, and one provider test by 70.

The repeated source claim, target claim, equivalence sentence, and synthetic `assert.<mapping-id>...` state-effect identifier establish formatting, not symbol-specific equivalence. Eleven mappings use a self-referential default callsite and seven contain no default-entry edge. A target-disconnect mutation proves the target is reachable, not that every source symbol assigned to it has migrated.

### P2 - Candidate metadata and the assigned review target disagree

At attestation commit `A`, `candidate-metadata.json` binds `independent_review_target` to evidence commit `E`, while the authoritative execution state and this review assignment bind the independent review target to `A`. The ancestry is valid and this did not cause the FAIL, but the protocol identity must be made unambiguous in the next candidate.

## Nonce-stratified semantic sample

The reviewer selected four records from each of query, provider, context, compact, tool, and session using the frozen nonce. Ten of 24 were rejected. The rejected set alone is more than the taskbook's minimum four.

| Family | Mapping | Reviewer result | Reason |
| --- | --- | --- | --- |
| query | `e01-src-0002` | REJECT | Context construction is not query admission. |
| query | `e01-src-0014` | ACCEPT | The bounded output-token recovery constant is exercised by the exact recovery state machine. |
| query | `e01-rej-0001` | ACCEPT | The accepted continuation wraps engine submission and is reasonably adapted by the top-level runtime loop. |
| query | `e01-src-0001` | REJECT | Lazy UI module loading is absent from the target loop. |
| provider | `e01-src-0019` | REJECT | Provider/auth client creation is not execution of a prepared request. |
| provider | `e01-src-0081` | ACCEPT | Prompt-too-long classification maps to context-overflow recovery classification. |
| provider | `e01-src-0120` | REJECT | Diff-path creation is absent. |
| provider | `e01-src-0127` | REJECT | Model exclusion is absent. |
| context | `e01-src-0227` | ACCEPT | Diminishing-progress threshold is represented in the continuation state machine. |
| context | `e01-src-0229` | ACCEPT | `checkTokenBudget` behavior is represented and behavior-tested. |
| context | `e01-src-0226` | ACCEPT | Completion threshold is represented in the same state machine. |
| context | `e01-rej-0038` | ACCEPT | The accepted continuation completes the same budget function. |
| compact | `e01-src-0198` | REJECT | Preserved-segment relink metadata is not owned by the mapped symbol. |
| compact | `e01-src-0153` | REJECT | Pending cache-edit state is absent. |
| compact | `e01-src-0164` | REJECT | Main-thread cache-state isolation is absent. |
| compact | `e01-src-0222` | REJECT | Session-memory eligibility is absent from the planner. |
| tool | `e01-src-0259` | ACCEPT | Per-message/round observation limits have a corresponding stateful enforcement path. |
| tool | `e01-src-0236` | REJECT | A complete streaming executor cannot map to one progress append method. |
| tool | `e01-src-0263` | ACCEPT | Content sizing maps to candidate registration and charged-character state. |
| tool | `e01-src-0261` | ACCEPT | Already-replaced observations remain replaced across enforcement. |
| session | `e01-src-0310` | ACCEPT | Log-entry conversion is reasonably adapted into checksummed branch restore. |
| session | `e01-src-0304` | ACCEPT | Deserialization is represented by validated snapshot restoration. |
| session | `e01-src-0306` | ACCEPT | History reading is adapted to snapshot-based restoration. |
| session | `e01-src-0305` | ACCEPT | Log reading is adapted to snapshot-based restoration. |

## Checks that passed

These checks confirm that V7 is a large improvement over the earlier template candidate, but they do not close the failed source and mutation gates.

- `bun x tsc -p packages/runtime/claude-runtime/tsconfig.json --noEmit`: PASS.
- Bun build of `apps/code-worker/src/main.ts`: PASS, 78 modules, 1.17 MB artifact.
- Node ESM build of the same entry: PASS, 78 modules, 1.17 MB artifact.
- Bun and Node artifact `--health`: PASS, TypeScript canonical owner.
- `bun test packages/runtime/claude-runtime/test`: PASS, 388 tests across 19 files, 0 failures.
- Reviewer default-entry stdio probe: PASS, 16 frames ending in `run.result`, no Python policy or agent fallback.
- Reviewer same-session/lost-ACK probe: PASS, four fresh commit IDs, zero duplicate IDs, restart epoch 1, committed effect count unchanged, tool state preserved.
- Mechanical source recount: PASS at `10,613`; semantic recognized upper bound: FAIL at `8,264`.
- Mechanical mutation result: 103/103; mapping-specific attribution: FAIL.

## Required remediation

1. Reject or remap all 26 records above. A remap must point to the exact target owner and include a source-specific behavior assertion, observable state effect, default path, and exact killer test.
2. Do not simply delete the records and retain PASS. Their removal leaves a `2,323`-line source-coverage shortfall, so the candidate must add genuinely migrated behavior or provide different valid source coverage.
3. Change the mutation runner to parse test outcomes and require every declared killer test to fail for that mutant. A test name merely appearing as PASS must not count.
4. Add semantic mutants that preserve reachability while removing the claimed behavior. Keep disconnect mutations as a separate reachability bucket.
5. Regenerate the source manifest, target custody map, mutation manifest, five-hop evidence, strict gate, candidate evidence, and candidate identity chain from the corrected implementation.
6. Keep E02 and E03 blocked until a new E01 candidate passes independent review.

Machine-readable reviewer evidence is recorded in `docs/reviews/evidence/M1-R01-v3/execution-01/independent-review-v7.json`.
