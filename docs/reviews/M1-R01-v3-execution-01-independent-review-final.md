# M1-R01 v3 Execution-01 Independent Review Final

## 1. Verdict

**FAIL**

The final review target does not satisfy the v3 candidate gate. The review found deterministic P0/P1 failures in immutable source identity, manifest closure, source-to-target semantic custody, effective changed-line credit, candidate scope, and independently reproduced behavior tests. Passing runtime probes, final/test line floors, Python-owner deletion, and internally consistent mutation receipts cannot offset these failures.

No production code or test was changed by this review. The root `docs/milestones/execution-state.yaml` was not modified because the parent reviewer owns the state transition.

## 2. Findings

### P0-1: all source manifest hashes describe checkout bytes, not the frozen commit

Claim overturned: the source manifest is immutable at Claude snapshot `c57f5a29e88e9a814bea47abeb9a0a6f725dc102`.

Independent audit compared every source record to bytes returned by `git show c57f5a29e88e9a814bea47abeb9a0a6f725dc102:<path>`. All `360/360` recorded hashes mismatch the commit blobs, while all `360/360` match the current CRLF-converted checkout. For `e01-src-0001` in `docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-source-manifest.jsonl:1`, the manifest records `a670af8657f66d31d18d2ee7ff452131b94ed2e4b318f5d84749e2db80cc8ae4`; the frozen `src/QueryEngine.ts` blob is `3b0d95a1f6b08239d269af9926d7e90a450e7d9904cb07fd88784283808636d6`.

Impact: source ranges are not cryptographically tied to the declared snapshot and are platform-dependent. The verifier checks source HEAD and then hashes working-tree bytes, so a clean checkout can still validate a non-frozen hash. Execution-01 must regenerate hashes from immutable blobs and make the verifier read snapshot objects directly.

### P0-2: nonce-selected accepted five-hop samples contain false semantic equivalence

Claim overturned: each accepted source range has source symbol -> target implementation -> callsite -> state mutation -> behavior-test custody.

The reviewer nonce was `c3d43d3e269ee30e962dd89293fef3c4cffe997e01f0ffcd05db28cacdcdaf91`. It selected four accepted records in each of query, tool, context, compact, provider, and session, plus four rejected records. The rejected records were absent as required, but multiple accepted chains were semantically false:

| Mapping | Source behavior | Claimed target | Failure |
| --- | --- | --- | --- |
| `e01-src-0243` | `claude-code-best/src/utils/toolResultStorage.ts:55`, feature/opt-out/minimum persistence threshold | `packages/runtime/claude-runtime/src/tools/execution-settlement-runtime.ts:512`, `appendProgress` | The target stores ordered bounded progress chunks and has no persistence-threshold selection policy. |
| `e01-src-0265` | `claude-code-best/src/utils/toolResultStorage.ts:557`, compactable candidate filtering | `packages/runtime/claude-runtime/src/loop/model-iteration-runtime.ts:349`, `recordToolObservation` | The target records one observation; it does not discover or filter compaction candidates. |
| `e01-src-0228` | `claude-code-best/src/utils/tokenBudget.ts:13`, continuation tracker construction | `packages/runtime/claude-runtime/src/context/token-runtime.ts:160`, `estimate` | Token estimation has no continuation counters or tracker timestamp. |
| `e01-src-0301` | `claude-code-best/src/utils/history.ts:58`, image-reference formatting | `packages/runtime/claude-runtime/src/loop/model-iteration-runtime.ts:596`, `restore` | Model-iteration restore has no image-reference formatting behavior. |
| `e01-src-0289` | `claude-code-best/src/utils/sessionRestore.ts:99`, file history, attribution, collapsed context, and todo restoration | `packages/runtime/claude-runtime/src/loop/model-iteration-runtime.ts:596`, `restore` | The target restores model rounds/tools/transcript only and does not own those source state domains. |
| `e01-src-0313` | `claude-code-best/src/utils/history.ts:283`, current flush-promise synchronization | `packages/runtime/claude-runtime/src/loop/model-iteration-runtime.ts:596`, `restore` | The target contains no flush-promise synchronization mechanism. |

Impact: one broken selected chain is a direct v3 failure; this sample has at least six. Execution-01 must split generic many-to-one mappings, reject unmigrated behavior, or implement the missing semantics with specific state, callsite, and sensitive tests.

### P0-3: candidate identity evidence is stale and manifest closure is incomplete

Claim overturned: evidence and manifests identify the exact final candidate and a defined default entry.

`docs/reviews/evidence/M1-R01-v3/execution-01/candidate-metadata.json:5` still identifies implementation `d6ff45435167c04c132e08e3de275a24cf28b544`, line 6 repeats it as the cleanroom target, line 7 identifies evidence commit `e4e3f7edeac6c628a988cf0ff273ad0f9f1d6e29`, and line 10 leaves `independent_review_target` null. Those values contradict implementation `6dc12a5cd68f18a85c286331fcb784b05434a2aa`, evidence `30be0d0ad7a55cf4d71d870d0e17d4191dc2b9dd`, and final target `be5a0fae9eaecb6a225f6eb1c9e781ed72761f5e`.

All 322 target-custody rows reference `e01.default-code-worker`, but `docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-gate-profile.json` defines no default-entry registry. The source manifest is also not sorted by `mapping_id`, contrary to schema v3. The candidate verifier does not reject either defect.

Impact: the receipts cannot establish that their results belong to the reviewed target, and the default entry cannot be resolved from frozen gate inputs. Execution-01 must refresh target metadata and add verifier-enforced referential integrity and ordering checks.

### P1-1: required token-winnowing deduction puts changed effective LOC below the floor

Claim overturned: changed effective TypeScript is `25,722`, above the `25,416` floor.

After removing blank/comment-only lines, the independent pre-clone changed count was `25,688`. A five-token AST-unit winnowing pass with length ratio `>= 0.8` and Jaccard `>= 0.8` found 79 near-clone/shared-skeleton units and deducted 777 shared lines. The conservative changed lower bound is therefore `24,943`, short of `25,416` by 473 lines. Examples include multiple structurally identical `*ToJson` functions and closely matching restore/normalization methods.

Impact: Execution-01 misses its anti-padding engineering floor. `scripts/verify_m1_r01_manifests.ts` must implement the contract's token-winnowing/control-skeleton deduction rather than relying only on exact whole-unit or token-kind hashes, and the implementation must add substantive non-duplicate custody rather than generated variants.

### P1-2: an allowed "control-plane" commit contains later production implementation

Claim overturned: intervening allowlisted commits are review/control-plane only and the candidate contains no later-execution preimplementation.

The gate profile allowlists commit `0cd21bff5171607680c11f2652c46e55a8a5a983` as a control-plane commit. Its diff includes `apps/api/zyra_api/main.py`, `package.json`, `packages/runtime/runtime-event-spine/**`, `packages/runtime/zyra_runtime/runtime_events/**`, and scripts. The baseline-to-implementation range therefore includes production state/event-spine work assigned outside this execution, not review-only metadata.

Impact: target isolation and LOC attribution are not trustworthy. Execution-01 must use a candidate ancestry containing only authorized E01 implementation plus review-only commits, or explicitly rebaseline under the unit-level scope rules before review.

### P1-3: the independently run E01 suite is not reproducibly green

Claim overturned: locked Bun evidence reports `381/381` passing behavior tests.

On final target `be5a0fae9eaecb6a225f6eb1c9e781ed72761f5e`, Bun `1.2.15` typecheck and build passed, but `bun run runtime:e01:test` produced `380 pass, 1 fail, 1219 assertions`. At `packages/runtime/claude-runtime/test/runtime.test.ts:317`, provider response stop reasons were actually `["end_turn", "tool_use"]` instead of `["tool_use", "end_turn"]`.

Impact: either canonical response ordering is nondeterministic or the submitted green receipt is stale/non-reproducible. In both cases the candidate fails the behavior gate. Execution-01 must make response projection order deterministic and regenerate clean receipts at the exact candidate target.

## 3. Target identity

| Role | Commit | Result |
| --- | --- | --- |
| Verified baseline | `c34535a783e88f9481387ced89cba4fbc333dc74` | Present |
| Implementation | `6dc12a5cd68f18a85c286331fcb784b05434a2aa` | Descends from baseline |
| Candidate evidence | `30be0d0ad7a55cf4d71d870d0e17d4191dc2b9dd` | Descends from implementation |
| Final review target | `be5a0fae9eaecb6a225f6eb1c9e781ed72761f5e` | Descends from evidence |
| Historical failed candidate | `df2937406e639862ec6a29795016b51651ee9cc0` | Not substituted for the final target |

The ancestry chain itself passes. Candidate metadata inside the evidence bundle does not.

## 4. Scope isolation

The baseline-to-implementation diff contains 181 files, 58,935 insertions, and 35,271 deletions. The E01 TypeScript package, tests, worker entry, and Python-owner deletions are present. The range also contains later runtime-event-spine and Python event-owner production code through the improperly allowlisted `0cd21bff5171607680c11f2652c46e55a8a5a983` commit. Scope isolation therefore fails.

The source repository is outside the Zyra Git boundary. Its HEAD was exactly `c57f5a29e88e9a814bea47abeb9a0a6f725dc102`, but checkout cleanliness and HEAD identity do not repair the blob-hash mismatch.

## 5. Independent line lower bounds

| Bucket | Independent lower bound | Floor | Result |
| --- | ---: | ---: | --- |
| Changed substantive TypeScript before clone deduction | 25,688 | N/A | Informational |
| Changed effective after required winnowing | 24,943 | 25,416 | FAIL |
| Final substantive TypeScript | 33,447 | 28,000 | PASS |
| Substantive TypeScript tests | 8,150 | 7,000 | PASS |
| Python-owner physical lines removed | 31,840 | 26,670 active profile | PASS |

Because the source snapshot hashes and sampled semantics fail, the independently defensible accepted-source semantic coverage is not credited as a passing gate even though the manifest lists 13,141 physical accepted source lines.

## 6. Source-language five-hop

The nonce selection covered exactly 24 accepted records, four in each required domain, and four rejected records. Rejected absence passed. Accepted semantic equivalence failed as detailed in P0-2. The failure pattern is systematic: many unrelated source symbols collapse onto a small number of generic target methods, tests, and mutation IDs without preserving their source policy or state domain.

## 7. Runtime origin

The reviewer-owned default-entry probe executed production TypeScript directly with a unique nonce. It made two compatible-provider calls, delegated one tool call, and confirmed that the second provider request contained a `role: "tool"` observation. This supports the narrow claim that the active provider -> tool -> observation -> provider loop is TypeScript-owned.

That runtime-origin success does not prove the 322 manifest mappings, immutable source identity, or candidate scope.

## 8. Write-path census

The candidate write-path receipt was inspected and the target map reports one canonical TypeScript owner per declared semantic domain. The Python owner manifest contains 763 delete dispositions, no listed Python owner path remains at the implementation commit, and the physical removed range is 31,840 lines.

The census does not close the undefined default-entry reference or later runtime-event-spine scope pollution. Those remain independent failures.

## 9. Reviewer probes

| Probe | Independent result |
| --- | --- |
| Default provider -> tool -> observation -> provider | PASS: two provider calls, one gateway call, second request includes a tool observation |
| Permission deny/ask gateway fence | PASS: both effects returned blocked receipts; gateway calls remained zero |
| Malformed SSE cleanup | PASS: run stopped with `model_stream_failed`; both discovered `inFlight` values were zero |
| Locked Bun TypeScript check | PASS |
| Locked Bun build | PASS: 77 modules, 1.14 MB entry |
| Locked Bun E01 suite | FAIL: 380/381 |

The first temporary probe attempt was invalid because it supplied no initial message; the runtime correctly rejected it. The corrected probe added the required user message and passed. The temporary probe file was removed after execution.

Same-session cross-process replay, lost-ACK process-kill replay, frozen reinstall, full cleanroom, built-health, and the full mutation runner were not independently rerun after hard gate failures were established. Candidate receipts for those areas were inspected. The omission cannot create a false PASS because the verdict is already blocked; it is recorded as residual unverified surface rather than positive evidence.

## 10. Test credibility

The mutation manifest and receipt set are internally consistent: 44 unique mutations, 44 recorded kills, no survivor/invalid entry, all patch fingerprints match the frozen manifest, and every original/restored target hash matches current production content. This review did not rerun all 44 compile/test cycles after the parent requested bounded closeout on a hard-failed candidate.

Test sensitivity is weakened by generic custody anchors reused across many semantically unrelated source ranges. More importantly, the exact E01 suite independently failed one committed assertion, so the submitted `381/381` receipt is not reproducible at the review target.

## 11. Upgrade triggers

This candidate hits the following upgrade triggers:

- It changes canonical runtime/session/provider/tool/context owners and deletes the prior Python-owner surface.
- Its ancestry includes production runtime-event-spine and API work outside E01's review-only allowance.
- Its evidence identity does not bind receipts to the final target.
- Its independently run behavior suite is not green.

A corrected candidate requires a new exact-target cleanroom, full manifest audit from immutable blobs, full mutation run, dependency/path audit, same-session/lost-ACK crash probes, and independent review. No later execution should begin before that review passes.

## 12. State transition

Recommended parent-owned transition:

`M1-R01-v3-Execution-01: implementation_complete_review_pending -> implementation_complete_review_failed`

Execution-02 and Execution-03 must remain blocked. This reviewer intentionally did not edit `G:/agent-zoo/docs/milestones/execution-state.yaml`.

## 13. Review evidence commit

The review evidence commit SHA is populated by the final response after committing this report and `docs/reviews/evidence/M1-R01-v3/execution-01-independent-review-final/reviewer-evidence.json`.

