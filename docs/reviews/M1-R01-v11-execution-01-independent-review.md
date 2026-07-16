# M1-R01 Execution-01 V11 Independent Adversarial Review

## 1. Verdict and immutable review identity

- Verdict: `FAIL`.
- Implementation candidate (`I`): `85a6ec230bc2d27d7a5b3bdd54d1580efce9266b`.
- Evidence commit (`E`): `b74cf37148f313e45595f23be39a73ae8743eb60`.
- Corrected critical self-review (`A2`, review target): `0573394868f170ec3f73fac882af5422510c6a96`.
- Corrected metadata commit (`M2`): `9d0a32d69184ffda7f336bbe3435f5ac0c4867ad`.
- Verified baseline: `c34535a783e88f9481387ced89cba4fbc333dc74`.
- Implementation diff baseline: `0cd21bff5e2d160476f2ce3cef766bf53aab1239`.
- Source snapshot: `c57f5a29e88e9a814bea47abeb9a0a6f725dc102`.

The corrected objects exist and are ancestry-ordered. The diff baseline is the direct child of the verified baseline, and the verified baseline remains unchanged. `I -> E` is direct and `M2` is the direct child of `A2`. The obsolete review commits `7c577c3974723df83e8ab137913be7ef85ba4284` and `ea1be624e88d544b27c62f6f1c2eaa600adb265a` remain between `E` and `A2` as failed binding history. They are not the effective review target or metadata authority.

This review used the corrected `I/E/A2/M2` identities. It did not treat the obsolete `A/M` binding as valid. E01 must remain incomplete, the verified baseline must not advance, and E02/E03 must remain blocked.

## 2. Blocking findings

### B1. Provider default execution is still not source-equivalent and provider snapshots leak the raw secret

Severity: `CRITICAL`.

The accepted `e01-src-0019` range for `src/services/api/client.ts::getAnthropicClient` contributes exactly `163` executable source lines. V11 now returns a real `AnthropicExecutableClient`; direct factory tests demonstrate Foundry token refresh, Bedrock SigV4, Vertex bearer binding, and bounded retry. That repairs the V10 descriptor-only defect at the factory level, but the default model path and restore custody remain incorrect.

[model-runtime.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/provider/model-runtime.ts#L636) always passes the currently configured endpoint into the source-custody factory. [cache-custody-runtime.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/provider/cache-custody-runtime.ts#L648) gives that input endpoint precedence over provider-specific Foundry discovery. Foundry cannot be selected as a `ProviderKind`; it is selected through environment flags. An independent default-path execution therefore produced:

```text
descriptor transport: foundry
descriptor endpoint: https://api.anthropic.com
actual request URL: https://api.anthropic.com/v1/messages
expected Foundry resource: https://resource.services.ai.azure.com
```

The V11 behavior test calls `getAnthropicClient` directly without the endpoint that `ProviderModelRuntime.prepare` supplies, so it does not detect this default-path failure. Only the Anthropic API-key case is executed through `ProviderModelRuntime.queryHaiku`; the Bedrock, Foundry, and Vertex assertions use direct factory calls.

The snapshot claim is also false. [model-runtime.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/provider/model-runtime.ts#L938) clones complete prepared requests into `snapshot.requests`. [model-runtime.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/provider/model-runtime.ts#L1068) places the real Anthropic key into `PreparedProviderRequest.headers`. An independent prepare/snapshot/restore execution found the original key in `JSON.stringify(snapshot)` and specifically in the prepared request headers. Restore can rebind when the caller supplies a secret, but the same secret has already been serialized into the snapshot. The existing restore test only compares source-custody metadata and never executes the restored request or asserts secret absence.

Because source coverage has zero margin, invalidating this one 163-line accepted mechanism gives:

```text
10,587 mechanically accepted - 163 invalid provider-client credit = 10,424 recognized
10,424 < 10,587 required
```

This independently fails both source custody and secret-free snapshot/restore semantics. Hermetic authentication tests are not themselves a blocker; the blocker is that they bypass and fail to reveal the broken default integration and snapshot leak.

### B2. The V11 verification contract cannot converge through the real runtime health path

Severity: `CRITICAL`.

V11 metadata and the authoritative verifier require `zyra.e01-verification/v6`. The production runtime still hard-codes `zyra.e01-verification/v5` in [stdio.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/stdio.ts#L40). Running the real current entry against `M2` returned:

```text
verificationStatus: candidate_metadata_contract_mismatch
verificationContractVersion: zyra.e01-verification/v5
implementationCandidate: null
evidenceCommit: null
reviewTarget: null
complete: false
```

This is not the required pre-review `candidate_pending_independent_review` state. A final PASS metadata-only commit cannot repair it because `I` will continue rejecting V6 metadata. The health behavior test also constructs a V5 fixture, so the 421-test suite does not cover the V11 contract.

The toolchain and cleanroom receipts report success because the health commands exit zero and run against exact `I`, whose historical metadata is expected to fail closed. They do not assert the semantic health payload against corrected V11 metadata. Consequently, `9/9` command exit codes do not close this blocker.

### B3. The declared default entry does not exist as a symbol and the application entry remains a thin dispatcher

Severity: `CRITICAL`.

The gate profile declares `apps/code-worker/src/main.ts::main`. [main.ts](G:/agent-zoo/zyra/apps/code-worker/src/main.ts#L1) contains no `main` declaration. It is a single minified argument dispatcher that delegates `--stdio` to `runStdioRuntime`, dispatches inventory, or returns static contract projections.

The execution authority requires `apps/code-worker/main.ts` to assemble the modules and explicitly lists a remaining thin dispatcher as an automatic failure. The substantial assembly in `stdio.ts` does not make the declared `main` symbol exist and does not satisfy the stated application-boundary requirement.

The V6 verifier checks only that the default-entry file can be read. It never checks the configured entry symbol. It also accepts an `entry_root` mapping whenever target and callsite strings are equal, even with zero default-entry edges. Six query mappings to `ClaudeRuntimeCore.run` use this shortcut. The runtime-origin and disable probes invoke `--e01-inventory`, not a real `--stdio` task, so they do not repair the missing declared entry hop or prove that the thin application entry owns the task path.

### B4. The reported 276/276 five-hop result is still syntactic reachability, not a connected state-effect proof

Severity: `HIGH`.

V11 materially improves test-name scoping: it parses the exact named `test(...)` callback, validates unique contract IDs, checks target hashes, and requires exact consolidation lists. It still does not establish the required source -> default caller -> target -> state effect -> assertion -> mutation chain.

The declaration analyzer records only callee leaf names. It falls back to any declaration with the same leaf if the requested class owner is not found, and it counts identifiers passed as arguments to any call as called symbols. It does not resolve the invoked symbol to the declared callee path/type.

Invocation anchors and assertion tokens are checked with `body.includes(...)`; the verifier does not establish that a token belongs to an assertion or that the asserted state was caused by the target. Generic tokens such as `toEqual` can satisfy a state assertion.

Edge arrays are not required to form one connected graph. The provider `queryModel` mapping combines one execution component (`run -> executePreparedProvider -> queryWithModel -> execute`) with a separate journal component (`run -> recordRuntimeEvent -> recordProvider -> applyProviderLifecycle`) and an unconnected `prepareProviderLifecycle -> bindProvider` edge. All edges can pass individually although there is no checked target-to-state-effect path.

The exact-target mutations predominantly replace the target with an immediate disconnect exception. A named test then fails because the target throws. This proves reachability, not that the claimed state mutation or semantic effect is observed. Mapping-specific prose includes required tokens, but tokenized prose is not semantic equivalence.

Cross-domain sampling produced the following result:

| Domain | Independent assessment |
| --- | --- |
| Query/default entry | FAIL: nonexistent declared `main`, zero-edge `entry_root` accepted |
| Provider/auth/retry | FAIL: Foundry default endpoint wrong and snapshot secret leaked |
| Tool/permission | Runtime behavior tests execute, but V6 state-effect proof remains substring/disconnect based |
| Compaction/session memory | Named enabled/fallback/restore behaviors pass; no new V11 blocker found |
| Protocol/journal | Write, resume, and lost-ACK probes pass mechanically; map edges do not prove one connected five-hop graph |
| Restore | Journal/session restore passes; provider restore serializes the raw credential |

Therefore the committed byte closure is valid, but `five_hop_mappings: 276` is not recognized as 276 behavior-complete chains.

## 3. Gates independently confirmed as passing

### 3.1 Frozen source and artifact mechanics

- Recomputed closure: `360 = 276 accepted + 84 rejected`.
- Target mappings: `276`.
- Accepted source executable lines under the V4/V6 counting rule: exactly `10,587` with no overlapping credited lines.
- Unique accepted source symbols: `262`.
- Unique target path/symbol pairs: `71`.
- All referenced source Git-blob SHA-256 values match source snapshot `c57f5a29...`.
- All target blob SHA-256 values match candidate `I`.
- Frozen source manifest SHA-256: `7b58eb1c445c63f614d7ddce6035946176400a6bd283ad667b4a6c42f7906e49`.
- Frozen target/five-hop SHA-256: `fa1904649dd207a8798e47418cadc81650ee082816f3d2403987d5ef55af99a9`.
- The committed five-hop file is byte-identical to the authoritative target custody map.
- The eight V9 forbidden source symbols are absent as accepted `source_symbol` values.
- The four session-memory global configuration functions are rejected with explicit reasons.

These mechanics pass. The zero margin makes B1 dispositive.

### 3.2 Effective TypeScript lines

| Bucket | Recomputed/evidence result |
| --- | ---: |
| Gross changed executable production TypeScript | `33,309` |
| Token-winnowing duplicate deduction | `671` |
| Effective changed production TypeScript | `32,638` |
| Required effective changed production TypeScript | `25,416` |
| Final production TypeScript | `50,229` |
| Final test TypeScript | `11,253` |
| Changed test TypeScript additions by numstat | `10,151` |

No production LOC blocker was established. Passing LOC does not repair the source-custody, entry, health, or five-hop blockers.

### 3.3 Mutation mechanics

- Declared/result records: `123 / 123`, all unique.
- Applied: `123`.
- Compile survived: `123`.
- Killed: `123`.
- Designated killer observed and failed: `123`.
- Frozen patch fingerprint matched: `123`.
- Original/restored source SHA matched: `123`.
- Survivors/invalid: `0 / 0`.

The mutation corpus passes mechanically. Its disconnect-target design does not prove each claimed state effect, as described in B4.

### 3.4 Runtime probes, tests, toolchain, and cleanroom

- Runtime-origin receipt exits zero and reports TypeScript ownership.
- Write-path revision advances `1 -> 2` with a changed state digest.
- Same-session resume uses three processes, two restart boundaries, revisions `2 -> 4 -> 6`, epochs `0 -> 1 -> 2`, and zero replayed transition IDs.
- Lost-ACK replay keeps revision/count stable and reports zero repeated effects.
- Disable receipt exits nonzero with `e01_typescript_runtime_disabled` for the inventory path.
- Dependency audit scans 101 runtime files and reports no forbidden source-repository path, symlink, or relative package link.
- Mutation evidence baseline passes.
- Independent rerun of the full suite: `421 passed`, `0 failed`, `1,361 expectations`.
- Independent rerun of the V11 provider/compaction file: `5 passed`, `59 expectations`.
- Independent typecheck passes.
- Authoritative verifier exits zero, demonstrating that B2-B4 are verifier blind spots rather than unreported gate failures.
- Evidence toolchain: `9/9` command exits succeed with Bun `1.2.15`, Node `22.17.0`, and TypeScript `5.8.3`.
- Exact-I cleanroom: `9/9` command exits succeed from a fresh Git archive and reports cleanup.

The probes and command receipts pass on their stated mechanics. Their semantic limitations are blocking where identified above.

## 4. Required disposition

- Keep E01 in `ready_for_fix` or equivalent incomplete state.
- Keep verified baseline at `c34535a783e88f9481387ced89cba4fbc333dc74`.
- Keep E02 and E03 blocked.
- Do not write final PASS metadata and do not run the metadata-aware completion verifier as a completion step.
- Remove raw provider secrets from every serialized request/snapshot and add an execute-after-restore secret-absence test.
- Make Foundry/Bedrock/Vertex provider selection and endpoint/auth behavior execute through the real `ProviderModelRuntime` default path, not direct factory-only tests.
- Align the production runtime health contract with V6 and assert the corrected pending/final payload, not only command exit zero.
- Replace the nonexistent `main` declaration and thin dispatcher evidence with a real declared application entry and a verified entry-to-runtime call path.
- Require symbol-resolved, connected call/state graphs and mutations of the claimed effect rather than leaf-name, substring, and throw-at-entry checks.

Root remediation documents and `execution-state.yaml` are outside the Zyra Git repository and were not modified by this review.
