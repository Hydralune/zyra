# M1-R01 V9 Execution-01 Critical Self-Review

## Verdict

`READY_FOR_INDEPENDENT_REVIEW`

This is not a completion verdict. E01 remains incomplete until an independent reviewer validates the immutable candidate/evidence chain and records `PASS`.

## Immutable identity chain

| Identity | Commit |
| --- | --- |
| Verified baseline | `c34535a783e88f9481387ced89cba4fbc333dc74` |
| Implementation diff baseline | `0cd21bff5e2d160476f2ce3cef766bf53aab1239` |
| V9 implementation candidate | `796a9c457c6bb871edd26808d91b5affd49f8923` |
| V9 raw evidence commit | `2698b5b6276eee001a9c6adee4f547cb6628bfce` |

The cleanroom target is exactly the implementation candidate. Root manifests under `G:/agent-zoo/docs/remediations` remain outside the Zyra Git repository and are an explicit workspace delivery boundary.

## V8 independent-review findings and V9 disposition

| V8 finding | V9 disposition | Evidence |
| --- | --- | --- |
| Sixteen accepted compact mappings reached an array argument that `recordOf` rejected, making the hook a deterministic no-op. | Fixed. `ContextCompactionRuntime` owns a `CompactionSourceCustodyRuntime`, builds a real request envelope, consumes the mutated message state, uses streamed summary retry, and persists custody state in its checksummed snapshot. | `source-custody-default-path.behavior.test.ts`; V9 strict gate; target-disconnect mutations. |
| `getAnthropicClient` credited 163 executable source lines to a static descriptor builder without SDK/auth/provider equivalence. | Rejected. The source range now has zero production credit. The descriptor remains an uncredited helper. | Source manifest exclusion reason and V9 strict gate. |
| Provider and compact custody lived in process singletons outside canonical restore state. | Fixed. Both parent runtimes own instance-scoped custody and include it in backward-compatible checksummed snapshots. Prepared provider requests also persist the concrete custody effect. | Provider and compact default-path snapshot/restore tests. |
| Exact-candidate built health reported an older candidate. | Fixed. The probe passes the exact candidate through `E01_IMPLEMENTATION_CANDIDATE`; Bun and Node health both report `796a9c457c6bb871edd26808d91b5affd49f8923`. The health fixture clears this outer override while testing an independent PASS fixture. | Toolchain and cleanroom receipts. |

## Source-credit correction

`getAnthropicClient` was removed from accepted source credit. Nine mechanisms were accepted only after they became reachable from concrete default-path owners:

| Source mechanism | Zyra target | Default callsite and state effect |
| --- | --- | --- |
| `buildSystemPromptBlocks` | `ProviderModelRuntime.buildSystemPromptBlocks` | `prepare`; persisted request body system cache controls. |
| `adjustParamsForNonStreaming` | `ProviderModelRuntime.adjustParamsForNonStreaming` | `prepare`; persisted maximum output and thinking budget. |
| `getMaxOutputTokensForModel` | `ProviderModelRuntime.getMaxOutputTokensForModel` | `prepare`; persisted request maximum tokens. |
| `queryWithModel` | `ProviderModelRuntime.queryWithModel` | coordinator provider execution; request state and response digest. |
| `queryHaiku` | `ProviderModelRuntime.queryHaiku` | coordinator Haiku branch; enforced non-streaming/no-tool/no-thinking contract. |
| `getEffectiveContextWindowSize` | `ContextCompactionRuntime.getEffectiveContextWindowSize` | auto-compact admission and custody envelope. |
| `collectReadToolFilePaths` | `ContextCompactionRuntime.collectReadToolFilePaths` | post-compact attachment discovery from real tool lineage. |
| `truncateToTokens` | `ContextCompactionRuntime.truncateToTokens` | post-compact attachment token budget. |
| `shouldExcludeFromPostCompactRestore` | `ContextCompactionRuntime.shouldExcludeFromPostCompactRestore` | post-compact path and read-lineage exclusion. |

V4 strict verification reports `10,690` accepted executable source lines, above the `10,587` minimum after rejecting the invalid client mapping.

## Five-hop and mutation evidence

- Source ranges: `360` total, `286` accepted, `74` rejected.
- Five-hop target mappings: `286/286`.
- Default entries: `1`.
- Frozen mutations: `129` declared, `129` applied, `129` compiled, `129` killed.
- Mutation survivors: `0`.
- Invalid mutations: `0`.
- Frozen patch mismatches: `0`.
- Restoration hash mismatches: `0`.

The runner requires every named killer to be observed with explicit failed status. A generic non-zero test exit cannot count as a kill. V9 also repaired the manifest generator so observation-budget mutations `045` and `046` cannot disappear during regeneration.

## Effective-line buckets

| Bucket | Lines | Credited |
| --- | ---: | --- |
| Gross changed executable TypeScript | 32,685 | Yes, before duplicate deduction |
| Token-winnowing duplicate deduction | 663 | No |
| Effective changed TypeScript | 32,022 | Yes |
| Final production TypeScript | 49,605 | Context only; not a substitute for behavior |
| Final test TypeScript | 11,109 | Test evidence only |
| Deleted Python owner code | 35,151 | Deletion evidence only |
| Generated/data/docs/vendor-like/source-pool/adapter-only/mock-only | Excluded | No |

The E01 minimum is `25,416` effective changed TypeScript lines. The line gate passes, but no line count is used to replace source equivalence, default-path reachability, mutation, or runtime evidence.

## Dynamic evidence

- Runtime origin: PASS; default entry reports TypeScript as canonical owner.
- Write path: PASS; the real journal revision and state digest change.
- Same-session resume: PASS across three force-killed processes and two restarts; replayed transition IDs are `0`.
- Lost ACK: PASS; repeated effect count is `0` and revision stays stable.
- Disable probe: PASS; disabling the TypeScript runtime breaks the default entry with the expected error.
- Dependency/path probe: PASS; no source-repository path, vendor runtime, link, or symlink is required.
- Mutation gate: PASS `129/129`.
- Runtime tests: PASS `420/420`, `1,322` expectations.

## Toolchain and cleanroom

Both the local toolchain and exact-commit cleanroom passed:

- Bun `1.2.15`.
- Node `22.17.0`.
- TypeScript `5.8.3` typecheck.
- Frozen Bun install.
- Bun build and built health.
- Node-target build and built health.
- E01 runtime behavior suite.
- Cleanroom source is `git archive` of `796a9c457c6bb871edd26808d91b5affd49f8923`.
- Cleanroom starts without `node_modules`, `dist`, `.tmp`, Git metadata, or inherited `NODE_PATH`.

## State custody and disconnect effects

- Provider cache TTL latch and cache samples are owned by `ProviderModelRuntime.sourceCustody` and restored from `ProviderModelSnapshot`.
- Compaction microcompact time gates and pending edits are owned by `ContextCompactionRuntime.sourceCustody` and restored from `ContextCompactionSnapshot`.
- Provider request custody effects are persisted in each prepared request and therefore in provider snapshots.
- Compaction summary retry changes the actual summary path; partial-boundary output contributes the committed boundary identity.
- Attachment restore selection changes the actual compact result attachment set.
- Coordinator provider execution now calls `queryHaiku` or `queryWithModel`; disconnecting either target is mutation-killed.

## Superseded verifier disclosure

`scripts/remediation/verify_m1_r01_e01_v3.ts` returns FAIL in V9. It hashes mutable workspace files rather than immutable source Git blobs and rejects shared success/failure test declarations even when the frozen target-specific mutation runner proves polarity. It is not silently ignored: `candidate-gate-result.json` records `FAIL_NOT_AUTHORITATIVE`. The authoritative contract is V4, which validates immutable source blobs, target hashes at the candidate, five-hop paths, frozen mutation fingerprints, effective lines, and clean receipt identity.

## Residual risks for independent review

- The reviewer must inspect that the nine newly accepted source mechanisms are not merely name matches and that the declared default callsites are materially exercised.
- The reviewer must independently subtract any mapping whose target behavior is weaker than its source range; the `10,690` margin over `10,587` is only `103` executable lines.
- The static Anthropic client descriptor remains in production but carries zero source credit; it must not be mistaken for the rejected SDK client factory.
- Windows Bun and Node paths are validated; no separate Linux cleanroom was run for this Windows-targeted execution.
- Productized health intentionally remains incomplete until independent review metadata is committed.

## Self-review conclusion

V9 closes the concrete V8 failures and passes the current authoritative mechanical, behavioral, mutation, resume, toolchain, and cleanroom gates. Independent review remains mandatory because the source-credit margin is narrow and semantic equivalence cannot be established by the verifier alone.
