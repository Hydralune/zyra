# M1-R01 V8 Execution-01 Critical Self-Review

## Verdict

Internal verdict: **PASS, pending independent review**.

Implementation candidate: `745480e8d439b04f56d714c63cd29e2b4f27e870`

Verified baseline retained until an independent PASS: `c34535a783e88f9481387ced89cba4fbc333dc74`

This verdict does not mark E01 complete, advance the verified head, or unblock E02/E03.

## V7 independent-review failures and V8 closure

| V7 failure | V8 closure | Evidence |
| --- | --- | --- |
| Source credit was short after rejecting generic mappings. | V8 accepts 278 materially routed symbols and rejects 82. The strict immutable-blob profile credits 10,604 executable source lines against the 10,587 floor. | `strict-gate.json`, `source-to-target-five-hop.jsonl` |
| Twenty-six mappings lacked exact symbols, default callsites, state effects, behavior tests, or mutation links. | Provider cache/error custody and compaction custody now have concrete Zyra-owned implementations. All 278 mappings resolve target hashes and symbols, an invoking callsite, a reachable snapshot property, a test-body state assertion, and a killed target mutation. | `source-to-target-five-hop.jsonl` |
| Mutation evidence could report a kill when an unrelated test failed. | The runner now requires the designated killer test itself to have status `failed`; mentioning the test or observing another non-zero suite result is insufficient. All 121 declared mutations satisfy that rule. | `mutation-results.json` |
| Tests were highly repeated and did not establish source-specific behavior. | Twenty-six source-specific provider/compaction cases assert distinct cache TTL, breakpoint, cache-break, mismatch, error, microcompact, boundary, streaming-summary, and session-memory effects. Generic and strict line profiles independently exclude structural repetition. | `source-custody-specific.behavior.test.ts`, `effective-loc-report.json`, `strict-gate.json` |
| Default runtime depended on Node strip-types and lacked a Bun/typecheck/build chain. | Frozen Bun 1.2.15 install, TypeScript 5.8.3 typecheck, a Bun bundle and health entry, a separate Node v22.17.0 bundle and health entry, and the 416-test E01 suite pass in the toolchain and cleanroom probes. | `toolchain-result.json`, `cleanroom-result.json` |
| Same-session resume replayed 52 transition IDs because the E01 journal was not restored. | The process probe restores the persisted TypeScript journal before append across three process epochs and two forced restarts. Replayed transition IDs are empty, revisions are monotonic, and lost-ACK retries repeat no external effect. | `same-session-resume-result.json`, `lost-ack-result.json` |
| Implementation baseline and verified head were conflated. | The implementation diff baseline remains `0cd21bff...`, the verified baseline remains `c34535a7...`, and implementation candidate `745480e8...` receives no verified status before independent review. | `strict-gate.json`, `candidate-metadata.json` |
| Candidate/review identity was ambiguous. | V8 follows the documented four-stage protocol: implementation `I`, evidence `E`, immutable target `A`, and post-anchor metadata `M`. The independent reviewer will be assigned `A`, not `E`; metadata remains pending until those identities exist. | `candidate-metadata.json` after evidence binding |

## Real ownership and default-path effects

- `ClaudeRuntimeCore.run` remains the default query loop and restores `E01RuntimeCoordinator` state before any new journal append.
- `ProviderModelRuntime.prepare` invokes provider source custody on the real prepared request. It mutates cache-control blocks and records a concrete `sourceCustody` result rather than writing an observation-only ledger row.
- `ProviderRecoveryRuntime.classify` invokes the exact assistant-error conversion path for provider failures.
- `ContextCompactionRuntime.compactConversation` invokes compaction custody. The time gate, microcompaction edits, automatic compaction plan, preserved boundary, optional streamed summary, and session-memory result affect returned messages and compact state.
- `PermissionedCapabilityHost.executeBatch` remains the canonical mixed-batch permission and settlement path. Denied calls cannot reach the gateway, while allowed calls produce ordered receipts.
- `E01RuntimeCoordinator.recordRuntimeEvent` connects compaction events to provider telemetry and persists the resulting snapshot with the journal.

These links are represented by 278 five-hop records across 69 target symbols. A source item is not accepted merely because it shares a provider, compact, permission, or session label.

## Mutation and behavior-test review

The corpus declares 121 frozen mutations. Each mutation records its target path and symbol, immutable patch fingerprint, expected killer test, actual per-test statuses, and restored source hash. A mutation counts as killed only when the patch applies, the declared killer is observed with status `failed`, and the source restores to its original hash. The final result is 121 killed, zero survived, and zero invalid.

The behavior suite passes 416 tests with 1,302 expectations. The source-specific tests are not credited merely by test name: the five-hop verifier extracts the test body and requires its declared state tokens. V8 fixed a generator defect that had incorrectly used the test name as a body anchor; the gate then moved from 250/278 to 278/278 without weakening verifier logic.

## Effective-line review

The strict profile is authoritative for the anti-inflation floor. It reports 32,492 gross changed executable TypeScript lines, deducts 663 token-winnowing clone lines, and accepts 31,829 effective changed lines against the 25,416 floor. It reports 49,412 final production TypeScript lines, 10,933 final test TypeScript lines, and 35,151 deleted Python lines.

The generic AST profile independently reports 27,276 effective changed TypeScript lines, 37,529 final non-test lines, and 9,155 effective behavior-test lines. Generated data, ledgers, manifests, source pools, fixtures, adapter-only code, dormant modules, type-only declarations, and detected structural clones receive no effective implementation credit.

The strict source margin is narrow: 10,604 accepted executable lines exceed the 10,587 threshold by only 17 lines. Therefore any independently rejected source mapping can invalidate the source-custody floor. This is a release sensitivity, not spare capacity.

## Runtime and cleanroom review

- Runtime-origin and write-path probes identify TypeScript as the canonical owner and reject the legacy Python path.
- The disable probe removes the actual E01 coordinator and confirms that the default entry fails rather than falling back.
- The dependency audit finds no root-source relative dependency, npm link, symlink, or forbidden runtime path.
- The cleanroom is a `git archive` of implementation candidate `745480e8...`, starts without Git metadata, `node_modules`, `dist`, or `.tmp`, and uses a frozen install.
- Bun and Node are built and health-checked as separate outputs. Passing Node strip-types is not used as build evidence.
- Three real process epochs restore the same session journal with no transition-ID replay; lost-ACK retries leave the committed revision and effect count unchanged.

## Residual risks for independent review

- The 17-line accepted-source margin makes semantic sampling decisive. The reviewer should reject any broad-domain mapping and rerun the strict calculation rather than accepting the aggregate count.
- Mapping fan-in reaches 46. High-fan-in provider, compact, telemetry, and settlement targets require sampling for distinct source behavior, not just symbol existence.
- Provider TTL/cache-break diagnostic state is owned inside the provider custody runtime and materialized in prepared-request effects; the reviewer should verify restore expectations do not imply a second coordinator-owned cache state.
- Stream-summary custody is conditional on a real summary stream. The reviewer should confirm that this conditional branch is sufficient for the adopted source behavior and is not claimed as unconditional default work.
- Root remediation documents and `execution-state.yaml` are outside the Zyra Git repository. They must be updated explicitly after the immutable review chain is known and disclosed as a separate commit boundary.

## Release decision

Do not advance `verified_head`, mark E01 complete, or unblock E02/E03 until a fresh independent review returns PASS against immutable review target `A`, the final metadata verifier passes, and the root execution state records the same identities. Any independent FAIL reopens E01 and must be fixed; it cannot be waived by this self-review.
