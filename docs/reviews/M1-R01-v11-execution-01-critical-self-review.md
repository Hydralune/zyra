# M1-R01 V11 Execution-01 Critical Self-Review

## 1. Review identity

- Execution: `E01 runtime core TypeScript cutover`
- Review revision: `V11`
- Implementation candidate (`I`): `85a6ec230bc2d27d7a5b3bdd54d1580efce9266b`
- Evidence commit (`E`): `b74cf37148f313e45595f23be39a73ae8743eb60`
- Verified baseline retained during review: `c34535a783e88f9481387ced89cba4fbc333dc74`
- Implementation diff baseline: `0cd21bff5e2d160476f2ce3cef766bf53aab1239`
- Self-review verdict: `PASS_PENDING_INDEPENDENT_REVIEW`
- E02/E03 status: not executed and still blocked by E01 final verification

This verdict is not an E01 completion claim. Completion still requires an immutable metadata binding, an independent review against that binding, a final PASS metadata commit, and the final metadata-aware verifier.

## 2. V10 failure disposition

### 2.1 Competing five-hop artifact

V10 left a 286-row `source-to-target-five-hop.jsonl` beside a 280-row frozen target map and retained forbidden V9 mappings. V11 removes that ambiguity:

- `scripts/remediation/m1_r01_e01_v6.ts` writes the committed five-hop artifact from the same final target-map rows used for the frozen custody map.
- `scripts/remediation/verify_m1_r01_e01_v4.ts` compares their bytes, not merely row counts or selected fields.
- The final closure is 360 source ranges: 276 accepted and 84 rejected, with exactly 276 target mappings.
- Four session-memory global configuration functions without a material target effect are rejected: `setSessionMemoryCompactConfig`, `getSessionMemoryCompactConfig`, `resetSessionMemoryCompactConfig`, and `initSessionMemoryCompactConfig`.
- No prior generic V9 mapping is retained for those functions.

Disposition: corrected.

### 2.2 Non-executable provider client

V10 treated `getAnthropicClient` as accepted while it returned only a descriptor. V11 changes the production contract:

- `getAnthropicClient` returns an `AnthropicExecutableClient`.
- Anthropic API key and OAuth paths bind the appropriate request headers.
- Bedrock supports AWS bearer authorization and credential-backed SigV4 signing, including session-token propagation.
- Foundry binds either an API key or an Azure token-provider result.
- Vertex binds a GCP token-provider result and optional user-project header.
- The executable client owns bounded retry, retry-after/exponential delay, and per-attempt credential refresh.
- `model-runtime.ts` delegates execution through the executable client, records attempt/refresh/delay state, excludes ephemeral executable state from snapshots, and reconstructs it during restore.
- Foundry uses the Anthropic request shape instead of the compatible/OpenAI request shape.

The behavior test invokes the actual executable request path through deterministic fetch/token/credential doubles. It checks a Foundry 429 retry and token refresh, Bedrock SigV4/session-token headers, Vertex bearer binding, and default Anthropic API-key retry state. It does not require live cloud credentials or an external network call.

Disposition: corrected for the E01 executable transport/authentication boundary.

### 2.3 Template five-hop checks

V10 accepted nonempty fields and reusable tokens without proving a mapping-specific chain. V11 raises the contract to `zyra.e01-verification/v6`:

- Each accepted mapping has a deterministic, unique behavior-contract ID.
- The verifier parses TypeScript declarations and named `test(...)` callbacks with the TypeScript AST.
- It locates the qualified caller and target declarations and proves that the declared caller references or invokes the declared callee.
- It permits `entry_root` only when the target is the declared default callsite.
- It requires the exact target mutation and state-assertion tokens inside the specifically named test callback rather than elsewhere in the test file.
- It requires a named killer mutation and validates the exact consolidation disclosure when multiple source ranges share one target behavior.
- It requires mapping-specific semantic-equivalence tokens.

The provider call graph was corrected to the real sequence `coordinator -> queryHaiku/queryWithModel -> execute`; the pending-edit mapping now names `ContextCompactionRuntime.compactConversation -> CompactionSourceCustodyRuntime.consumePendingCacheEdits` rather than a synthetic shortcut.

Disposition: corrected, with the limitations recorded in section 8.

## 3. Effective-line and source-custody audit

| Measure | V11 result |
| --- | ---: |
| Frozen source ranges | 360 |
| Accepted source ranges | 276 |
| Rejected source ranges | 84 |
| Accepted executable source lines | 10,587 |
| Required accepted executable source lines | 10,587 |
| Source-line margin | 0 |
| Five-hop mappings passing V6 | 276 / 276 |
| Gross changed executable TypeScript | 33,309 |
| Token-winnowing deductions | 671 |
| Effective changed TypeScript | 32,638 |
| Final production TypeScript | 50,229 |
| Final test TypeScript | 11,253 |
| Deleted Python lines reported by gate | 35,151 |

Generated content, data-as-code, documentation, vendor/source-pool material, adapter-only code, and mock/fixture-only material are excluded from effective changed TypeScript. The accepted source-line threshold has zero margin; any later invalidation of even one accepted line makes this candidate fail.

The authoritative evidence is `docs/reviews/evidence/M1-R01-v3/execution-01/strict-gate.json`, not a line-count claim in this review.

## 4. Dynamic reachability and semantic effect

- Default entry: `apps/code-worker/src/main.ts` boots the TypeScript E01 coordinator.
- Canonical write path: `ClaudeRuntimeCore.run -> E01RuntimeCoordinator.recordRuntimeEvent -> Journal.commit` changes revision and state digest.
- Disable probe: setting `ZYRA_DISABLE_E01_TYPESCRIPT_RUNTIME=1` makes the default entry fail with `e01_typescript_runtime_disabled`; the claimed runtime cannot silently fall back.
- Provider execution: the query/model runtime consumes the executable provider client, not merely its descriptor.
- Compaction/session-memory custody: auto, reactive, partial, streaming, pending-edit, snapshot and restore state are consumed on runtime paths and covered by named behavior tests.
- The default inventory reports TypeScript as canonical owner and does not require a root source repository, vendor runtime, or legacy inspection sidecar.

These are semantic effects, not ledger-only reachability claims.

## 5. Mutation evidence

The V11 mutation run reports:

- Declared: 123
- Applied: 123
- Compile-survived: 123
- Killed: 123
- Survived: 0
- Invalid: 0
- Kill rate: 1.0
- Frozen patch fingerprints matched: 123
- Restored source hashes matched: true

Mutation count alone is not treated as proof. Its role is to demonstrate that the named behavior tests observe the target mutations described by the five-hop contracts. The AST verifier separately checks the binding from each mapping to an exact target mutation and named killer test.

## 6. Runtime, recovery, dependency and cleanroom evidence

- Runtime-origin probe: PASS.
- Canonical write-path probe: PASS.
- Same-session resume probe: PASS across two forced process restarts; revisions advanced `2 -> 4 -> 6`; replayed transition IDs: 0.
- Lost-ack probe: PASS; duplicate retry observed; repeated external effect count: 0.
- Disable/kill probe: PASS; default entry failed when the canonical TypeScript coordinator was disabled.
- Dependency audit: PASS across 101 scanned runtime files; no forbidden root-repository path, runtime symlink, or relative package link.
- Toolchain: Bun `1.2.15`, Node `22.17.0`, TypeScript `5.8.3`; frozen install, typecheck, Bun build/health, Node build/health and tests all passed.
- Behavior suite: 421 passed, 0 failed, 1,361 expectations across 16 files.
- Cleanroom: PASS from `git archive` of exact candidate `85a6ec230bc2d27d7a5b3bdd54d1580efce9266b`, without inherited `node_modules`, `dist`, `.tmp`, Git metadata or `NODE_PATH`.

The pre-review health response intentionally remains incomplete/PENDING because final metadata and independent review do not yet exist. That status is not a toolchain failure and must not be rewritten to complete before independent PASS.

## 7. State custody and external-boundary assessment

- Session/run transition state, restart epoch, journal revision, outbox delivery and idempotency are owned by the TypeScript runtime journal/coordinator boundary.
- Provider descriptors are serializable and redacted; executable credential providers and request clients remain ephemeral and are rebound during restore.
- Provider attempt, credential-refresh and retry-delay effects are exposed through request state.
- The committed dependency audit finds no runtime dependency on `../claude-code-best` or another root source repository.
- Root remediation documents remain outside the Zyra Git repository. They cannot be represented by the Zyra evidence commit and must be updated and disclosed separately only after final independent PASS.

## 8. Residual risks and limitations

1. The accepted source coverage has zero margin. The independent reviewer must recompute it from immutable Git blobs rather than trust this report.
2. AST verification proves the declared syntactic call/reference edge and anchors inside the exact named test callback. It is not a whole-program dynamic call graph and does not alone prove full semantic equivalence; mutation, default-path probes and behavior assertions are required complementary evidence.
3. Provider authentication tests are hermetic. They execute real header/signing/retry construction against deterministic request doubles, but do not perform live Anthropic/AWS/Azure/GCP network authentication. Live provider dispatch is a later competition evidence boundary and is not claimed here.
4. The 421-test count is not credited by repetition or volume. Only tests bound to concrete state effects and mutations support the V11 claim.
5. Historical V3-V10 reports remain in the evidence directory for audit history. V11 metadata, strict gate, five-hop map, mutation result and independent review must be used as the current authority.

## 9. Independent-review instructions

The reviewer should treat `I`, `E`, this self-review commit, and the following metadata-binding commit as immutable inputs and should fail the candidate if any of these checks fail:

- Recompute the 10,587 accepted executable source lines and scrutinize the zero margin.
- Reconcile all 360 source ranges into exactly 276 accepted and 84 rejected ranges.
- Byte-compare the five-hop artifact and authoritative target custody map and check that V9 forbidden mappings are absent.
- Trace representative mappings across provider, query loop, tools, permission, compaction/session memory, protocol, journal and restore domains.
- Inspect `getAnthropicClient`, actual request/auth/retry consumption and snapshot rehydration rather than relying on descriptors.
- Challenge the AST verifier with identifier callbacks, false caller edges, misplaced assertion tokens and duplicate behavior-contract IDs.
- Confirm all 123 mutation targets are applied, killed and restored.
- Confirm probes and cleanroom bind the exact implementation candidate.

Until that review returns PASS and final metadata-aware verification succeeds, E01 remains incomplete and the verified baseline remains unchanged.
