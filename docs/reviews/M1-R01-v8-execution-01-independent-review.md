# M1-R01 V8 Execution-01 Independent Review

Review date: 2026-07-16

Reviewer nonce: `c22566942dffd6f1d78a04944dc8fee147300bbc942f9b6a411bde90e3aa6cee`

## 1. Verdict

**FAIL**

The candidate passes the immutable identity chain, mechanical line-count checks, the declared mutation-accounting check, the pinned TypeScript toolchain commands, the 416-test E01 suite, runtime-origin probing, three-process same-session resume, lost-ACK idempotency, and the Python-disable probe.

It nevertheless fails the source-custody hard gate. Sixteen accepted compact mappings, covering 960 unique executable upstream lines, terminate in a custody hook that is deterministically a no-op on both real default call sites. Removing those invalid credits lowers the maximum recognized source coverage from 10,604 to 9,644 executable lines, below the required 10,587. A separate 163-line provider-client mapping is also materially non-equivalent to its upstream source.

## 2. Findings

### P0: accepted compact mappings do not reach a state effect through the default runtime path

The accepted compact mappings target `CompactionSourceCustodyRuntime` in [compaction-custody-runtime.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/compact/compaction-custody-runtime.ts:294). The target first converts its input through `recordOf`; [recordOf](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/compact/compaction-custody-runtime.ts:53) explicitly rejects arrays.

Both production hooks pass `arguments[0]` from methods whose first argument is the message array:

- [autoCompactIfNeeded](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/compact/context-runtime.ts:372)
- [compactConversation](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/compact/context-runtime.ts:514)

Consequently, both hooks return before invoking any mapped method or mutating any custody state. This breaks target-symbol to default-call-site to state-effect to behavior-test traceability.

The affected accepted mapping IDs are:

`e01-rej-0029`, `e01-rej-0030`, `e01-rej-0036`, `e01-src-0153`, `e01-src-0164`, `e01-src-0165`, `e01-src-0166`, `e01-src-0168`, `e01-src-0179`, `e01-src-0180`, `e01-src-0198`, `e01-src-0201`, `e01-src-0204`, `e01-src-0222`, `e01-src-0223`, and `e01-src-0224`.

These rows cover 960 unique executable upstream lines. The strict verifier mechanically reports 10,604 accepted executable source lines. The conservative semantic upper bound is therefore:

```text
10,604 - 960 = 9,644 < 10,587
```

The source-custody floor fails without relying on any other disputed mapping.

The direct source-custody tests do not repair the chain. [source-custody-specific.behavior.test.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/test/e01/source-custody-specific.behavior.test.ts:1) instantiates the custody runtime directly. The nominal hook test passes a fabricated object containing `messages`, rather than the array supplied by either default context-runtime method, at [source-custody-specific.behavior.test.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/test/e01/source-custody-specific.behavior.test.ts:223).

### P0: `e01-src-0019` maps a full provider client factory to a small descriptor builder

The immutable upstream range `claude-code-best@c57f...:src/services/api/client.ts:88-316` implements `getAnthropicClient` across 163 executable lines. It owns provider-specific SDK client construction, Anthropic and OAuth headers, OAuth refresh, AWS and GCP credential refresh, Azure token acquisition, Foundry handling, proxy/fetch selection, timeout handling, and Bedrock/Vertex/Foundry transports.

The accepted target [getAnthropicClient](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/provider/cache-custody-runtime.ts:300) is a synchronous descriptor builder for `anthropic`, `bedrock`, and `vertex`. It does not construct an SDK client and does not implement the upstream credential-refresh, Foundry, proxy/fetch, timeout, Azure, or GCP discovery behavior.

The default caller at [ProviderModelRuntime.prepare](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/provider/cache-custody-runtime.ts:591) supplies `ProviderRequestOptions`, which does not expose the required upstream credential inputs, and the returned prepared request does not carry the constructed descriptor as an equivalent provider client. The designated test at [source-custody-specific.behavior.test.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/test/e01/source-custody-specific.behavior.test.ts:19) bypasses the default caller and only checks a transport label, redacted header, and fingerprint.

This mapping independently lacks semantic equivalence and a valid five-hop chain. Its 163 lines are not needed for the already decisive 9,644-line upper bound.

### P1: newly introduced compact/provider custody state is outside canonical snapshot and restore

`CompactionSourceCustodyRuntime` exposes internal snapshot/restore behavior at [compaction-custody-runtime.ts](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/compact/compaction-custody-runtime.ts:357), but [ContextCompactionRuntime.snapshot](G:/agent-zoo/zyra/packages/runtime/claude-runtime/src/compact/context-runtime.ts:824) does not include it and its restore path does not restore it. The provider custody runtime has the same standalone-state pattern. No production caller invokes these custody snapshot/restore methods.

Therefore the manifest's state-effect and persistence claims are stronger than the actual canonical state boundary. The three-process journal probe proves the existing session journal resumes exactly; it does not prove that these new singleton custody states survive a process epoch.

### P2: exact implementation archive reports a stale implementation candidate

The built Bun and Node health commands in the exact implementation archive both ran successfully, but reported:

```text
implementationCandidate=b4a5c2e...
verificationStatus=implementation_generated_pending_validation
```

The reviewed implementation is `745480e8d439b04f56d714c63cd29e2b4f27e870`. The external candidate metadata correctly binds the reviewed identities, so this is not the basis of the FAIL verdict, but the runtime health identity is not self-consistent evidence for the reviewed candidate.

## 3. Immutable target identity and entry gate

| Identity | Commit |
|---|---|
| Verified baseline | `c34535a783e88f9481387ced89cba4fbc333dc74` |
| Implementation `I` | `745480e8d439b04f56d714c63cd29e2b4f27e870` |
| Evidence `E` | `0112ed616286170fca5568013be43f5b09b224d1` |
| Review target `A` | `ccf01feae06f8a1f18cc30319b2d53b98837d594` |
| Candidate metadata `M` | `cb9bd0cf8ee0893c32e7c8256d1e0e479106e2b4` |

Entry-gate result: **PASS**.

The ancestry is baseline -> `I` -> `E` -> `A` -> `M`. `A` is an empty freeze commit after `E`. `M` changes candidate metadata only, binds the stated `I`, `E`, and `A`, leaves `independent_review_verdict=PENDING`, and leaves `verified_complete=false`. The verified head remains unchanged.

## 4. Scope isolation

The implementation accounting uses the declared V4 zero-credit checkpoint as its lower boundary, so the acknowledged pre-E01 event-spine work is excluded from E01 credit. No finding in this review depends on crediting that pre-checkpoint work. No E02 or E03 owner is accepted as completed by this review.

## 5. Effective line bounds

The reviewer-owned strict verifier completed with exit code 0 and reported:

| Metric | Result |
|---|---:|
| Mechanical accepted source executable lines | 10,604 |
| Source target/five-hop rows | 278 / 278 |
| Gross changed executable TypeScript | 32,492 |
| Token-winnowing deduction | 663 |
| Effective changed TypeScript | 31,829 |
| Final production TypeScript | 49,412 |
| Final test TypeScript | 10,933 |
| Deleted Python | 35,151 |
| Declared mutation fingerprints | 121 / 121 |

The mechanical production, final-production, test, and Python-deletion quantities exceed their numeric floors. They do not compensate for the semantic source-custody failure. After the minimum proven invalid compact deduction, recognized source coverage is at most 9,644 lines and fails the 10,587-line hard floor.

## 6. Source-to-target audit and the previous 26-route dispute set

The current manifest contains 360 source rows: 278 accepted and 82 rejected.

For the previous 26-route dispute set:

- `e01-rej-0001` and `e01-rej-0002` are now rejected.
- Fifteen accepted compact rows from that dispute set fail the default-call-site/state-effect hops described in P0.
- `e01-src-0019` fails semantic equivalence and default-call-site behavior.
- The remaining eight routes do not offset invalid credit and were not used as additional deductions after the hard floor was already disproved.

The newly accepted `e01-src-0224` row reaches the same disconnected compact target and is included in the 960-line deduction.

## 7. Runtime origin and default execution

The reviewer-owned runtime-origin probe passed. The default E01 runtime reported the TypeScript canonical owner and runtime version 6. The Python-disable probe exited with code 1 and the expected `e01_typescript_runtime_disabled` failure, demonstrating that the exercised default entry does not silently fall back to the deleted Python runtime.

This result proves the broad TypeScript cutover origin. It does not make the compact custody hook dynamically effective.

## 8. Write-path and state-custody census

The canonical session journal, revisions, transition IDs, restart epochs, and ACK idempotency remain owned by the TypeScript runtime and persisted through its journal path. The context runtime and provider runtime own their established snapshots/prepared request records.

The newly added compact/provider custody helpers introduce additional in-memory singleton state. That state is neither incorporated into the established canonical snapshot nor restored from it. It must either be moved under the canonical owner, explicitly persisted/restored, or removed from source-credit claims.

## 9. Reviewer-owned probes

All successful dynamic probes used an exact archive of implementation `I` and reviewer-specific session IDs and payloads derived from the nonce.

| Probe | Result |
|---|---|
| Runtime origin | PASS: TypeScript canonical owner, runtime v6 |
| Same-session process epoch 1 | PASS: PID 75988, revision 2, restart epoch 0 |
| Same-session process epoch 2 | PASS: PID 84000, revision 4, restart epoch 1 |
| Same-session process epoch 3 | PASS: PID 83048, revision 6, restart epoch 2 |
| Transition-ID replay | PASS: none observed |
| Lost ACK retry | PASS: revision 1, committed count 1, repeated effect 0 |
| Python-disable | PASS: exit 1, `e01_typescript_runtime_disabled` |
| Pinned Bun version | PASS: 1.2.15 |
| Frozen install | PASS: 5 packages |
| Typecheck | PASS |
| Bun build and health | PASS |
| Node build and health | PASS |
| E01 test suite | PASS: 416 tests, 1,302 expects |

A reviewer mutation was prepared in the isolated archive to disconnect both ineffective compact hook calls. The targeted test command could not start because the sandboxed `npx` resolver was denied access to its global CLI path and then attempted a blocked registry connection. It was not retried because the input-type contradiction is statically deterministic, the candidate already fails the hard floor, and further low-value reproduction would not change the verdict.

## 10. Mutation and test credibility

The submitted mutation evidence declares 121 mutations and 121 fingerprints. All 121 patches match, compile, restore their original hashes, and have a designated killer recorded as failed. The runner parses per-test status rather than accepting a nonzero aggregate exit alone. There are 78 distinct designated killer names and 93 distinct target symbols; the declared split is 69 disconnect-target mutations and 52 semantic mutations.

That accounting defect from the previous attempt is fixed. It does not establish the missing compact default path because the compact source-custody tests and corresponding disconnect mutations operate on the helper directly. A mutation can be killed by a direct helper test while the real production caller remains a no-op. The 416-test suite therefore passes but is not failure-sensitive to the exact five-hop behavior claimed by the affected manifest rows.

## 11. High-risk validation

E01 changes the canonical language/runtime boundary and therefore warrants upgraded validation. The candidate supplied and the reviewer independently exercised the relevant TypeScript toolchain, build targets, runtime-origin path, process-epoch resume path, lost-ACK path, disable path, and mutation accounting. Those checks are sufficient to establish the positive cutover behavior but cannot waive a failed source-custody hard gate.

The combined toolchain wrapper did not emit a usable receipt from the archive environment; the reviewer ran each pinned component command directly instead. No additional full-repository suite was run because the decisive defect is local, deterministic, and already lowers recognized source coverage below the mandatory threshold.

## 12. Required state transition

The independent review result is `FAIL`. E01 must return to implementation/fix status. The verified head must remain `c34535a783e88f9481387ced89cba4fbc333dc74`, and E02/E03 must remain blocked.

This reviewer intentionally did not modify root `docs/milestones/execution-state.yaml`, candidate metadata, production code, tests, manifests, or remediation taskbooks. The owning implementation session must record the state transition after consuming this committed review.

## 13. Review evidence binding

Machine-readable evidence is committed with this report at [independent-review-v8.json](G:/agent-zoo/zyra/docs/reviews/evidence/M1-R01-v3/execution-01/independent-review-v8.json:1). The immutable review commit is the Git commit containing both reviewer-owned files; its full hash is returned to the owning session after commit creation.
