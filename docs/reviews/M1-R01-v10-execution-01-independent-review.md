# M1-R01 Execution-01 V10 Independent Adversarial Review

## 1. Verdict and immutable review identity

- Verdict: `FAIL`.
- Implementation candidate (`I`): `ad0479228541e99594aba87934cd2ae0a6f5ca5b`.
- Evidence commit (`E`): `c26868cef0e372c93ed58ca877fc520957b53f3a`.
- Critical self-review (`A`, review target): `4819f1888300f8dd975fde8225518d4a7362df36`.
- Metadata commit (`M`): `564cb88a201cf528421d48ee451d71bfdd1ee67e`.
- Verified baseline: `c34535a783e88f9481387ced89cba4fbc333dc74`.
- The chain is linear: `I -> E -> A -> M`. The verified baseline is an ancestor of `I` and has not been advanced.
- This review read immutable Git objects. It did not use the moving worktree as the review target.
- E01 must remain incomplete. E02 and E03 must remain blocked.

## 2. Blocking findings

### B1. The committed five-hop evidence is not a single closed 280-record corpus

Severity: `CRITICAL`.

The frozen root corpus in `E` is internally byte-consistent:

| Artifact | Recomputed result |
| --- | ---: |
| Frozen source manifest | `360 = 280 accepted + 80 rejected` |
| Frozen target custody map | `280` |
| Frozen mutation manifest | `123` |
| Candidate gate top-level result | `ok: true` |
| G0/index/frozen-file SHA-256 values | `6 / 6 exact matches` |

However, [source-to-target-five-hop.jsonl](G:/agent-zoo/zyra/docs/reviews/evidence/M1-R01-v3/execution-01/source-to-target-five-hop.jsonl) at the same immutable evidence commit still contains `286` mappings. It is not marked historical or superseded by its filename or schema, and the V10 self-review explicitly calls this JSONL an evidence artifact while claiming `280 / 280`.

The stale artifact contains all eight V9 outside-curated mappings that V10 says receive zero credit:

| Mapping | Stale credited symbol |
| --- | --- |
| `e01-rej-0004` | `buildSystemPromptBlocks` |
| `e01-rej-0005` | `queryHaiku` |
| `e01-rej-0006` | `queryWithModel` |
| `e01-rej-0008` | `adjustParamsForNonStreaming` |
| `e01-rej-0010` | `getMaxOutputTokensForModel` |
| `e01-rej-0032` | `collectReadToolFilePaths` |
| `e01-rej-0034` | `truncateToTokens` |
| `e01-rej-0035` | `shouldExcludeFromPostCompactRestore` |

It also omits the two V10 frozen mappings `e01-src-0019` (`getAnthropicClient`) and `e01-src-0156` (`consumePendingCacheEdits`). Thus `286 = 280 - 2 + 8`, rather than a harmless duplicate serialization of the frozen target map.

The V4 verifier reads only the root target custody map and never validates or invalidates the committed five-hop JSONL. Therefore `candidate-gate-result.json` can report `280` and `ok:true` while a competing committed evidence file still grants the forbidden V9 credit. The V10 evidence package is not closed.

### B2. `getAnthropicClient` remains a descriptor factory, not the credited provider-client construction mechanism

Severity: `CRITICAL`.

The accepted upstream source range `e01-src-0019` is `src/services/api/client.ts::getAnthropicClient`, lines `88-316`, contributing `163` executable lines under the V4 counting rule.

The upstream function returns `Promise<Anthropic>` and materially constructs one of four clients:

- `new AnthropicBedrock(...)`, including AWS credential refresh/signing inputs.
- `new AnthropicFoundry(...)`, including Azure AD token-provider construction.
- `new AnthropicVertex(...)`, including `GoogleAuth` construction and refresh.
- `new Anthropic(...)`, including OAuth/API-key configuration, fetch/proxy wiring, retries, and timeout.

The target [cache-custody-runtime.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/provider/cache-custody-runtime.ts#L377) still returns `AnthropicClientDescriptor | null`. It creates no SDK client, signer, Azure token provider, Google authentication client, or provider-specific executable transport. For Anthropic it stores redacted placeholder authentication headers rather than an executable client credential.

[model-runtime.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/provider/model-runtime.ts#L631) consumes some descriptor fields into a `PreparedProviderRequest`, specifically endpoint, selected provider, headers, and timeout. Execution still goes through the generic `ProviderTransportRuntime`. Bedrock requests are not bound to AWS signing, Vertex requests are not bound to `GoogleAuth`, and Foundry requests are not bound to an Azure AD provider. `client.maxRetries` is persisted in the descriptor but does not control request execution or the coordinator recovery state machine.

The behavior test [source-custody-default-path.behavior.test.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/test/e01/source-custody-default-path.behavior.test.ts#L163) asserts descriptor fields and later uses a fake transport. It does not demonstrate provider-specific client construction, authentication, signing, or retry consumption.

This is not source-equivalent to the credited 163-line source mechanism. Deducting that one range gives:

```text
10,626 claimed - 163 invalid getAnthropicClient credit = 10,463 recognized
10,463 < 10,587 required
```

The source-coverage gate therefore fails even before considering any other semantic mapping dispute.

### B3. The V4 `280 / 280 five-hop` result is a schema/token count, not per-source behavioral proof

Severity: `HIGH`.

The verifier [verify_m1_r01_e01_v4.ts](G:/agent-zoo/zyra/scripts/remediation/verify_m1_r01_e01_v4.ts#L474) checks the following:

- A target symbol leaf appears somewhere in the target file.
- A callsite symbol leaf appears somewhere in the callsite file.
- A test name and configured assertion strings appear in the test file.
- Referenced mutation IDs exist.
- `state_store`, `state_effect_kind`, and `state_effect_assertion` are nonempty strings.

It does not verify an AST call from the declared immediate callsite to the target, does not prove the test invokes the target, does not prove the asserted state change is caused by that target, and does not establish semantic equivalence between each source symbol and the consolidated target.

The frozen map contains `280` mappings but only `71` unique target symbols, `42` unique behavior-test names, and `101` unique mutation IDs. Consolidation is allowed, but each credited source mechanism still needs a real semantic chain. Several records instead reuse a broad path template. For example, `setSessionMemoryCompactConfig`, `getSessionMemoryCompactConfig`, `resetSessionMemoryCompactConfig`, and `initSessionMemoryCompactConfig` are routed to `ContextCompactionRuntime.planCompaction`, the same generic runtime test, and shared compact mutations. A plan-selection method does not demonstrate configuration initialization/reset behavior.

The map's `semantic_equivalence` field is also predominantly generic: `193 / 280` records use the same statement that an observable effect is preserved. Nonempty prose cannot replace a behavior-specific assertion.

Consequently, the committed `five_hop_mappings: 280` metric is not sufficient evidence that every accepted source range has the required source symbol -> immediate callsite -> target state effect -> behavior test -> mutation chain.

## 3. Gates independently confirmed as passing

### 3.1 Frozen root mechanics and source-role restrictions

- All six frozen root-manifest bytes match both `root-manifest-index.json` and `g0-summary.json` SHA-256 values.
- Gate profile authority is V6 generation plus V4 verification.
- Receipt, G0, strict gate, candidate gate, and metadata all bind candidate `I` and verified baseline `c34535a7...`.
- Recalculation using source commit `c57f5a29e88e9a814bea47abeb9a0a6f725dc102` reproduces `10,626` mechanically counted source lines before semantic deductions.
- All 21 referenced source blobs match their frozen SHA-256 values.
- The only accepted post-cutoff symbols are the six authorized retry helpers: `getDefaultMaxRetries`, `getMaxRetries`, `getRetryAfterMs`, `getRateLimitResetDelayMs`, `categorizeRetryableAPIError`, and `getErrorMessageIfRefusal`.
- The eight V9 outside-curated records are rejected in the frozen source manifest and receive zero credit in the frozen target map. B1 remains blocking because the separate committed five-hop artifact contradicts that disposition.

### 3.2 Compact, restore, and post-compact semantics

- Auto-compaction state is consumed by `ContextCompactionRuntime.autoCompactIfNeeded`, rather than merely logged.
- Partial-boundary state constrains the actual split and contributes the committed boundary identity.
- Streaming summary output and attempts are consumed and snapshotted.
- Session-memory results affect summary and boundary state on the default compact path.
- Pending microcompact edits are consumed once and recorded in canonical boundary/session-memory state.
- Auto, partial, stream, session-memory, pending, and consumed state are included in snapshot/restore.
- Post-compact restore excludes already-read paths, plan/memory paths, dependency/cache paths, and admits eligible unread files.
- The truncation marker is included within the token budget.
- The post-compact helper symbols remain rejected and receive no frozen source credit.

No additional compact-state blocker was found in V10.

### 3.3 Effective TypeScript line buckets

| Bucket | Independently reconciled value |
| --- | ---: |
| Gross changed executable production TypeScript | `33,044` |
| Token-winnowing duplicate deduction | `663` |
| Effective changed production TypeScript | `32,381` |
| Required effective changed production TypeScript | `25,416` |
| Final production TypeScript | `49,964` |
| Final test TypeScript, kept separate | `11,170` |

The production path filter excludes tests, fixtures, generated/vendor/source-pool paths, scripts, and test files. The report identifies `114` duplicate pairs across `2,252` compared units. No LOC blocker was established by this review. Passing LOC does not repair B1-B3.

### 3.4 Mutation evidence

- Frozen mutation manifest: `123` unique IDs.
- Result corpus: `123` unique IDs with no missing or extra result.
- Applied: `123`.
- Compile survived: `123`.
- Killed: `123`.
- Survivors: `0`.
- Invalid: `0`.
- Frozen patch fingerprint matches: `123`.
- Designated killer observed and failed: `123`.
- Every restored source hash equals its original hash.

The mutation corpus mechanically passes. It proves its 123 declared target perturbations are observed; it does not establish the missing per-source semantic equivalence in B2-B3.

### 3.5 Runtime, resume, dependency, toolchain, and cleanroom evidence

- Runtime origin starts `apps/code-worker/src/main.ts --e01-inventory` and observes canonical owner `typescript`.
- Write path advances revision `1 -> 2`, changes the state digest, and commits through the TypeScript journal.
- Same-session resume uses three distinct worker processes and two force-kill restarts, advances revisions `2 -> 4 -> 6`, advances epochs `0 -> 1 -> 2`, and reports zero replayed transition IDs.
- Lost-ACK replay leaves the committed revision and count unchanged and reports `repeated_effect_count = 0`.
- Disable probe makes the real default entry fail with `e01_typescript_runtime_disabled`.
- Dependency audit scans `101` runtime files and reports no forbidden root path, symlink, or relative package link.
- Toolchain runs `9 / 9` commands successfully with Bun `1.2.15`, Node `22.17.0`, and TypeScript `5.8.3`.
- Exact-I cleanroom uses a fresh `git archive`, runs `9 / 9` commands, passes `421` tests with `1,350` expectations, and removes the temporary tree.

No blocker was found in these probes.

## 4. Metadata and repository boundary

- `E` is the direct child of `I`, `A` is the direct child of `E`, and `M` is the direct child of `A`.
- `candidate-metadata.json` binds `I`, `E`, `A`, cleanroom target `I`, verified baseline `c34535a7...`, and verdict `PENDING` at `M`.
- `G:/agent-zoo/docs/remediations/**` is outside the Zyra Git repository. The metadata describes that non-Git boundary and the evidence commit freezes the six root-manifest bytes.
- No baseline drift was found. Root execution state must not be advanced on this FAIL.

## 5. Required disposition

- Keep E01 in a fix-required state.
- Keep verified baseline at `c34535a783e88f9481387ced89cba4fbc333dc74`.
- Keep E02 and E03 blocked.
- Replace or explicitly retire the stale 286-record five-hop artifact so one immutable evidence corpus exists.
- Implement a real executable provider-client/transport construction boundary, or reject `getAnthropicClient` source credit and recover the source threshold with other valid mechanisms.
- Upgrade the five-hop verifier to validate behavior-specific call/state/test/mutation relationships rather than nonempty strings and token presence.

