# M1-R01 V4 Execution-01 Independent Review

## 1. Verdict

**FAIL**

The immutable review target `a6b9a8679170fa834265f941f281a3fd45d30022` does not satisfy the V4 E01 gate. The runtime repair is materially better and its main behavioral paths pass, but source-custody semantics, manifest schema conformance, frozen mutation identity, and independent cleanroom reproducibility contain blocking failures.

No production code, production test, candidate evidence, or root execution state was changed by this review. E02 and E03 must remain blocked.

## 2. Findings

### P0-1: nonce-selected accepted mappings still contain false semantic equivalence

Reviewer nonce: `a7668cb1855e0a364ba77563f627b157f8182a0a7f040c465cd5d0068dfe1172`.

The deterministic sample selected four accepted records in each required domain and four rejected records. At least four selected accepted chains are false:

| Mapping | Frozen source behavior | Claimed target | Failure |
| --- | --- | --- | --- |
| `e01-src-0011` | `src/query.ts:115`, a conditional module binding for `snipCompact` | `E01RuntimeCoordinator.decideQuery` | `decideQuery` handles abort, turn-limit, empty-input, and stop routing. It contains no snip-module loading, projection, or compaction behavior. |
| `e01-src-0004` | `src/QueryEngine.ts:127`, a conditional `snipProjection` module binding | `E01RuntimeCoordinator.decideQuery` | The target has no snip-boundary projection behavior. |
| `e01-src-0151` | `src/services/compact/microCompact.ts:56`, a nullable lazy module-cache slot | `ContextCompactionRuntime.compactConversation` | A module-cache slot is not semantically equivalent to an entire compaction state transition; the mapped test does not exercise cached-module initialization or reuse. |
| `e01-src-0264` | `src/utils/toolResultStorage.ts:536`, construction of a `tool_use_id -> tool_name` map by walking assistant messages | `ToolResultRuntime.deliver` | Delivery seals and projects one result; it does not reconstruct tool names from prior assistant messages. |

The source rows are at `G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-source-manifest.jsonl:49`, `:42`, `:189`, and `:302`. Their target claims are at `G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-target-custody-map.jsonl:25`, `:18`, `:165`, and `:271`. The generic state/test anchors do not repair the missing source behavior.

One false accepted nonce chain is an automatic source-language gate failure. Consequently, the structural `10,735` accepted executable-line count cannot be credited as semantically closed.

### P0-2: 14 accepted continuation rows use a forbidden source role

The source manifest marks 14 `e01-rej-*` continuation rows as `accepted: true` and `source_role: "reference_only"`. Schema V3 permits only `primary` or `supplementary` for `source_role` at `G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/schema-v3.md:54`.

The V4 continuation exception permits an `e01-rej-*` identifier when the range is the tail of the same accepted source symbol. It does not waive the source-role schema. The continuation symbols themselves match, but all 14 records violate the allowed role vocabulary. `verify_m1_r01_e01_v4.ts` returned `ok: true` because it does not enforce this field.

Affected IDs: `e01-rej-0001`, `e01-rej-0002`, `e01-rej-0003`, `e01-rej-0011`, `e01-rej-0019`, `e01-rej-0022`, `e01-rej-0023`, `e01-rej-0028`, `e01-rej-0029`, `e01-rej-0030`, `e01-rej-0031`, `e01-rej-0036`, `e01-rej-0037`, and `e01-rej-0038`.

### P0-3: 11 of 46 executed mutations do not match their frozen patch fingerprints

The independent detached-worktree mutation run did real edits and produced `46/46` compilable kills, `46` distinct mutant hashes, and exact source restoration. However, `11/46` `actual_patch_sha256` values differ from the corresponding frozen manifest fingerprint.

The mismatch is newline-dependent. For example, `e01-mut-001-restore-before-bootstrap` has frozen LF fingerprint `cbbe8d7a...`, while the fresh Windows worktree applied CRLF material and produced `b2f17db...`. The runner records both fields at `scripts/remediation/run_m1_r01_e01_mutations.ts:433` and `:437` but never compares them, so it reports a green mutation gate for a patch identity that is not the frozen patch identity.

Affected IDs: `001`, `002`, `003`, `007`, `014`, `017`, `021`, `026`, `029`, `031`, and `033`. Mutation sensitivity passes; frozen mutation integrity does not.

### P1-1: exact-target cleanroom is not independently reproducible

A detached worktree at implementation `cb5cc61627a71c60cea179736a8d9dda3f5aac39` ran the committed cleanroom procedure. `git archive` produced the exact `I` tree without `.git`, `node_modules`, `dist`, or `.tmp`; Bun `1.2.15` reported correctly. The next command, `bun install --frozen-lockfile`, exited `1` during the `bun` package postinstall with:

```text
bun: command not found: node
error: postinstall script from "bun" exited with 1
```

Typecheck, build, tests, and built health were therefore not reached in that cleanroom. The frozen candidate receipt claims the same command passed earlier, so this is specifically an independent reproducibility failure, not an assertion that the checked-out dependency tree cannot run.

### P2-1: the declared Node source stdio path cannot load the TypeScript runtime

Bun source health and both Bun/Node built-bundle health paths pass. The declared `stdio:node` path at `packages/runtime/claude-runtime/package.json:14` fails under Node `22.17.0` strip-only TypeScript loading because parameter properties are unsupported. Bun remains the registered default entry, so this is secondary to the hard failures above, but the declared Node source path is not usable as submitted.

## 3. Candidate identity and scope

| Role | Commit | Result |
| --- | --- | --- |
| Verified baseline | `c34535a783e88f9481387ced89cba4fbc333dc74` | Present |
| Actual E01 diff baseline | `0cd21bff5e2d160476f2ce3cef766bf53aab1239` | Explicit; zero E01 credit |
| Implementation candidate `I` | `cb5cc61627a71c60cea179736a8d9dda3f5aac39` | Present and reviewed |
| Candidate evidence `E` | `4e7ae6d721fb7be85a590d4a1ef2fa4fd1bf92a6` | Descends from `I` |
| Immutable review target `A` | `a6b9a8679170fa834265f941f281a3fd45d30022` | Descends from `E`; tree equals `E` |
| Metadata commit `M` | `fe4792171e6c2a10bc6f528b77366cc2320b8ff7` | Metadata-only descendant of `A` |

Candidate metadata correctly binds `I/E/A`, names the `0cd21...` diff baseline, and keeps `verified_complete=false`. The gate profile does not allowlist `0cd21...` as a control-plane commit, and the verifier assigns it zero E01 credit. Identity and rebaseline handling pass.

## 4. Structural manifest and raw-source audit

| Check | Independent result |
| --- | --- |
| Source closure | `360 = 295 accepted + 65 rejected` |
| Target closure | `295` target rows, one per accepted mapping |
| Ordering | Source, target, and mutation manifests sorted by ID |
| Raw source identity | `360/360` ranges reference 21 raw Git blobs at Claude snapshot `c57f5a29...`; zero hash mismatches |
| Checkout newline control | All 21 checkout-byte hashes differ from raw blobs, confirming the audit did not hash the CRLF checkout |
| Target identity | 14 unique target files at `I`; zero missing files or target hash mismatches |
| Default entry | `e01.default-code-worker` resolves to `apps/code-worker/src/main.ts`, symbol `main`, command `bun apps/code-worker/src/main.ts` |
| Previously false symbols | All five are rejected and have no target row |

The five explicitly retested prior false symbols are `getPersistenceThreshold`, `formatImageRef`, `restoreSessionStateFromLog`, `currentFlushPromise`, and `createBudgetTracker`. Their rejection repair passes.

Structural closure does not offset P0-1 or P0-2.

## 5. Effective-line audit

The committed verifier reports `31,423` gross executable changed TypeScript, `654` five-token winnowing deduction, and `30,769` effective changed lines.

A separate conservative reviewer implementation used five-token shingles, an eight-shingle winnowing window, minimum length ratio `0.8`, and minimum Jaccard `0.8`. It normalized identifiers and literals, included top-level declarations and class members, and intentionally over-deducted repeated type/property skeletons. It found:

| Measure | Reviewer result |
| --- | ---: |
| Token-bearing added lines | 31,697 |
| Deducted duplicate lines | 4,795 |
| Conservative effective lower bound | 26,902 |
| Required floor | 25,416 |

The effective changed-line floor passes even under the more aggressive deduction. Final production TypeScript (`48,348`), test TypeScript (`10,553`), and Python-owner deletion (`35,151`) also exceed their active floors.

## 6. Build, test, and runtime behavior

| Check | Result |
| --- | --- |
| Bun | `1.2.15` |
| TypeScript | `5.8.3` |
| `typecheck:e01` | PASS |
| Bun build | PASS, 78 modules, 1.16 MB entry |
| Source Bun health | PASS |
| Built Bun health | PASS |
| Built Node health | PASS |
| Source Node strip-only health | FAIL, unsupported TypeScript parameter property |
| Full E01 suite | PASS, `384/384`, 1,219 assertions |

Reviewer-owned randomized behavior probes passed:

- Provider stop-reason order is exactly `tool_use`, then `end_turn`.
- Provider round 1 produces a tool call; `ModelIterationRuntime.buildRevisionMessages()` invokes `ToolObservationBudgetRuntime`, emits a budgeted observation, and provider round 2 consumes the revised transcript.
- The original tool output remains unchanged while provider-visible observation content is replaced.
- The observation-budget snapshot contains no receipt, delivery, or accumulator collections; raw result receipt custody remains with `ToolResultRuntime`.
- Permission effects `deny` and `ask` produce zero gateway delegations.
- Malformed SSE is rejected and transport active/queued counts both return to zero.
- Same-session model-iteration restore adds one fresh transition with zero replay.
- The committed three-process resume probe reports revisions `2,4,6`, restart epochs `0,1,2`, and zero replayed transition IDs.
- The lost-ACK probe reports zero repeated effects.

The dynamic observation-budget repair is therefore accepted as real runtime behavior. It is not the reason for this FAIL.

## 7. Mutation audit

| Measure | Result |
| --- | ---: |
| Declared/applied | `46/46` |
| Compile survived | `46/46` |
| Killed | `46/46` |
| Unique mutant hashes | `46` |
| Restored source hashes | `46/46` |
| Frozen patch fingerprints matched | `35/46` |

Test sensitivity is strong, including both new observation-budget mutations. Patch identity remains a hard failure because the executed bytes are not the frozen bytes for 11 rows.

## 8. Cleanroom and dependency boundary

Dependency/path scanning passed: 95 runtime files, no forbidden root-source paths, no symlinks, and no relative package links. Disabling the TypeScript E01 owner makes the default entry fail as required.

The independent exact-`I` cleanroom failed at frozen install as described in P1-1. The cleanroom gate therefore fails despite the checked-out tree's green typecheck/build/tests.

## 9. State transition

Recommended parent-owned state:

`M1-R01-v3-Execution-01: implementation_complete_review_failed`

E02 and E03 remain blocked. This reviewer intentionally did not modify `G:/agent-zoo/docs/milestones/execution-state.yaml`.

## 10. Machine evidence

Reviewer-owned evidence is under `docs/reviews/evidence/M1-R01-v4/execution-01-independent-review/`.

