# M1-R01 V12 Execution-01 Critical Self-Review

## 1. Review identity

- Execution: `E01 runtime core TypeScript cutover`
- Review revision: `V12`
- Implementation candidate (`I`): `567b14acfbbabcf59a82868d1c20b042f056a80c`
- Evidence commit (`E`): `70c0d97af385dca39927e9a9d4860020ef3886f7`
- Verified baseline retained during review: `c34535a783e88f9481387ced89cba4fbc333dc74`
- Implementation diff baseline: `0cd21bff5e2d160476f2ce3cef766bf53aab1239`
- Self-review verdict: `PASS_PENDING_INDEPENDENT_REVIEW`
- E02/E03 status: not executed and still blocked by E01 final verification

This verdict is not an E01 completion claim. Completion still requires an immutable metadata binding, an independent adversarial review against that binding, a final PASS metadata commit, a metadata-aware verifier, and a real product health projection that reports the same identities.

## 2. V11 blocking findings

### 2.1 V11-B1: provider endpoint and secret custody

V11 proved direct provider factory behavior but did not prove the default `ProviderModelRuntime` path. Foundry inherited the Anthropic endpoint, and prepared request headers containing a raw credential entered snapshots.

V12 changes the default integration rather than adding another factory-only test:

- Provider selection resolves the provider-specific endpoint before executable-client preparation. Foundry uses its configured Azure resource endpoint rather than a caller-supplied Anthropic default.
- Provider credentials remain in the ephemeral vault/client boundary. Serializable prepared requests and snapshots retain redacted metadata and credential references, not raw authorization values.
- Restore requires credential rebinding and executes through the restored default model path.
- Default-path tests cover Anthropic, Bedrock, Foundry and Vertex endpoint/authentication behavior with deterministic transports, and assert that serialized snapshots do not contain the supplied secret.
- HTTP recovery projects only the allow-listed `retry-after`, `x-ratelimit-reset` and `request-id` headers into canonical recovery state. Credential-bearing response or request headers are not projected.

Disposition: corrected for the E01 hermetic provider transport and restore boundary. Live cloud authentication remains outside this claim.

### 2.2 V11-B2: health verification-contract mismatch

V11 metadata required `zyra.e01-verification/v6` while the production health path accepted V5. V12 moves the production runtime, health fixtures, strict verifier and candidate metadata to `zyra.e01-verification/v7`.

The health projection now distinguishes unavailable evidence, identity-bound pending review, contract mismatch and identity-bound final PASS. It cannot report `complete:true` unless implementation, evidence, review target, review commit, PASS verdict and final metadata agree. Pre-review cleanroom health therefore remains incomplete by design rather than being counted as a completion signal.

Disposition: corrected. Final convergence must still be demonstrated after the independent-review and final-metadata commits exist.

### 2.3 V11-B3: nonexistent default entry symbol

V11 declared `apps/code-worker/src/main.ts::main` without an actual `main` declaration. V12 provides a declared application entry and explicit command profiles for task execution, health and inventory. The task profile reaches `runStdioRuntime` and the TypeScript-owned runtime; health and inventory are projections rather than substitutes for that task path.

The V7 semantic graph requires the configured entry symbol to exist and rejects an `entry_root` shortcut when the target is not the actual declared entry. Runtime-origin and disable probes use the product entry, while task-path tests exercise the runtime assembly behind it.

Disposition: corrected.

### 2.4 V11-B4: leaf-name and disconnected five-hop acceptance

V11 used leaf-name matching, substring assertions and predominantly disconnect-at-entry mutations. V12 replaces this acceptance logic with a TypeScript TypeChecker graph and target-effect mutations:

- Symbols are identified by normalized file and qualified declaration identity, including alias, callback and interface resolution.
- Default-entry edges, immediate callsite edges and state-effect edges must form a connected graph for the declared target.
- The configured default entry symbol must exist.
- Named test callbacks are parsed and assertion subjects are bound to the target/state observation rather than accepted as arbitrary `includes` tokens.
- Each accepted target has a compilable `suppress-state-effect` or other exact semantic mutation. A test must observe the declared killer failure; a target exception alone is not sufficient evidence of the claimed effect.
- The committed five-hop artifact remains byte-identical to the authoritative target custody map.

The strict V7 result is `276/276` accepted five-hop mappings. This is stronger than whole-file tokens or isolated graph fragments, but it is still complemented by runtime probes, behavior tests and mutation execution rather than treated as a whole-program proof.

Disposition: corrected within the statically declared E01 surface.

## 3. V12 mutation-driven production corrections

The first V12 mutation run killed `116/123` and exposed seven concrete weaknesses. They were not reclassified as invalid:

| Target | Defect found | Correction |
| --- | --- | --- |
| `openAiTool` | OpenAI tool projection lacked a directly observed schema effect | Added schema assertions on the real provider request path |
| `ContextCompactionRuntime.buildPostCompactMessages` | Mature post-compact messages were built but ignored | Installed the returned messages into the canonical runtime session |
| `ContextCompactionRuntime.getAutoCompactThreshold` | Source-custody threshold was not consumed by the default query path | Derived the effective query threshold from the compaction runtime |
| `E01RuntimeCoordinator.restore` | Restore continuity was not asserted at the affected owner boundary | Added revision and message-continuity observations after restore |
| `parseRetryAfter` | Provider error headers were lost before canonical retry policy | Projected allow-listed recovery headers and asserted the `600 ms` plan |
| `ProviderTelemetryRuntime.notifyCompaction` | Compaction telemetry generation/effect was not directly observed | Added event and generation assertions |
| `ContextCompactionRuntime.runPostCompactCleanup` | Cleanup output existed without a specific effect assertion | Added cleanup-generation and cleanup-effect assertions |

After these production and test corrections, all seven mutants were killed. The `parseRetryAfter` target initially remained reported as a survivor because static reachability declared both a non-retryable test and the retry-delay test as mandatory killers. Only the latter asserts `retryPlan.delayMs === 600`. V12 adds an exact target-to-killer override in the manifest generator; the mutation runner remains unchanged and still requires every declared killer to fail. The final result is `123/123` compilable frozen-patch mutations killed with restored source hashes.

## 4. Candidate-gate authority correction

During V12 validation, `e01:candidate:gate` still invoked the retired schema-v3 manifest verifier. That verifier overwrote current evidence and reported seven false incomplete chains plus obsolete provider activation checks, even though the explicit V7 verifier passed.

V12 changes the package script so the default candidate gate invokes `scripts/remediation/verify_m1_r01_e01_v4.ts` and writes `candidate-gate-result.json`. There is now one E01 gate implementation for explicit and default candidate validation. Evidence was regenerated from the final implementation candidate after this correction; no result from the retired verifier is credited.

## 5. Effective-line and source-custody audit

| Measure | V12 result |
| --- | ---: |
| Frozen source ranges | 360 |
| Accepted source ranges | 276 |
| Rejected source ranges | 84 |
| Accepted executable source lines | 10,587 |
| Required accepted executable source lines | 10,587 |
| Source-line margin | 0 |
| Five-hop mappings passing V7 | 276 / 276 |
| Gross changed executable TypeScript | 33,449 |
| Token-winnowing deductions | 671 |
| Effective changed TypeScript | 32,778 |
| Final production TypeScript | 50,371 |
| Final test TypeScript | 11,352 |
| Deleted Python lines reported by gate | 35,151 |

Generated content, data-as-code, documentation, vendor/source-pool material, adapter-only code, and mock/fixture-only material are excluded. The accepted source-line threshold has zero margin; invalidating any accepted executable line makes this candidate fail.

The authoritative result is `docs/reviews/evidence/M1-R01-v3/execution-01/strict-gate.json`, bound to `I`, not the numbers copied into this review.

## 6. Runtime, recovery and toolchain evidence

- Strict candidate gate: PASS for exact `I`.
- Default `e01:candidate:gate`: PASS through the same strict verifier.
- Behavior suite: `422` passed, `0` failed, `1,355` expectations across 16 files.
- Mutation: `123` declared, applied, compile-survived and killed; zero survivors or invalid records.
- Runtime-origin probe: PASS.
- Canonical write-path probe: PASS.
- Same-session resume probe: PASS with zero replayed transition IDs.
- Lost-ACK probe: PASS with zero repeated external effects.
- Disable probe: PASS without fallback to a hidden owner.
- Dependency audit: PASS with no root source-repository, symlink or relative package-link dependency.
- Toolchain: Bun `1.2.15`, Node `22.17.0`, TypeScript `5.8.3`; typecheck and Bun/Node builds passed.
- Cleanroom: PASS from a fresh `git archive` of exact `I`, a frozen install, built health and the 422-test suite, followed by cleanup. The first sandboxed attempt failed only because dependency downloads were denied; the authorized network rerun succeeded without changing code or the lockfile.

## 7. State custody and default-path effects

- Session/run transitions, restart epoch, journal revision, outbox delivery and idempotency are TypeScript-owned.
- Provider credentials are ephemeral; serializable provider state contains only bounded metadata and redacted/rebindable references.
- Query compaction now consumes both the mature threshold and mature post-compact message result.
- Retry-after reaches canonical recovery policy and changes its bounded delay.
- Permission denial prevents gateway delegation; mixed batches preserve ordered settlement.
- Disabling the E01 TypeScript runtime changes observable product behavior rather than selecting Python or vendor fallback.
- The dependency audit confirms that final runtime behavior does not require `../claude-code-best` or another root source repository.

## 8. Residual risks and limitations

1. Accepted source coverage has zero margin. The independent reviewer must recompute all 10,587 lines from immutable source blobs.
2. The TypeChecker graph covers declared TypeScript symbols and named tests. It cannot prove arbitrary reflection, code generated after validation, or an unknown dynamic import; dependency and product-entry probes constrain those gaps for E01.
3. Provider tests are hermetic. They execute endpoint, header/signing, retry and restore behavior against deterministic transports, but do not claim live Anthropic/AWS/Azure/GCP authentication.
4. A mutation kill proves that the named test observes the mutated effect under the frozen patch. It does not independently prove broad semantic equivalence to every upstream branch.
5. Historical V3-V11 reports remain audit history only. V12 metadata, strict gate, five-hop map, mutation results and review identities must be used as current authority.
6. Root remediation documents are outside the Zyra Git repository and cannot be included in `I`, `E` or the later review commits.

## 9. Independent-review instructions

The independent reviewer must treat `I`, `E`, this self-review commit and the following metadata-binding commit as immutable inputs and fail V12 if any item below fails:

- Recompute source credit and scrutinize the zero-line margin.
- Re-run the V7 strict verifier and confirm explicit/default gate identity.
- Trace default application entry, provider endpoint/auth/restore, compaction, permission, protocol/journal and same-session resume paths.
- Search all serialized provider snapshots for supplied secrets and execute a restored request.
- Challenge symbol aliases, callbacks, interfaces, disconnected graphs, assertion subjects and entry-root shortcuts.
- Inspect exact state-effect mutations, especially the seven V12 survivors and the `parseRetryAfter` killer binding.
- Confirm all 123 patches compile, are killed by declared tests and restore source hashes.
- Confirm cleanroom, toolchain and probes bind exact `I` and do not rely on root source repositories or caches.
- Confirm pre-review health remains incomplete and that final health can become complete only through identity-bound PASS metadata.

Until that review returns PASS and final metadata-aware verification succeeds, E01 remains incomplete and the verified baseline remains unchanged.
