# M1-R01 V9 Execution-01 Independent Review

## Verdict

`FAIL`

Execution-01 is not complete. The immutable Git ancestry, mutation receipts, process-resume probe, and exact-candidate toolchain are valid, but the source-custody gate is not reproducible from the receipt-bound root manifests and the recognized source coverage falls below the `10,587` executable-line floor after mandatory boundary and semantic deductions.

## Review identity

| Identity | Commit | Result |
| --- | --- | --- |
| Verified baseline | `c34535a783e88f9481387ced89cba4fbc333dc74` | Bound consistently in candidate metadata and gate output |
| Implementation diff baseline | `0cd21bff5e2d160476f2ce3cef766bf53aab1239` | Direct child of the verified baseline; receives zero E01 credit |
| Implementation candidate `I` | `796a9c457c6bb871edd26808d91b5affd49f8923` | Descendant of the verified baseline |
| Raw evidence `E` | `2698b5b6276eee001a9c6adee4f547cb6628bfce` | Direct child of `I` |
| Critical self-review target `A` | `73542eb1b9c8fe0efbc66415b9e6b9754ff11df2` | Direct child of `E` |
| Metadata chain head `M` | `b9acd65211b2ec566744dcb0cdbb766ea28c7a9d` | Direct child of `A`; binds `I/E/A` |
| Claude source snapshot | `c57f5a29e88e9a814bea47abeb9a0a6f725dc102` | Immutable source blobs used for line and semantic review |

The four-part Git chain is valid. The failure is not an ancestry failure.

## Findings

### P0: The committed strict gate cannot be reproduced from the receipt-bound root manifests

The root G0 artifacts and the committed V9 strict evidence describe different source closures:

| Evidence | Accepted | Rejected | Target mappings | Mutations |
| --- | ---: | ---: | ---: | ---: |
| Root `g0-summary.json` and receipt-bound manifests | 322 | 38 | 322 | 44 |
| Committed `strict-gate.json` | 286 | 74 | 286 | 129 |

The baseline receipt binds SHA-256 `c48f26723cf247064db076616ab74c6f3292649c4fbea2711de343945bc75f83` for `execution-01-source-manifest.jsonl` and `a3f94c32616ae06590422fc45ffada6cdc2d1dfb4fa23446d4a8bddabeecdf85` for `execution-01-target-custody-map.jsonl`. Those hashes match the current root files. They do not describe the `286/74` closure recorded by the committed strict gate and five-hop file.

A reviewer-owned V4 verifier run against candidate `I` and those receipt-bound files returned exit `1`: `322` accepted, `38` rejected, `10,662` accepted executable lines, and `322` targets. It also reported the dirty receipt and numerous mappings without exact target/test mutation custody. This is materially different from the committed `286`, `10,690`, and `286` pass.

The candidate gate is internally contradictory as well: [candidate-gate-result.json](G:/agent-zoo/zyra/docs/reviews/evidence/M1-R01-v3/execution-01/candidate-gate-result.json) records top-level `"ok": false` with an empty `failures` array, while [strict-gate.json](G:/agent-zoo/zyra/docs/reviews/evidence/M1-R01-v3/execution-01/strict-gate.json) records `"ok": true`.

Because the exact manifest bytes used for the claimed strict pass are not frozen in the immutable Zyra evidence chain, the `286/286` five-hop result is not reproducible from the authoritative root delivery boundary.

### P0: Eight forbidden outside-curated symbols were used to recover the source-credit floor

The V4 correction section of `execution-01-runtime-core-typescript-cutover.md` permits an `e01-rej-*` range to be recovered only when it is an explicit statement tail of the same accepted symbol. It expressly prohibits admitting a new symbol whose exclusion is `outside curated executable source boundary`.

V9 nevertheless credited these eight new-symbol ranges:

| Source symbol | Immutable executable lines |
| --- | ---: |
| `buildSystemPromptBlocks` | 24 |
| `queryHaiku` | 49 |
| `queryWithModel` | 48 |
| `adjustParamsForNonStreaming` | 25 |
| `getMaxOutputTokensForModel` | 13 |
| `collectReadToolFilePaths` | 45 |
| `truncateToTokens` | 7 |
| `shouldExcludeFromPostCompactRestore` | 23 |
| Total forbidden credit | 234 |

The counts were recomputed from the immutable Claude source blobs with the verifier's executable-line predicate. Removing the prohibited `234` lines from the committed `10,690` leaves at most `10,456`, already below `10,587`.

### P0: The newly claimed implementations are materially weaker than their source ranges

The target symbols are default-reachable by name, but reachability is not semantic equivalence:

| Source mechanism | Review result | Material difference |
| --- | --- | --- |
| `buildSystemPromptBlocks` | Invalid | Source splits the prompt by cache scope and conditionally adds cache control using enable/skip/query-source policy. Target normalizes existing blocks and always marks only the final block. |
| `queryHaiku` | Invalid | Source constructs prompts, selects the small fast model, disables tools/thinking, defaults prompt caching off, supplies an empty permission context, and uses VCR. Target requires an already prepared Haiku request and only validates/delegates it. |
| `queryWithModel` | Invalid | Source constructs and executes a non-streaming no-tool/no-thinking query through the full provider path. Target is a one-line alias for `execute` over an already prepared arbitrary request. |
| `adjustParamsForNonStreaming` | Plausible implementation, forbidden source credit | Target performs the expected non-streaming max-token and thinking-budget cap in `prepare`, but its source range is still outside the frozen curated boundary. |
| `getMaxOutputTokensForModel` | Invalid | Source chooses model defaults, applies a feature-controlled slot cap, then a bounded environment override. Target only clamps a caller-provided requested value to model/environment maxima. |
| `getEffectiveContextWindowSize` | Invalid | Source subtracts the reserved compact-summary output allowance. Target returns a model/configuration cap without that subtraction. |
| `collectReadToolFilePaths` | Invalid | Source expands paths and identifies preserved full reads so the caller can skip reinjection. Target only slash-normalizes and participates in the inverse selection described below. |
| `truncateToTokens` | Invalid | Source reserves space for its truncation marker. Target first fills the token budget, then appends a marker, so the returned content can exceed the declared maximum. |
| `shouldExcludeFromPostCompactRestore` | Invalid | Source excludes plan and memory files; preserved reads are excluded separately by the caller. Target includes preserved-read paths and excludes unread candidates when any read is present, reversing the source dedup rule. |

The source post-compact caller filters out `preservedReadPaths.has(expandPath(file.filename))`. In contrast, [context-runtime.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/compact/context-runtime.ts#L842) returns exclusion for paths *not* in the collected read set. The V9 test asserts that `/workspace/read.txt` is reinjected, thereby freezing the opposite of the source behavior rather than detecting it.

The semantically invalid ranges above total `224` executable lines when the plausible adjustment helper is left credited. That produces `10,466/10,587`. Combining the semantic rejection of the 15-line effective-window helper with the frozen-boundary rejection of all eight outside-curated symbols produces the review upper bound:

`10,690 - 234 - 15 = 10,441 < 10,587`

There is also an independent root-manifest calculation. The receipt-bound manifest still accepts the invalid 163-line `getAnthropicClient` factory range. The reviewer-owned verifier reports `10,662` mechanical lines for that manifest, so removing only that already-established overclaim leaves `10,499/10,587` before any other semantic deduction.

### P1: The V8 compact array no-op is fixed at entry, but several mapped effects remain transient shadow calculations

[context-runtime.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/compact/context-runtime.ts#L265) now constructs an object envelope, calls the instance-owned `CompactionSourceCustodyRuntime`, and consumes mutated messages. This fixes the V8 failure where `arguments[0]` was an array rejected by `recordOf`.

The repair is only partial for the sixteen reviewed mappings:

| Compact path | Result |
| --- | --- |
| Time-based microcompact | Real: mutated messages feed the parent compaction path; time and pending-edit maps are snapshotted. |
| Stream summary | Real: the returned summary drives the committed boundary and session memory. |
| Partial boundary | Partial: only the helper-generated boundary identity is consumed; the preserved-segment result is not the parent plan owner. |
| Auto-compaction plan | Shadow: `sourceCustodyAutoCompaction` is written to the temporary envelope, ignored by `ContextCompactionRuntime`, and absent from snapshots. The parent recomputes admission independently. |
| Session-memory compaction result | Shadow: `sourceCustodySessionMemory` is written to the temporary envelope, ignored by the parent, and absent from snapshots. The parent updates its own summary state independently. |

`CompactionSourceCustodyRuntime.snapshot()` contains only `lastMicrocompactAt` and `pendingEdits`. It does not preserve the auto plan, session-memory result, streamed-attempt state, or partial result. Direct helper tests and target-disconnect failures therefore do not prove canonical default-path custody for all sixteen source ranges.

### P1: V3-to-V4 supersession is not closed by the actual gate artifacts

Replacing mutable-checkout source hashing with immutable Git-blob hashing is a sound correction, and the execution document names V4 as the intended authority. The delivery artifacts did not complete that transition:

| Artifact | Still declares |
| --- | --- |
| Root gate profile `commands.source_validator` | `verify_m1_r01_e01_v3.ts` |
| Root baseline receipt `schema_validator_command` | `verify_m1_r01_e01_v3.ts` |
| Root gate profile `checker_source_paths` | V3 generator/verifier; no V4 verifier |
| Candidate gate | `ok:false`, despite labeling V4 as authority |

V3's failure therefore cannot be treated as harmless solely because candidate metadata says it is superseded. The root profile/receipt must be regenerated under one coherent authority, and the resulting candidate gate must itself be `ok:true`.

### P2: Root execution state does not authorize the V9 review candidate

At review time, `docs/milestones/execution-state.yaml` still records V8 as the latest revision-3 candidate, `candidate_zyra_head: null`, `execution_01_review_allowed: false`, and `execution_01_fix_in_progress`. This does not alter the Git ancestry review, but it confirms that the root control plane and the V9 metadata commit are not synchronized.

## Checks that passed

| Check | Result |
| --- | --- |
| Git ancestry and I/E/A/M identity | PASS |
| Implementation diff baseline parent relation | PASS |
| Provider custody instance scope and checksummed restore | PASS |
| Compact timestamp/pending-edit instance scope and restore | PASS, limited to the state actually included in the snapshot |
| Mutation manifest and actual patch fingerprints | PASS `129/129` |
| Explicit designated killer status | PASS `129/129`; every expected killer status is `failed` |
| Mutation restoration hash | PASS `129/129` |
| Same-session resume | PASS; 3 force-killed processes, revisions `2/4/6`, restart epochs `0/1/2`, zero replayed transition IDs |
| Lost ACK | PASS; one committed effect, stable revision, zero repeated effects |
| Bun and Node builds | PASS |
| Built health candidate identity | PASS; both paths report exact `I` |
| Exact-candidate cleanroom | PASS; target `I`, 9 commands, frozen install, typecheck, Bun/Node build and health, 420 tests |
| Runtime tests | PASS `420/420`, `1,322` expectations |

These checks are necessary but cannot compensate for a non-reproducible source closure or insufficient recognized source coverage.

## Required disposition

- Keep `verified_zyra_head` at `c34535a783e88f9481387ced89cba4fbc333dc74`.
- Record V9 as a failed candidate; do not unlock E02 or E03.
- Freeze one root manifest/profile/receipt set under the actual authoritative verifier and commit or content-address the exact manifest bytes used by strict evidence.
- Do not admit outside-curated new symbols without revising the frozen source boundary through the authorized source-role process.
- Replace the query wrappers and post-compact restore logic with source-equivalent behavior, and make every credited compact effect consumed by and restorable from canonical parent state.
- Regenerate a candidate gate whose own top-level verdict is `ok:true`, then create a fresh immutable I/E/A/M review chain.
